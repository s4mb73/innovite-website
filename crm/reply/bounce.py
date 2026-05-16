"""DSN (RFC 3464) bounce parser + auto-suppression.

A DSN is structured as multipart/report with three parts:
  1. human-readable explanation
  2. message/delivery-status — the structured fields (Status, Action,
     Original-Recipient, etc.)
  3. message/rfc822 — the original message that bounced

We extract:
  - bounced_address    — Original-Recipient or Final-Recipient
  - smtp_code          — Status (e.g. '5.1.1')
  - severity           — hard (5.x.x) or soft (4.x.x) or unknown
  - reason             — Diagnostic-Code if present, else the human part

Severity classification
-----------------------
RFC 3463 status codes:
  4.x.x  — persistent transient (soft)
  5.x.x  — permanent (hard)
  3.x.x and below — non-failure (we ignore)

Hard bounces:
  - Insert into crm.bounces (severity='hard')
  - Auto-add to crm.suppressed_addresses (reason='hard_bounce')
  - Flip the originating email (if found) status='bounced'

Soft bounces:
  - Insert into crm.bounces (severity='soft')
  - Retry counter handled by outreach engine on next attempt.
    For now (no retry counter column) we just log and let the next
    cadence run attempt naturally.
"""
from __future__ import annotations

import logging
import re
from email.message import EmailMessage

import db

logger = logging.getLogger("crm.reply.bounce")

_STATUS_RE = re.compile(r"^Status:\s*(\d\.\d+\.\d+)", re.MULTILINE | re.IGNORECASE)
_FINAL_RE  = re.compile(r"^Final-Recipient:\s*[^;]+;\s*([^\s]+)", re.MULTILINE | re.IGNORECASE)
_ORIG_RE   = re.compile(r"^Original-Recipient:\s*[^;]+;\s*([^\s]+)", re.MULTILINE | re.IGNORECASE)
_DIAG_RE   = re.compile(r"^Diagnostic-Code:\s*(.+)$", re.MULTILINE | re.IGNORECASE)

# Content-block keywords that often appear in the Diagnostic-Code when
# the bounce is the email template's fault rather than the address.
# Conservative list — false-positive on these costs us a suppression
# we shouldn't have made; false-negative just means the address still
# auto-suppresses, which is the existing behaviour.
_CONTENT_KEYWORDS = (
    "spam", "blocked content", "policy reason", "phishing", "blacklist",
    "blocklist", "url filter", "content filter", "message rejected",
    "suspicious", "deemed spam", "looks like spam",
)

# Per-category actions:
#   suppress=True  → auto-add to suppression list
#   ops_alert=True → log a warning (template/infra issue worth noticing)
_CATEGORY_RULES: dict[str, dict] = {
    "no_such_user":   {"suppress": True,  "ops_alert": False},
    "mailbox_full":   {"suppress": False, "ops_alert": False},  # transient, retry naturally
    "policy_block":   {"suppress": True,  "ops_alert": True},   # IP/domain reputation issue
    "content_block":  {"suppress": False, "ops_alert": True},   # template is the problem
    "auth_fail":      {"suppress": False, "ops_alert": True},   # SPF/DKIM broken — infra bug
    "greylist":       {"suppress": False, "ops_alert": False},
    "unknown":        {"suppress": False, "ops_alert": False},  # don't over-act on ambiguous DSNs
}


def _categorise(smtp_code: str | None, reason: str | None) -> str:
    """Map an SMTP enhanced-status code + diagnostic text to a category.

    Specific codes first; then content-keyword check; then generic
    severity buckets; then 'unknown'."""
    code = (smtp_code or "").strip()
    diag = (reason or "").lower()

    # Greylist family — first-attempt deferrals, retry naturally.
    if code in ("4.7.1", "4.4.7", "4.2.0"):
        return "greylist"

    # Mailbox-full family — transient, retry then suppress.
    if code in ("4.2.2", "5.2.2", "5.2.3"):
        return "mailbox_full"

    # Auth fail — SPF / DKIM / DMARC alignment problems.
    if code in ("5.7.0", "5.7.7", "5.7.8", "5.7.9", "5.7.20", "5.7.21",
                "5.7.22", "5.7.23", "5.7.24", "5.7.25", "5.7.26"):
        return "auth_fail"

    # Address-not-found family.
    if code in ("5.1.1", "5.1.2", "5.1.3", "5.1.6", "5.1.10"):
        return "no_such_user"

    # 5.7.x with content-related keywords → content block (template issue)
    # else → policy block (reputation issue).
    if code.startswith("5.7"):
        if any(kw in diag for kw in _CONTENT_KEYWORDS):
            return "content_block"
        return "policy_block"

    # Generic transient.
    if code.startswith("4"):
        return "greylist"

    return "unknown"


