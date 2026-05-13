"""Reply Engine — the tick.

Polls IMAP across all healthy mailboxes. For each new message:
  - DSN → bounce.py
  - Reply → matcher.py → sentiment.py → persist + cancel cadence

Modes
-----
REPLY_MODE=dry_run (default) — no IMAP. The tick is a no-op so the box
runs without Zoho creds. Useful while we're in dry-run outreach too.

REPLY_MODE=live — opens IMAP per mailbox using the same env-var-named
password convention as outreach (mailbox.smtp_pass_env_name is reused
since Zoho gives one app password for both SMTP + IMAP).

UID cursoring
-------------
Each mailbox has a row in crm.mailbox_state. last_uid is the highest
UID we've processed. On tick, FETCH UID (last_uid+1):* — empty result
on quiet days is the common case.

UID-VALIDITY change resets last_uid to 0 and we re-fetch everything;
acceptable for a 1-operator agency, the alternative (per-mailbox UID-V
tracking) buys nothing at this scale.

Single-process IMAP
-------------------
Connections are serial across mailboxes. With 21 mailboxes at 1s per
mailbox average, a full tick is under 30s — well inside the 10-min
cadence. Parallel later when soak shows the serial loop is a bottleneck.

Idempotency
-----------
Same message processed twice is a no-op: UID cursor prevents most
re-fetches; in the rare case a server replays UIDs after a UIDVALIDITY
change, the duplicate reply row is harmless audit noise rather than a
business-state bug.
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import os
from email.message import EmailMessage
from email.utils import parseaddr

import db
from reply import matcher, sentiment, bounce

logger = logging.getLogger("crm.reply.engine")

TICK_INTERVAL_S = 600  # 10 minutes between ticks.


def _load_mailboxes() -> list[dict]:
    """Active mailboxes — healthy or warming, not paused or disconnected."""
    return db.fetch_all("""
        select m.id, m.address, m.imap_host, m.imap_port,
               m.smtp_user, m.smtp_pass_env_name,
               coalesce(s.last_uid, 0) as last_uid
        from crm.mailboxes m
        left join crm.mailbox_state s on s.mailbox_id = m.id
        where m.paused = false
          and m.health_state in ('healthy','warming')
    """)


def _save_uid(mailbox_id: int, last_uid: int) -> None:
    db.execute(
        """insert into crm.mailbox_state (mailbox_id, last_uid, last_polled_at)
           values (%s, %s, now())
           on conflict (mailbox_id) do update
           set last_uid = excluded.last_uid,
               last_polled_at = excluded.last_polled_at,
               last_error = null,
               last_error_at = null""",
        (mailbox_id, last_uid),
    )


def _save_error(mailbox_id: int, err: str) -> None:
    db.execute(
        """insert into crm.mailbox_state (mailbox_id, last_uid, last_polled_at, last_error, last_error_at)
           values (%s, 0, now(), %s, now())
           on conflict (mailbox_id) do update
           set last_polled_at = now(),
               last_error = excluded.last_error,
               last_error_at = excluded.last_error_at""",
        (mailbox_id, err[:500]),
    )


def _headers_dict(msg: EmailMessage) -> dict:
    """Slim headers dict for the matcher."""
    out = {}
    for key in ("Message-ID", "In-Reply-To", "References", "From", "Subject", "Content-Type"):
        v = msg.get(key)
        if v is not None:
            out[key] = v
    return out


def _body_text(msg: EmailMessage) -> str:
    """Best-effort plaintext body. Walks multipart, prefers text/plain."""
    if not msg.is_multipart():
        try:
            payload = msg.get_payload(decode=True)
            if isinstance(payload, bytes):
                return payload.decode(msg.get_content_charset("utf-8"), errors="replace")
        except Exception:
            return ""
        return msg.get_payload() or ""

    plaintext = []
    html_fallback = []
    for part in msg.walk():
        ctype = (part.get_content_type() or "").lower()
        if part.is_multipart():
            continue
        try:
            payload = part.get_payload(decode=True)
            text = payload.decode(part.get_content_charset("utf-8"), errors="replace") if isinstance(payload, bytes) else str(payload or "")
        except Exception:
            continue
        if ctype == "text/plain":
            plaintext.append(text)
        elif ctype == "text/html":
            html_fallback.append(text)
    if plaintext:
        return "\n".join(plaintext)
    if html_fallback:
        # Crude tag strip — fine for sentiment classification.
        import re as _re
        return _re.sub(r"<[^>]+>", " ", "\n".join(html_fallback))
    return ""


def _persist_reply(matched: dict, msg: EmailMessage, body: str, sentiment_tag: str) -> None:
    """Insert into crm.replies + flip lead status + cancel queued sends."""
    from_addr = parseaddr(msg.get("From") or "")[1] or None
    subject = msg.get("Subject") or ""

    db.execute(
        """insert into crm.replies
             (lead_id, email_id, from_address, subject, body, sentiment, detected_at)
           values (%s, %s, %s, %s, %s, %s, now())""",
        (matched["lead_id"], matched["email_id"], from_addr,
         subject[:500], body[:8000], sentiment_tag),
    )

    # Flip the originating email to mark it replied to.
    if matched.get("email_id"):
        db.execute(
            "update crm.emails set replied_at = now() where id = %s",
            (matched["email_id"],),
        )

    # OOO replies should NOT change lead status or cancel the cadence —
    # the lead is still active; they just acknowledged via autoresponder.
    if sentiment_tag == "ooo":
        db.execute(
            "insert into crm.activity_log (client_id, lead_id, action, detail) "
            "values (%s, %s, 'auto_responder_skipped', %s)",
            (matched.get("client_id"), matched["lead_id"], f"ooo from {from_addr}"),
        )
        return

    # Negative → suppress the lead's email so we don't re-cadence accidentally.
    if sentiment_tag == "negative":
        try:
            row = db.fetch_one(
                "select decision_maker_email from crm.leads where id = %s",
                (matched["lead_id"],),
            )
            email_addr = row.get("decision_maker_email") if row else None
            if email_addr:
                db.suppress_address(email_addr, reason="unsubscribe",
                                    added_by="system",
                                    detail=f"negative reply from {from_addr}")
        except Exception:
            logger.exception("auto-suppress on negative failed")

    # Positive / neutral / negative all set lead.status='replied' and
    # cancel queued sends (US-009).
    db.execute(
        "update crm.leads set status='replied' where id=%s and status != 'replied'",
        (matched["lead_id"],),
    )
    db.execute(
        """update crm.emails
           set status='cancelled', cancel_reason='reply_received'
           where lead_id = %s and status = 'scheduled'""",
        (matched["lead_id"],),
    )

    db.execute(
        "insert into crm.activity_log (client_id, lead_id, action, detail) "
        "values (%s, %s, 'reply_received', %s)",
        (matched.get("client_id"), matched["lead_id"],
         f"sentiment={sentiment_tag} from {from_addr}"),
    )


def _process_message(mailbox_id: int, msg: EmailMessage) -> str:
    """Dispatch one message. Returns the action taken for logging.

    'bounce' | 'replied' | 'orphan' | 'skipped'
    """
    # 1. DSN bypass — bounce path.
    if bounce.is_dsn(msg):
        parsed = bounce.parse(msg)
        if parsed:
            bounce.record_and_suppress(parsed, mailbox_id=mailbox_id)
            return "bounce"
        return "skipped"

    # 2. Reply path.
    headers = _headers_dict(msg)
    from_addr = parseaddr(msg.get("From") or "")[1] or None
    matched = matcher.find_email_for_inbound(headers, from_addr)

    body = _body_text(msg)

    if not matched:
        # Unmatched — write to replies with lead_id=null so it surfaces
        # in Inbox > Needs you for manual triage.
        sentiment_tag = sentiment.classify(body, msg.get("Subject"))
        db.execute(
            """insert into crm.replies
                 (lead_id, email_id, from_address, subject, body, sentiment, detected_at)
               values (null, null, %s, %s, %s, %s, now())""",
            (from_addr, (msg.get("Subject") or "")[:500], body[:8000], sentiment_tag),
        )
        return "orphan"

    sentiment_tag = sentiment.classify(body, msg.get("Subject"))
    _persist_reply(matched, msg, body, sentiment_tag)
    return "replied"


def _poll_mailbox(mb: dict) -> dict:
    """Open IMAP, fetch new messages, dispatch each. Returns a counts dict."""
    env_name = mb.get("smtp_pass_env_name")
    password = os.environ.get(env_name) if env_name else None
    if not password:
        return {"mailbox": mb["address"], "error": f"env var {env_name} unset"}

    counts = {"replied": 0, "bounce": 0, "orphan": 0, "skipped": 0, "errored": 0}
    new_max_uid = mb["last_uid"]

    try:
        with imaplib.IMAP4_SSL(mb["imap_host"], int(mb["imap_port"]),
                               timeout=20) as imap:
            imap.login(mb["smtp_user"], password)
            imap.select("INBOX", readonly=True)
            # Fetch UIDs strictly greater than last seen.
            since = (mb["last_uid"] + 1) if mb["last_uid"] else 1
            typ, data = imap.uid("search", None, f"UID {since}:*")
            if typ != "OK":
                return {"mailbox": mb["address"], "error": "IMAP search failed"}
            uids = data[0].split() if data and data[0] else []
            for raw_uid in uids:
                try:
                    uid = int(raw_uid)
                except ValueError:
                    continue
                if uid <= mb["last_uid"]:
                    # Some IMAP servers include the boundary UID when *: is used.
                    continue
                typ, fetched = imap.uid("fetch", str(uid), "(RFC822)")
                if typ != "OK" or not fetched or not fetched[0]:
                    counts["errored"] += 1
                    continue
                raw_bytes = fetched[0][1]
                if not isinstance(raw_bytes, (bytes, bytearray)):
                    counts["errored"] += 1
                    continue
                msg = email.message_from_bytes(raw_bytes,
                                               policy=email.policy.default)
                action = _process_message(mb["id"], msg)
                counts[action] = counts.get(action, 0) + 1
                if uid > new_max_uid:
                    new_max_uid = uid
    except Exception as e:
        logger.exception("Mailbox %s IMAP poll failed", mb["address"])
        _save_error(mb["id"], f"{type(e).__name__}: {e}")
        return {"mailbox": mb["address"], "error": str(e)[:200], "counts": counts}

    if new_max_uid > mb["last_uid"]:
        _save_uid(mb["id"], new_max_uid)
    else:
        # Touch last_polled_at even when nothing new.
        _save_uid(mb["id"], mb["last_uid"])

    return {"mailbox": mb["address"], "counts": counts, "last_uid": new_max_uid}


def tick(mode: str | None = None) -> dict:
    """Run one engine pass. Returns a per-mailbox summary."""
    if mode is None:
        mode = os.environ.get("REPLY_MODE", "dry_run")
    if mode != "live":
        return {"mode": mode, "skipped": "dry_run"}

    mailboxes = _load_mailboxes()
    results = []
    for mb in mailboxes:
        results.append(_poll_mailbox(mb))
    return {"mode": mode, "mailboxes": results}
