"""Reoon Email Verifier API adapter.

Why Reoon (vs Hunter / ZeroBounce / NeverBounce / MillionVerifier)
-----------------------------------------------------------------
Researched against actual user discussion on r/coldemail / HN: Reoon
hit our hard requirements:
  - True pay-as-you-go (no monthly commitment) at ~$0.002 / verify
  - Sub-1s API response, suitable for inline use during enrichment
  - Returns a graded answer on catch-alls (`unknown` / `risky` /
    `catch-all`) rather than refunding the credit and giving us
    nothing useful (the MillionVerifier failure mode)

Endpoint
--------
GET https://emailverifier.reoon.com/api/v1/verify
    ?email=<addr>&key=<api_key>&mode=power

Modes:
  - instant : MX + syntax checks only. We do these locally anyway,
              so paying Reoon for them is wasteful.
  - power   : Full SMTP-level verification including catch-all
              detection. The mode we want.

Response shape (normalised to our internal vocabulary):
  Reoon status      → our status
  ---------------------------------
  safe              → 'valid'
  invalid           → 'invalid'
  disabled          → 'invalid'      (mailbox exists but rejects)
  catch_all         → 'catch_all'
  risky             → 'risky'
  unknown           → 'unknown'
  disposable        → 'disposable'

Failure handling
----------------
  - REOON_API_KEY unset    → status='not_configured', skip silently.
                             Lets the verifier ship without forcing
                             a key into every dev environment.
  - HTTP 4xx (rate-limit /
    bad key)               → status='error', error msg attached.
                             Caller decides whether to retry.
  - HTTP 5xx / network     → status='error'. Same.
  - Network timeout (>5s)  → status='error'. Don't block enrichment
                             for one stuck call.

Cost discipline
---------------
Never call this from a loop unprotected. Caller (orchestrator) is
responsible for de-duping per address and skipping when the local
pre-check has already returned a verdict.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("crm.scraper.email_verifier_reoon")

API_URL = "https://emailverifier.reoon.com/api/v1/verify"
HTTP_TIMEOUT = 5.0
ENV_KEY = "REOON_API_KEY"

# Reoon status → our normalised status. Anything we don't recognise
# falls through to 'unknown' so we don't accidentally mark something
# as valid based on an unfamiliar response value.
_STATUS_MAP = {
    "safe":       "valid",
    "valid":      "valid",
    "invalid":    "invalid",
    "disabled":   "invalid",
    "catch_all":  "catch_all",
    "catchall":   "catch_all",
    "catch-all":  "catch_all",
    "risky":      "risky",
    "unknown":    "unknown",
    "disposable": "disposable",
    "role":       "risky",  # role addresses are not great cold-outreach targets
}


def _api_key() -> str | None:
    """Return the API key from env, or None when unset. We don't
    cache because we want to pick up runtime env-file edits without
    a worker restart (e.g. when the operator rotates the key)."""
    key = (os.environ.get(ENV_KEY) or "").strip()
    return key or None


def is_available() -> bool:
    """True iff REOON_API_KEY is set. Caller can short-circuit to
    'not_configured' before paying the import / lookup cost."""
    return _api_key() is not None


def verify(email: str) -> dict:
    """Verify one email via Reoon's power-mode endpoint.

    Returns:
      {
        'status':     'valid' | 'invalid' | 'catch_all' | 'risky' |
                      'unknown' | 'disposable' |
                      'not_configured' | 'error',
        'score':      int 0-100  (None when not returned)
        'is_catchall': bool | None
        'is_role':    bool | None
        'raw':        original Reoon response (for debugging — don't
                      persist this; just for source_errors[])
        'error':      short error string when status == 'error'
      }
    Never raises.
    """
    out: dict = {"status": "not_configured", "score": None,
                 "is_catchall": None, "is_role": None,
                 "raw": None, "error": None}

    key = _api_key()
    if not key:
        out["error"] = "REOON_API_KEY not set"
        return out

    if not email or "@" not in email:
        out["status"] = "invalid"
        out["error"] = "no @ in address"
        return out

    params = urllib.parse.urlencode({
        "email": email.strip().lower(),
        "key":   key,
        "mode":  "power",
    })
    url = f"{API_URL}?{params}"

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        out["status"] = "error"
        out["error"]  = f"HTTP {e.code} from Reoon"
        # 401/403 nearly always means a bad key — surface clearly so the
        # operator notices in source_errors rather than mistaking it
        # for a transient failure.
        if e.code in (401, 403):
            out["error"] = f"HTTP {e.code} — check REOON_API_KEY"
        return out
    except urllib.error.URLError as e:
        out["status"] = "error"
        out["error"]  = f"network: {str(e.reason)[:80]}"
        return out
    except TimeoutError:
        out["status"] = "error"
        out["error"]  = f"timeout after {HTTP_TIMEOUT}s"
        return out
    except Exception as e:
        # Last-resort guard — never let a verifier hiccup blow up the
        # enrichment chain. Log and continue.
        logger.exception("reoon verify crashed for %s", email[:80])
        out["status"] = "error"
        out["error"]  = f"unexpected: {str(e)[:80]}"
        return out

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        out["status"] = "error"
        out["error"]  = "non-JSON response"
        return out
    out["raw"] = data

    raw_status = (data.get("status") or "").lower().strip()
    out["status"] = _STATUS_MAP.get(raw_status, "unknown")

    # Reoon returns the score as overall_score (0-100). Some endpoint
    # variants also surface 'score' or 'confidence'. Take whichever is
    # present.
    for k in ("overall_score", "score", "confidence"):
        v = data.get(k)
        if isinstance(v, (int, float)):
            out["score"] = int(v)
            break

    # Bool flags are useful even when status is 'valid' — a role
    # account that verified can still be a poor outreach target.
    out["is_catchall"] = bool(data.get("is_catchall") or data.get("is_catch_all"))
    out["is_role"]     = bool(data.get("is_role_account") or data.get("is_role"))

    return out
