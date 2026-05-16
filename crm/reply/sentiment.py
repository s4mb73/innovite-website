"""Reply sentiment classification via Anthropic Haiku.

Returns one of:
  'positive' | 'neutral' | 'negative' | 'ooo' | 'referral' | 'wrong_person'.

Definitions
-----------
positive     — interest, question, "yes let's talk", asks for more info, agrees to a call.
neutral      — non-committal acknowledgement, "noted", "not now but maybe later".
negative     — unsubscribe, "not interested", angry, hostile.
ooo          — out-of-office, autoreply, vacation responder.
referral     — "I'm not the right person, try X" with a name or address.
wrong_person — "I left this company" / "wrong contact" with NO redirect.

Why split referral vs wrong_person
----------------------------------
Referrals have the highest meeting-conversion rate of any inbound — the
recipient is recommending you to someone inside their network. They
deserve a dedicated, fast handler. Wrong-person without a redirect is
recoverable in a different way: re-enrich the company for the actual
decision-maker. Bucketing both into 'neutral' loses both signals.

Why Haiku, not Sonnet
---------------------
Tiny classification task, cheap and fast (~£0.0002/call at scale).
Sonnet doesn't add accuracy at this length; reserve it for the
reply-DRAFTER (US-023, deferred).

Fallback
--------
No key, network failure, or unparseable response → 'neutral'. We pick
neutral because it's the least-aggressive default: the operator still
sees the reply in the Inbox; cadence still cancels (US-009); no
spurious unsubscribe is recorded.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

VALID = {"positive", "neutral", "negative", "ooo", "referral", "wrong_person"}

SYSTEM_PROMPT = """You classify B2B sales reply emails into ONE of:
  positive | neutral | negative | ooo | referral | wrong_person

positive     — interested, asks a question, agrees to a call, asks for more info.
neutral      — non-committal acknowledgement, "noted", "not now but maybe later".
negative     — unsubscribe, hostile, "not interested", remove me.
ooo          — out-of-office, autoreply, vacation responder.
referral     — "I'm not the right person — try X" AND names a specific
               person, email, or department to contact instead.
wrong_person — "I'm no longer at this company" OR "this is the wrong
               address" with no specific redirect provided.

Decision rules:
- If they redirect to a named contact, it's 'referral' (not 'neutral'
  or 'wrong_person').
- If they say they're not the right person but provide no redirect,
  it's 'wrong_person'.
- If they say "I'll forward this" without naming a contact, that's
  'positive' (they're acting as a referrer, conversation continues).

Output strictly: one of those six words, lowercase, no punctuation, no
explanation. Just the single word."""


def _api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY")


def _call_anthropic(body: str, timeout: int = 8) -> str | None:
    payload = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": 10,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": body}],
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
                    return (b.get("text") or "").strip().lower()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return None
    return None


def classify(reply_body: str, subject: str | None = None) -> str:
    """Return one of {positive, neutral, negative, ooo}. Never raises."""
    if not reply_body:
        return "neutral"

    # Cheap OOO short-circuit — saves an API call when the regex pattern
    # is obvious. Real classifier handles the rest.
    lowered = (subject or "").lower() + " " + reply_body[:300].lower()
    if any(token in lowered for token in (
        "out of office", "out-of-office", "auto-reply", "auto reply",
        "on leave", "annual leave", "currently away",
    )):
        return "ooo"

    if not _api_key():
        return "neutral"

    # Truncate body — Haiku doesn't need the full thread for classification.
    raw = _call_anthropic(reply_body[:2000])
    if not raw:
        return "neutral"
    raw = raw.split()[0].strip(".,;:") if raw else ""
    return raw if raw in VALID else "neutral"
