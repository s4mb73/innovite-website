"""Vidora content audit — Claude Vision over a public Instagram grid.

Given an `instagram_snapshot.snapshot()` result, fetch the 12 grid images
from IG's public CDN, send them inline (base64) to Claude with a
structured scoring prompt, parse the JSON response, and return an
audit dict ready for the PDF renderer.

We fetch ourselves (urllib direct — IG CDN is publicly accessible, no
proxy needed) because Anthropic's URL-based image input respects
robots.txt and IG's CDN is disallowed there.

Scoring dimensions (Vidora v1 rubric, ported forward):
  visual_cohesion       — do the 12 images feel like one brand?
  production_quality    — pro vs phone-snap (lighting, sharpness, framing)
  video_presence        — % of grid that is video / motion
  on_brand_consistency  — logo, colours, typography, recurring motifs
  content_variety       — services / testimonials / BTS / educational mix
  visual_appeal         — would a customer stop scrolling?

Each 0-100, overall is a weighted average. Grade letter from overall:
  A 85+ · B 70-84 · C 55-69 · D 40-54 · F <40

Failure modes: every call returns either a populated audit dict, or
None. Callers must handle None (no audit available — skip the
audit section in the PDF, log the lead for manual review).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger("crm.pipeline.vidora_audit")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
# Sonnet 4.6 for vision audit — Haiku's vision is weak on critique
# quality; Opus is overkill cost-wise for batch audit work.
ANTHROPIC_MODEL = "claude-sonnet-4-6"
ANTHROPIC_VERSION = "2023-06-01"

_DEFAULT_TIMEOUT_S = 90

# Minimum grid sample size we'll grade. 12 is the IG grid convention;
# we tolerate up to 3 missing thumbnails to absorb transient CDN errors.
_MIN_IMAGES_FOR_AUDIT = 9

# How heavily each dimension counts toward the overall score.
_WEIGHTS = {
    "visual_cohesion":      0.20,
    "production_quality":   0.25,
    "video_presence":       0.15,
    "on_brand_consistency": 0.15,
    "content_variety":      0.10,
    "visual_appeal":        0.15,
}

_SYSTEM_PROMPT = (
    "You are a senior creative director auditing an Instagram grid on "
    "behalf of Vidora — a videography and content agency that pitches "
    "service businesses on fixing their content. Your output drives a "
    "sales PDF that goes directly to a prospect, so be specific, "
    "honest, and image-grounded. Never invent details the images don't "
    "support. Never flatter to be polite — fluff scores wreck the pitch."
)

_USER_TEMPLATE = """\
Audit the Instagram grid below.

Account: @{handle}
Followers: {followers:,}
Total posts: {total_posts:,}
Last post: {last_post_human}
Verified: {verified}
Business account: {business}
Category: {category}
Bio: {bio}
Recent grid engagement: {engagement_summary}

Score each dimension 0-100. Be brutally honest. Then list 3-5 specific
weaknesses (image-grounded). Then write ONE cold-email-opener sales hook
(<= 25 words) that creates curiosity, references a concrete weakness,
and offers Vidora's help without naming Vidora.

