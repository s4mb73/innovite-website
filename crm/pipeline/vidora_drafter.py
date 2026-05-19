"""Vidora Day-1 email drafter.

Distinct from pipeline/drafter.py (which serves accountancy / ROCA) — the
inputs are different (audit weaknesses + sales_hook + summary, not CH /
DNS / LinkedIn signals) and the tone is different (Vidora pitches video
production to media-shy SMBs, not accountancy efficiency to overworked
finance directors).

Same shape contract: returns {subject: str, body: str}. The caller writes
those onto crm.leads.email_subject / email_body_day1 and queues a row in
crm.emails — exactly mirroring how runner.py uses pipeline.drafter.

Falls back to a templated draft if ANTHROPIC_API_KEY is missing or the
call times out. We always return a usable shape — never None — so the
caller never has to branch.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger("crm.pipeline.vidora_drafter")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"  # dated; matches drafter.py

SYSTEM_PROMPT = """You are writing the Day-1 cold email for Vidora Media,
a UK video-content production agency that helps consumer-facing service
businesses (clinics, salons, restaurants, fitness studios) get more
inbound enquiries by upgrading their Instagram grid from amateur snaps
to scripted short-form video.

Tone: direct, warm, anti-fluff. UK English. No exclamation marks.
No banned words: leverage, utilise, streamline, solutions, transform,
revolutionise, synergy, empower, game-changer.

Structure:
  - Subject line: 4-7 words. Lowercase except the business name. No clickbait.
  - Body: 80-130 words.
      1. Opening line referencing one specific image-grounded weakness
         from their grid (not generic). Lead with what you noticed, not
         what they're missing.
      2. One sentence on what changes when the grid is scripted video
         (no claims, no stats — just the shift).
      3. Single soft CTA: 15-minute call.
      4. Sign-off: "Louis" on its own line.

Hard rules:
  - No emoji.
  - Do not mention "Vidora" by name.
  - Do not link out.
  - Do not say "I came across your profile" or "I noticed you" verbatim
    — vary the opener.

Return strict JSON only:
  {"subject": "...", "body": "..."}
No prose, no fences, no commentary."""


def _api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY")


def _call_anthropic(prompt: str, timeout: int = 12) -> str | None:
    api_key = _api_key()
    if not api_key:
        return None
    payload = json.dumps({
        "model":      ANTHROPIC_MODEL,
        "max_tokens": 500,
        "system":     SYSTEM_PROMPT,
        "messages":   [{"role": "user", "content": prompt}],
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
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        logger.warning("vidora drafter: anthropic call failed: %s", e)
        return None
    for block in data.get("content") or []:
        if block.get("type") == "text":
            return block.get("text", "")
    return None


_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_json(raw: str) -> dict | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = _JSON_FENCE.search(raw)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return None
    return None


# Marker text the fallback body carries. Any draft body containing this
# substring is operator-debug output, not a real outbound email; the
# caller raises on it rather than queueing it for a clinic to read.
_FALLBACK_MARKER = "[Templated fallback"


class TemplatedFallbackError(RuntimeError):
    """Raised when the drafter could only produce the operator-debug
    templated fallback. Forces the caller to mark the lead email_status=
    'not_found' rather than queueing the marker as a real send."""


def _fallback(business_name: str) -> dict[str, str]:
    return {
        "subject": f"quick thought on {business_name}",
        "body": (
            "Hi,\n\n"
            f"Took a look at {business_name} on Instagram — the photography "
            "is clean but the grid leans heavily on static posts.\n\n"
            "We work with consumer-facing service businesses to upgrade "
            "that kind of grid into scripted short-form video — the kind "
            "that turns a casual scroll into a booking enquiry.\n\n"
            "Worth a 15-minute call to walk you through what we'd change?\n\n"
            "Louis\n\n"
            f"{_FALLBACK_MARKER} — Anthropic call did not complete.]"
        ),
    }


def draft_day1(audit: dict[str, Any], snapshot: dict[str, Any],
               business: dict[str, Any]) -> dict[str, str]:
    """Generate the Day-1 cold email for a Vidora lead.

    Args:
      audit: vidora_audit.audit() output — sales_hook, weaknesses, summary
      snapshot: instagram_snapshot.snapshot() output — handle, follower_count, bio
      business: google_places candidate — business_name, city, website

    Returns {subject, body}. Never raises; templated fallback on any
    failure so the caller can always write something to the lead row.
    """
    business_name = (business.get("business_name")
                     or snapshot.get("full_name")
                     or snapshot.get("handle")
                     or "your business")
    weaknesses = audit.get("weaknesses") or []
    sales_hook = (audit.get("sales_hook") or "").strip()
    summary = (audit.get("summary") or "").strip()

    prompt = (
        f"Business: {business_name}\n"
        f"City: {business.get('city') or '—'}\n"
        f"Instagram handle: @{snapshot.get('handle') or '?'}\n"
        f"Followers: {snapshot.get('follower_count') or '—'}\n"
        f"Bio: {(snapshot.get('biography') or '').strip()[:200] or '—'}\n"
        f"\n"
        f"Audit overall: {audit.get('overall_score')}/100  "
        f"(grade {audit.get('grade') or '?'})\n"
        f"Audit summary: {summary or '—'}\n"
        f"Image-grounded weaknesses (verbatim from vision pass):\n"
        + "\n".join(f"  - {w}" for w in weaknesses[:5])
        + f"\n\nSales hook the audit produced (use as a thinking aid, "
          f"not verbatim): {sales_hook or '—'}\n"
    )

    raw = _call_anthropic(prompt)
    parsed = _parse_json(raw or "")
    if not parsed or not parsed.get("subject") or not parsed.get("body"):
        logger.info(
            "vidora drafter: Anthropic call failed for %s — refusing to "
            "ship the templated fallback as a real email",
            business_name,
        )
        raise TemplatedFallbackError(
            f"no valid draft produced for {business_name!r}"
        )

    out_body = str(parsed["body"]).strip()
    # Belt-and-braces: if Anthropic ever echoes the marker back inside a
    # genuine response, treat it the same as a fallback. Cheaper than
    # sending the marker to a clinic and discovering the leak later.
    if _FALLBACK_MARKER in out_body:
        raise TemplatedFallbackError(
            f"draft body contained fallback marker for {business_name!r}"
        )

    return {
        "subject": str(parsed["subject"]).strip()[:120],
        "body":    out_body,
    }