def is_dsn(msg: EmailMessage) -> bool:
    """Cheap test: multipart/report or delivery-status content type."""
    ctype = (msg.get_content_type() or "").lower()
    if ctype == "multipart/report":
        return True
    # Some servers send delivery-status as a top-level part without the
    # multipart/report wrapper. Check for the diagnostic header signal.
    for part in msg.walk():
        if (part.get_content_type() or "").lower() == "message/delivery-status":
            return True
    return False


def _extract_delivery_status_text(msg: EmailMessage) -> str:
    """Get the structured delivery-status section as raw text."""
    for part in msg.walk():
        if (part.get_content_type() or "").lower() == "message/delivery-status":
            try:
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode("utf-8", errors="replace")
                if isinstance(payload, str):
                    return payload
            except Exception:
                return ""
    # Fallback: scan the whole body for Status: header.
    try:
        return msg.as_string()[:4000]
    except Exception:
        return ""


def parse(msg: EmailMessage) -> dict | None:
    """Parse a DSN into {bounced_address, smtp_code, severity, reason, raw}.

    Returns None if the message looks like a DSN but parsing failed.
    """
    text = _extract_delivery_status_text(msg)
    if not text:
        return None

    addr_match = _ORIG_RE.search(text) or _FINAL_RE.search(text)
    if not addr_match:
        return None
    address = addr_match.group(1).strip().strip("<>").lower()

    status_match = _STATUS_RE.search(text)
    smtp_code = status_match.group(1) if status_match else None

    severity = "unknown"
    if smtp_code:
        if smtp_code.startswith("5"):
            severity = "hard"
        elif smtp_code.startswith("4"):
            severity = "soft"

    diag_match = _DIAG_RE.search(text)
    reason = diag_match.group(1).strip()[:200] if diag_match else None

    return {
        "bounced_address": address,
        "smtp_code":       smtp_code,
        "severity":        severity,
        "category":        _categorise(smtp_code, reason),
        "reason":          reason,
        "raw":             text[:4000],
    }


def record_and_suppress(parsed: dict, mailbox_id: int | None) -> None:
    """Insert into crm.bounces + auto-suppress if hard.

    Best-effort: failures are logged, never raised. The poller should
    continue even if a single bounce write fails.
    """
    addr = parsed["bounced_address"]

    # Try to attach to the originating email for traceability.
    email_row = db.fetch_one(
        "select id, lead_id from crm.emails "
        "where lower(to_address) = lower(%s) and status in ('sent','dry_run_ready') "
        "order by sent_at desc nulls last limit 1",
        (addr,),
    )
    email_id = email_row["id"] if email_row else None
    lead_id = email_row["lead_id"] if email_row else None

    category = parsed.get("category") or "unknown"
    rules = _CATEGORY_RULES.get(category, _CATEGORY_RULES["unknown"])

    try:
        db.execute(
            """insert into crm.bounces
                 (email_id, lead_id, mailbox_id, bounced_address,
                  severity, smtp_code, category, reason, raw)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (email_id, lead_id, mailbox_id, addr,
             parsed["severity"], parsed["smtp_code"], category,
             parsed["reason"], parsed["raw"]),
        )
    except Exception:
        logger.exception("Failed to insert bounce row for %s", addr)

    if email_id:
        try:
            db.execute(
                "update crm.emails set status='bounced' where id=%s",
                (email_id,),
            )
        except Exception:
            logger.exception("Failed to flip email %s to bounced", email_id)

    # Category-specific actions. Content_block / auth_fail intentionally
    # do NOT suppress — those bounces are about our template or our DNS,
    # not the recipient's address.
    if rules.get("suppress"):
        try:
            db.suppress_address(
                addr,
                reason="hard_bounce" if category != "policy_block" else "complaint",
                added_by="system",
                detail=f"SMTP {parsed.get('smtp_code') or '?'} ({category})",
            )
        except Exception:
            logger.exception("Failed to auto-suppress bounce for %s", addr)

    if rules.get("ops_alert"):
        # WARN-level log so it surfaces in journalctl / log aggregation
        # without being lost in the INFO firehose.
        logger.warning(
            "BOUNCE category=%s addr=%s code=%s mailbox_id=%s reason=%s",
            category, addr, parsed.get("smtp_code"), mailbox_id, parsed.get("reason"),
        )