Respond as RAW JSON ONLY — no prose, no fences:
{{
  "scores": {{
    "visual_cohesion":      0-100,
    "production_quality":   0-100,
    "video_presence":       0-100,
    "on_brand_consistency": 0-100,
    "content_variety":      0-100,
    "visual_appeal":        0-100
  }},
  "weaknesses":  ["...", "...", "..."],
  "sales_hook":  "...",
  "summary":     "2-3 sentence overall verdict"
}}
"""


def audit(snapshot: dict, *, timeout: int = _DEFAULT_TIMEOUT_S) -> dict | None:
    """Audit a profile snapshot. Returns enriched audit dict or None."""
    if not snapshot or not snapshot.get("posts"):
        logger.info("audit: empty snapshot — nothing to score")
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("audit: ANTHROPIC_API_KEY not set — skipping")
        return None

    image_urls = [p.get("display_url") for p in snapshot["posts"] if p.get("display_url")]
    if not image_urls:
        logger.info("audit: no usable image URLs in snapshot")
        return None

    images = []
    failed = 0
    for url in image_urls:
        blob = _download_image(url)
        if blob is not None:
            images.append(blob)
        else:
            failed += 1
    # Refuse to score a half-grid: an audit prompt that claims to be
    # looking at "the grid" while seeing 5/12 thumbnails will produce
    # confidently wrong critiques. Threshold is set so we still tolerate
    # the odd transient CDN miss but never grade off a substantially
    # incomplete sample.
    if len(images) < _MIN_IMAGES_FOR_AUDIT:
        logger.warning(
            "audit: %d/%d images downloaded (need ≥%d) — refusing to score partial grid",
            len(images), len(image_urls), _MIN_IMAGES_FOR_AUDIT,
        )
        return None
    if failed:
        logger.info("audit: %d/%d images failed (proceeding with %d)",
                    failed, len(image_urls), len(images))

    user_text = _USER_TEMPLATE.format(
        handle=snapshot.get("handle") or "?",
        followers=snapshot.get("follower_count") or 0,
        total_posts=snapshot.get("post_count") or 0,
        last_post_human=_humanize_last_post(snapshot.get("last_post_at")),
        verified="yes" if snapshot.get("is_verified") else "no",
        business="yes" if snapshot.get("is_business_account") else "no",
        category=snapshot.get("business_category") or "—",
        bio=(snapshot.get("biography") or "—").replace("\n", " ")[:240],
        engagement_summary=_engagement_summary(snapshot["posts"]),
    )

    content = []
    for media_type, b64 in images:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        })
    content.append({"type": "text", "text": user_text})

    raw = _call_anthropic(
        api_key=api_key,
        system_prompt=_SYSTEM_PROMPT,
        content_blocks=content,
        timeout=timeout,
    )
    if not raw:
        return None

    parsed = _parse_json(raw)
    if parsed is None:
        logger.warning("audit: response not parseable — head=%r", raw[:200])
        return None

    return _finalize(snapshot, parsed)


# ── Image download ─────────────────────────────────────────────────

_IMAGE_TIMEOUT_S = 15
_IMAGE_MAX_BYTES = 6_000_000  # 6MB ceiling per image — IG grid pics are well under 1MB

_VALID_MEDIA_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")


def _download_image(url: str) -> tuple[str, str] | None:
    """Fetch an IG CDN URL → (media_type, base64). Returns None on failure."""
    if not url:
        return None
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36"),
        "Accept":      "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer":     "https://www.instagram.com/",
    })
    try:
        with urllib.request.urlopen(req, timeout=_IMAGE_TIMEOUT_S) as resp:
            media_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            data = resp.read(_IMAGE_MAX_BYTES + 1)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        logger.info("image fetch failed: %s — %s", type(e).__name__, str(e)[:120])
        return None

    if len(data) > _IMAGE_MAX_BYTES:
        logger.info("image too large (>%d bytes), skipping", _IMAGE_MAX_BYTES)
        return None
    if media_type not in _VALID_MEDIA_TYPES:
        # Fall back to jpeg — IG CDN sometimes serves with generic types.
        media_type = "image/jpeg"
    return media_type, base64.b64encode(data).decode("ascii")


# ── Anthropic call ─────────────────────────────────────────────────

def _call_anthropic(*, api_key: str, system_prompt: str,
                    content_blocks: list, timeout: int) -> str | None:
    payload = json.dumps({
        "model":      ANTHROPIC_MODEL,
        "max_tokens": 1500,
        "system":     system_prompt,
        "messages":   [{"role": "user", "content": content_blocks}],
    }).encode("utf-8")

    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=payload,
        headers={
            "Content-Type":      "application/json",
            "x-api-key":         api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            pass
        logger.warning("anthropic HTTP %s: %s", e.code, body)
        return None
    except (urllib.error.URLError, TimeoutError) as e:
        logger.warning("anthropic call failed: %s", e)
        return None

    for block in data.get("content") or []:
        if block.get("type") == "text":
            return block.get("text", "")
    logger.warning("anthropic returned no text block")
    return None


# ── Parsing + scoring ──────────────────────────────────────────────

def _parse_json(raw: str) -> dict | None:
    """Tolerate fenced output even though we asked for raw JSON."""
    txt = (raw or "").strip()
    if txt.startswith("```"):
        # Strip first and last fence lines.
        lines = txt.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        txt = "\n".join(lines).strip()
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        # Last-ditch: find the first { and matching }.
        start = txt.find("{")
        end = txt.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(txt[start:end + 1])
            except json.JSONDecodeError:
                pass
    return None


def _finalize(snapshot: dict, parsed: dict) -> dict | None:
    """Compute overall + grade + attach snapshot fields the PDF needs.

    Returns None if the model omitted or mangled any score dimension —
    silently coercing nulls to 0 would write a fake F-grade lead into
    the CRM with no signal that the audit was actually broken.
    """
    raw_scores = parsed.get("scores") or {}
    clean_scores: dict[str, int] = {}
    bad: list[str] = []
    for k in _WEIGHTS:
        v = _try_clamp(raw_scores.get(k))
        if v is None:
            bad.append(k)
        else:
            clean_scores[k] = v
    if bad:
        logger.warning(
            "audit: model omitted/invalid dimensions %s — discarding (raw_scores=%r)",
            bad, raw_scores,
        )
        return None

    overall = round(sum(clean_scores[k] * w for k, w in _WEIGHTS.items()), 1)

    return {
        "handle":         snapshot.get("handle"),
        "scores":         clean_scores,
        "overall_score":  overall,
        "grade":          _grade_for(overall),
        "weaknesses":     [w for w in (parsed.get("weaknesses") or [])
                           if isinstance(w, str) and w.strip()][:5],
        "sales_hook":     (parsed.get("sales_hook") or "").strip()[:240],
        "summary":        (parsed.get("summary") or "").strip()[:600],
        "model":          ANTHROPIC_MODEL,
        "audited_at":     datetime.now(timezone.utc).isoformat(),
    }


def _try_clamp(v) -> int | None:
    """Coerce a score to 0..100 int. Returns None if not a number."""
    if v is None:
        return None
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, n))


def _grade_for(overall: float) -> str:
    if overall >= 85: return "A"
    if overall >= 70: return "B"
    if overall >= 55: return "C"
    if overall >= 40: return "D"
    return "F"


# ── Prompt helpers ─────────────────────────────────────────────────

def _humanize_last_post(iso: str | None) -> str:
    if not iso:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    days = (datetime.now(timezone.utc) - dt).days
    if days <= 0:    return "today"
    if days == 1:    return "yesterday"
    if days < 14:    return f"{days} days ago"
    if days < 60:    return f"{days // 7} weeks ago"
    if days < 730:   return f"{days // 30} months ago"
    return f"{days // 365} years ago"


def _engagement_summary(posts: list) -> str:
    likes = [p.get("like_count") or 0 for p in posts if p.get("like_count") is not None]
    comments = [p.get("comment_count") or 0 for p in posts if p.get("comment_count") is not None]
    videos = sum(1 for p in posts if p.get("is_video"))
    if not likes:
        return "no engagement data"
    avg_likes = sum(likes) // max(1, len(likes))
    avg_comments = sum(comments) // max(1, len(comments))
    return (f"avg {avg_likes:,} likes, {avg_comments:,} comments per post · "
            f"{videos}/{len(posts)} posts are video")
