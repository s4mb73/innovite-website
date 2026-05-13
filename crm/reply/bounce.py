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

    try:
        db.execute(
            """insert into crm.bounces
                 (email_id, lead_id, mailbox_id, bounced_address, severity, smtp_code, reason, raw)
               values (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (email_id, lead_id, mailbox_id, addr,
             parsed["severity"], parsed["smtp_code"], parsed["reason"], parsed["raw"]),
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

    if parsed["severity"] == "hard":
        try:
            db.suppress_address(addr, reason="hard_bounce", added_by="system",
                                detail=f"SMTP {parsed.get('smtp_code') or '?'}")
        except Exception:
            logger.exception("Failed to auto-suppress hard bounce for %s", addr)
