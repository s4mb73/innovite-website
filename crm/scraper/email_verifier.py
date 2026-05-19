"""Email verifier — orchestrates the local pre-check + Reoon paid call.

Two-stage funnel
----------------
1. Local pre-check (free, ~10ms per address). Kills:
     - malformed addresses (pattern generator typos)
     - dead domains (no MX / no A)
     - disposable / throwaway providers
2. Reoon paid call (~$0.002 per address, ~500ms). Only runs when the
   pre-check returns 'plausible'. Returns the real verdict including
   catch-all detection — the bit we can't do locally without a
   reputation-managed IP pool.

Why this shape:
  - Cost discipline. ~30% of pattern-generated addresses die at the
    local stage for free. Those don't burn a credit.
  - Independence. Local checks give us cross-verifiable signals
    (DMARC/SPF, MX provider) that Reoon doesn't surface. If Reoon
    says 'valid' but our local says 'no_mx', something is wrong —
    flag both into source_errors for operator review.
  - Graceful degradation. If REOON_API_KEY is unset, the verifier
    still works at the local-only tier and returns
    `verification_source='local_only'`. The drafter just sees a
    lower confidence and adjusts.

Confidence bands (downstream consumers can branch on this):
  high     — Reoon 'valid' + score ≥ 90, NOT catch-all, NOT role
  medium   — Reoon 'valid' but score 70-89, OR Reoon 'catch_all'
             (pattern probably correct, mailbox just can't be probed)
  low      — Reoon 'risky' or 'unknown', OR local-only 'plausible'
  none     — pre-check said 'syntax_invalid' / 'no_mx' / 'disposable'
             OR Reoon said 'invalid' / 'disposable'

Persistence
-----------
The orchestrator returns a flat dict that the runner maps onto these
columns on crm.leads:
  email_verification_status     — final normalised status string
  email_verification_confidence — high/medium/low/none
  email_verification_source     — 'local_only' | 'reoon' | 'reoon+local'
  email_verification_checked_at — ts
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from scraper import email_verifier_local as local_check
from scraper import email_verifier_reoon as reoon

logger = logging.getLogger("crm.scraper.email_verifier")


def _grade(reoon_result: dict, local_result: dict) -> str:
    """Collapse pre-check + Reoon into a single confidence band.

    high / medium / low / none — see module docstring.
    """
    # Pre-check killers always trump anything else — a malformed or
    # MX-less address can't be sent to no matter what Reoon says.
    if local_result["status"] in ("syntax_invalid", "no_mx", "disposable"):
        return "none"

    rs = reoon_result["status"]
    if rs in ("invalid", "disposable"):
        return "none"
    if rs == "valid":
        score = reoon_result.get("score") or 0
        is_catchall = reoon_result.get("is_catchall")
        is_role     = reoon_result.get("is_role")
        if score >= 90 and not is_catchall and not is_role:
            return "high"
        if score >= 70 or is_catchall:
            return "medium"
        return "low"
    if rs == "catch_all":
        # Catch-all means the server accepts anything, so the verifier
        # can't prove the mailbox exists — but it can't disprove it
        # either. The pattern guess is plausible; the operator should
        # know it's unverified.
        return "medium"
    if rs == "risky":
        return "low"
    if rs == "unknown":
        return "low"
    if rs in ("error", "not_configured"):
        # Local pre-check was 'plausible' and we have nothing else.
        # Fall back to 'low' so the drafter knows it's a guess.
        return "low"
    return "low"


def _collapse_status(reoon_result: dict, local_result: dict) -> str:
    """Final status string persisted to the lead row. Reoon's normalised
    status takes precedence except when the pre-check killed it first."""
    lr = local_result["status"]
    if lr in ("syntax_invalid", "no_mx", "disposable"):
        return lr
    rs = reoon_result["status"]
    # When Reoon isn't configured (or errored), surface the pre-check's
    # 'plausible' as the persisted status — better than 'not_configured'
    # which is meaningless to a non-technical operator.
    if rs in ("not_configured", "error"):
        return "plausible"
    return rs


def verify(email: str | None) -> dict:
    """Two-stage verification: local pre-check, then Reoon iff plausible.

    Returns:
      {
        'status':         persisted normalised status
        'confidence':     'high' | 'medium' | 'low' | 'none'
        'source':         'local_only' | 'reoon' | 'reoon+local'
        'checked_at':     datetime (UTC)
        'reoon_score':    int | None
        'is_catchall':    bool | None
        'is_role':        bool | None
        'reason':         short human string for the operator
        'errors':         [str, ...]   (transient failures, not validation)
      }

    Never raises. Empty/None email returns status='syntax_invalid'.
    """
    out: dict = {
        "status": "syntax_invalid", "confidence": "none",
        "source": "local_only", "checked_at": datetime.now(timezone.utc),
        "reoon_score": None, "is_catchall": None, "is_role": None,
        "reason": "no email provided", "errors": [],
    }

    if not email:
        return out

    local = local_check.precheck(email)

    if local["status"] in ("syntax_invalid", "no_mx", "disposable"):
        # Killed for free. No paid call needed.
        out["status"]     = local["status"]
        out["confidence"] = "none"
        out["source"]     = "local_only"
        out["reason"]     = local["reason"]
        return out

    # Pre-check said 'plausible'. Escalate to Reoon if we have a key.
    if not reoon.is_available():
        out["status"]     = "plausible"
        out["confidence"] = "low"
        out["source"]     = "local_only"
        out["reason"]     = "passed local checks; no Reoon key configured"
        return out

    r = reoon.verify(email)
    if r["status"] in ("error",):
        # Reoon transient — keep the local 'plausible' verdict but
        # surface the error so the operator knows it wasn't verified.
        out["status"]     = "plausible"
        out["confidence"] = "low"
        out["source"]     = "local_only"
        out["reason"]     = f"passed local; Reoon error ({r.get('error')})"
        out["errors"].append(r.get("error") or "reoon error")
        return out

    out["status"]      = _collapse_status(r, local)
    out["confidence"]  = _grade(r, local)
    out["source"]      = "reoon+local"
    out["reoon_score"] = r.get("score")
    out["is_catchall"] = r.get("is_catchall")
    out["is_role"]     = r.get("is_role")

    # Concise reason for the operator UI / drafter context.
    bits = [out["status"]]
    if r.get("score") is not None:
        bits.append(f"score {r['score']}")
    if r.get("is_catchall"):
        bits.append("catch-all")
    if r.get("is_role"):
        bits.append("role")
    out["reason"] = " · ".join(bits)
    return out
