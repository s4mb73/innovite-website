"""Day-1 email drafter — Anthropic Haiku with templated fallback.

Reads the lead's enriched profile + the chosen hook_type and writes a
subject + body that respects the Innovite marketing copy rules:
  - UK English (organise, optimise, analyse, ...)
  - No banned words (leverage, utilise, streamline, solutions, ...)
  - No emoji
  - No exclamation marks
  - Short sentences. Concrete > abstract.

Why Haiku, not Sonnet/Opus
--------------------------
Output is a short email. Haiku writes good copy at this length, cheap
and fast (sub-2-second p50). Sonnet is reserved for reply classification
later, where intent inference is the bottleneck not throughput.

Fallback
--------
If ANTHROPIC_API_KEY is missing or the call fails / times out, we return
a templated draft. The drafter NEVER fails the run — a templated email
is better than no email. The fallback is marked in the body subject so
the operator can spot it before approving sends.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"  # Latest Haiku per memory cutoff

SYSTEM_PROMPT = """You are writing the Day-1 outreach email for Innovite, a UK B2B
AI lead-generation agency. Style rules (these are absolute):

- UK English only: organise, optimise, analyse, personalise, behaviour.
- Banned words — do not use any of these: leverage, utilise, streamline,
  solutions, cutting-edge, innovative, transform, revolutionise, synergy,
  ecosystem, empower, game-changer, best-in-class, hyper-personalised.
- No emoji. No exclamation marks. No em-dashes.
- Short sentences. Concrete over abstract.
- Voice: confident founder, direct, anti-fluff.
- Length: 80-130 words for the body. 5-8 words for the subject.
- No fake personal touches ("I was just on your website earlier..."). Be
  honest about why you are writing.
- Sign off as "Sammy" (single line, no title).

Output strictly as JSON:
{"subject": "...", "body": "..."}
No markdown, no commentary, no code fences. Just the JSON object."""


HOOK_GUIDANCE = {
    "low_rating_with_volume":
        "Open with the rating signal — they have many reviews but their average is dragging. "
        "Tie it to lost local search visibility and lead quality.",
    "rating_under_4":
        "Open by acknowledging their reviews exist (so the email is grounded), then offer a "
        "way to improve review velocity and quality.",
    "few_reviews":
        "Open with the review-count signal — they are invisible in local search. "
        "Tie it to competitors who outrank them on review volume.",
    "no_ssl":
        "Open with the trust signal — their site is not on HTTPS so browsers warn visitors. "
        "Tie it to bounce rate before form submissions.",
    "no_website":
        "Open by noting they have a Google Maps presence but no website surfaces in search. "
        "Tie it to outbound becoming the only way to fill the pipeline.",
    "no_decision_maker":
        "Open with the local-business angle — keep it generic but warm; we do not have a "
        "named contact so do not invent one.",
    "dm_name_only_no_email":
        "Open with the contact's name and a question about how they currently handle outbound.",
    "general_growth":
        "Open with the local-business growth angle — no specific weakness yet, so anchor on "
        "their industry and city and ask one specific question.",
}


def _api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY")


def _templated_fallback(business: dict, hook_type: str) -> dict:
    """Used when Anthropic is unavailable. Spotting this in production
    should trigger a check of the ANTHROPIC_API_KEY env var."""
    name = business.get("decision_maker_name") or "there"
    company = business.get("business_name", "your business")
    return {
        "subject": f"Quick question for {company}",
        "body": (
            f"Hi {name},\n\n"
            f"I run a UK agency that helps {company.lower()}-type businesses get "
            f"more qualified leads without scaling headcount.\n\n"
            f"Worth a 15-minute call to share what we've seen working in your sector?\n\n"
            f"Sammy\n\n"
            f"[Templated fallback — Anthropic call did not complete.]"
        ),
    }


def _call_anthropic(prompt: str, timeout: int = 12) -> str | None:
    payload = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": 600,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": _api_key() or "",
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            blocks = data.get("content") or []
            for b in blocks:
                if b.get("type") == "text":
                    return b.get("text", "")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return None
    return None


def _build_prompt(business: dict, hook_type: str) -> str:
    parts = [
        f"Business: {business.get('business_name', '')}",
        f"City: {business.get('city', '')}",
        f"Website: {business.get('website') or '(none)'}",
        f"Google rating: {business.get('google_rating') or '—'} "
        f"({business.get('google_review_count') or 0} reviews)",
    ]
    if business.get("companies_house_revenue_band"):
        parts.append(f"Revenue band (CH heuristic): {business['companies_house_revenue_band']}")
    if business.get("decision_maker_name"):
        parts.append(f"Decision maker: {business['decision_maker_name']} "
                     f"({business.get('decision_maker_title', '')})")

    guidance = HOOK_GUIDANCE.get(hook_type, HOOK_GUIDANCE["general_growth"])

    return (
        "Write the Day-1 outreach email for this lead.\n\n"
        + "\n".join(parts)
        + f"\n\nHook guidance: {guidance}\n\n"
        "Output the JSON object only."
    )


def draft_day1(business: dict, hook_type: str) -> dict:
    """Returns {"subject": "...", "body": "..."}. Never raises."""
    if not _api_key():
        return _templated_fallback(business, hook_type)

    raw = _call_anthropic(_build_prompt(business, hook_type))
    if not raw:
        return _templated_fallback(business, hook_type)

    # Try to parse the JSON — Haiku sometimes wraps it.
    raw = raw.strip()
    if raw.startswith("```"):
        # Strip fences if the model added them despite the prompt.
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        return _templated_fallback(business, hook_type)

    if not isinstance(out, dict) or "subject" not in out or "body" not in out:
        return _templated_fallback(business, hook_type)

    return {"subject": str(out["subject"]), "body": str(out["body"])}
