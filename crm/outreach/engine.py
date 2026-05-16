"""Outreach Engine — the tick.

One pass through the scheduled-emails queue. Picks a mailbox per email,
applies all gates, either marks the email shipped (live mode) / dry-run
(default) / skipped / cancelled and writes a row to crm.outreach_actions.

Modes
-----
dry_run (default):
  - status flip: scheduled → dry_run_ready
  - no SMTP, no IMAP, no real email
  - mailbox.sent_today still increments and last_send_at still updates so
    the rotation logic is exercised end-to-end
  - cadence auto-schedules (Day-3 / Day-7 rows are written) so the engine
    can run a full week against itself before going live

live:
  - status flip: scheduled → sent (or bounced / failed)
  - real SMTP send via the mailbox's creds
  - bounces handled by Reply Engine (separate service)

Switch via env: OUTREACH_MODE=live (anything else → dry_run).

Gate ordering
-------------
1. system_outreach_paused        — settings
2. sending_hours / weekend       — settings
3. cadence day-N enabled         — settings (gates by email_number)
4. client outreach_paused        — client row
5. lead replied                  — lead row / replied_at
6. to_address suppressed         — suppressed_addresses table
7. mailbox capacity              — mailbox row (sent_today < daily_cap, not paused)
8. mailbox 2-minute jitter       — mailbox.last_send_at
"""
from __future__ import annotations

import json
import logging
import os
import random
import smtplib
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Iterable

from itsdangerous import URLSafeSerializer

import db
from outreach import policy
from pipeline import drafter

logger = logging.getLogger("crm.outreach.engine")

# Random uniform per-mailbox cool-down between sends. Fixed intervals
# (e.g. always 120s) are a known bot signature for ESP spam filters;
# a random window mimics human-ish pacing without affecting throughput.
JITTER_MIN_SECONDS = 90
JITTER_MAX_SECONDS = 240

TICK_INTERVAL_S = 300  # 5 minutes between ticks (throttled by the worker loop).

# List-Unsubscribe (RFC 8058) — required by Gmail/Yahoo Feb-2024 sender
# rules. Token-signed so the endpoint can verify the request came from
# our own email without a DB lookup. UNSUBSCRIBE_BASE_URL is env-overridable
# for staging / local testing.
UNSUBSCRIBE_BASE_URL = os.environ.get(
    "UNSUBSCRIBE_BASE_URL", "https://app.innovite.io/unsubscribe"
)


def _jitter_cutoff(now: datetime) -> datetime:
    """Random per-call mailbox-eligibility cutoff in the jitter window.

    A mailbox is eligible if its last_send_at is older than `cutoff`.
    Re-rolling the random per call means consecutive picks in the same
    tick see slightly different windows — the effective inter-send
    interval per mailbox is uniform across [JITTER_MIN, JITTER_MAX]."""
    return now - timedelta(
        seconds=random.randint(JITTER_MIN_SECONDS, JITTER_MAX_SECONDS)
    )


def _unsubscribe_url(email_id: int, address: str) -> str:
    """Signed one-click unsubscribe URL for the List-Unsubscribe header.

    Token is itsdangerous-signed so the endpoint can verify provenance
    without storing per-email unsubscribe rows. If FLASK_SECRET_KEY
    rotates, outstanding links go invalid — acceptable since the
    operator can always suppress the address manually."""
    secret = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-in-prod")
    ser = URLSafeSerializer(secret, salt="unsubscribe-v1")
    token = ser.dumps({"e": int(email_id), "a": address})
    return f"{UNSUBSCRIBE_BASE_URL}?t={token}"


# ── Settings + suppression load ──────────────────────────────────────
def _load_settings() -> dict:
    rows = db.fetch_all("select key, value from crm.settings")
    return {r["key"]: r["value"] for r in rows}


def _load_suppressed() -> set[str]:
    """All hard-suppressed addresses, lower-cased.

    Reads crm.suppressed_addresses if it exists; treated as empty if not.
    The table lands in step 9 (this migration); engine survives without it.
    """
    try:
        rows = db.fetch_all("select address from crm.suppressed_addresses")
    except Exception:
        return set()
    return {(r.get("address") or "").lower() for r in rows if r.get("address")}


# ── Mailbox selection ───────────────────────────────────────────────
def _pick_mailbox(client_id: int, lead_id: int | None = None) -> dict | None:
    """Return the best mailbox for this send, or None if nothing fits.

    Rules, in order:
    0. **Stickiness** — if the lead has a preferred_mailbox_id set (Day 1
       went out from it), keep using it. Otherwise threading breaks: the
       recipient sees two unrelated cold-touch emails from different
       senders instead of a follow-up to the existing thread.
    1. Dedicated mailbox for this client if one has capacity.
    2. Pool — any healthy/warming mailbox with capacity, ordered by
       remaining headroom so the most-rested mailbox goes first.
    3. Excludes paused, disconnected, or capacity-exhausted mailboxes.
    4. Respects the randomised per-mailbox jitter window.
    """
    now = datetime.now(timezone.utc)
    cutoff = _jitter_cutoff(now)

    base_cols = """
        select id, address, smtp_host, smtp_port, smtp_user, smtp_pass_env_name,
               from_name, daily_cap, sent_today, last_send_at, dedicated_client_id
        from crm.mailboxes
        where paused = false
          and health_state in ('healthy','warming')
          and sent_today < daily_cap
          and (last_send_at is null or last_send_at < %s)
    """

    # 0. Stickiness: lead's locked mailbox, if it still meets the gates.
    if lead_id is not None:
        sticky = db.fetch_all(
            base_cols + " and id = (select preferred_mailbox_id from crm.leads where id = %s) limit 1",
            (cutoff, lead_id),
        )
        if sticky:
            return sticky[0]

    # 1. Dedicated.
    dedicated = db.fetch_all(
        base_cols + " and dedicated_client_id = %s order by (daily_cap - sent_today) desc limit 1",
        (cutoff, client_id),
    )
    if dedicated:
        return dedicated[0]

    # 2. Pool fallback: any mailbox not dedicated to a different client.
    pool = db.fetch_all(
        base_cols + " and (dedicated_client_id is null or dedicated_client_id = %s) "
        "order by (daily_cap - sent_today) desc limit 1",
        (cutoff, client_id),
    )
    return pool[0] if pool else None


def _save_lead_preferred_mailbox(lead_id: int, mailbox_id: int) -> None:
    """Lock the lead to this mailbox so Day 3 and Day 7 ship from the
    same address (thread integrity). Only sets if currently null — once
    Day 1's mailbox is locked in, the lead stays on it for life."""
    db.execute(
        "update crm.leads set preferred_mailbox_id = %s "
        "where id = %s and preferred_mailbox_id is null",
        (mailbox_id, lead_id),
    )


def _any_mailbox_paused_by_jitter(client_id: int) -> bool:
    """True if a mailbox WOULD have been picked but is still in the
    jitter window. Lets us record reason='mailbox_jitter' instead of
    the more generic 'no_mailbox_capacity'."""
    now = datetime.now(timezone.utc)
    cutoff = _jitter_cutoff(now)
    rows = db.fetch_all("""
        select 1
        from crm.mailboxes
        where paused = false
          and health_state in ('healthy','warming')
          and sent_today < daily_cap
          and (dedicated_client_id is null or dedicated_client_id = %s)
          and last_send_at is not null
          and last_send_at >= %s
        limit 1
    """, (client_id, cutoff))
    return len(rows) > 0


# ── Audit / status helpers ──────────────────────────────────────────
def _audit(email_id: int | None, lead_id: int | None, client_id: int | None,
           mailbox_id: int | None, action: str, reason: str | None, mode: str) -> None:
    db.execute(
        """insert into crm.outreach_actions
             (email_id, lead_id, client_id, mailbox_id, action, reason, mode)
           values (%s, %s, %s, %s, %s, %s, %s)""",
        (email_id, lead_id, client_id, mailbox_id, action, reason, mode),
    )


def _cancel(email_id: int, reason: str) -> None:
    db.execute(
        "update crm.emails set status='cancelled', cancel_reason=%s where id=%s",
        (reason, email_id),
    )


# ── Cadence (also covers US-005 step 10) ────────────────────────────
def _next_step_at(after: datetime, step: int, settings: dict) -> datetime:
    """When should the next step be scheduled?

    Day-1 → Day-3: +3 days (skip weekends if configured to next Monday).
    Day-3 → Day-7: +4 days (same rule).
    """
    cad = settings.get("cadence") or {}
    skip_weekends = bool(cad.get("skip_weekends", True))
    delta_days = 3 if step == 2 else 4
    target = after + timedelta(days=delta_days)
    if skip_weekends:
        while target.weekday() >= 5:
            target += timedelta(days=1)
    return target


def _maybe_schedule_followup(email_row: dict, settings: dict, mode: str) -> None:
    """After a Day-1 sends, write the Day-3 row. After Day-3, Day-7.

    Body + subject are generated by drafter.draft_followup at this point —
    the lead context is fresh and the parent thread is in hand. This is
    deliberate (rather than generating at send-time on the follow-up day):
    if Anthropic is briefly down on a Wednesday, we still have a clean
    templated fallback for the row queued on Monday.

    Skips when the cadence toggle for the next step is disabled.
    """
    current = int(email_row["email_number"])
    if current >= 3:
        return
    next_step = current + 1

    cad = settings.get("cadence") or {}
    if cad.get(f"day{next_step}_enabled") is False:
        return

    next_at = _next_step_at(datetime.now(timezone.utc), next_step, settings)

    parent_subject = email_row.get("subject") or ""
    parent_body = email_row.get("body") or ""

    # Pull a minimal "business" dict from the lead for the drafter. We do
    # not refetch the full enriched profile because the drafter only needs
    # name + city + decision_maker for follow-up tone — and the lead row
    # already has those.
    lead = db.fetch_one(
        "select business_name, city, decision_maker_name "
        "from crm.leads where id = %s",
        (email_row["lead_id"],),
    ) or {}

    draft = drafter.draft_followup(
        {
            "business_name":       lead.get("business_name") or "",
            "city":                lead.get("city") or "",
            "decision_maker_name": lead.get("decision_maker_name") or "",
        },
        parent_subject,
        parent_body,
        next_step,
    )

    # Follow-ups inherit approval from the thread — the Day-1 cold touch
    # already passed the operator's gate, so Day-3 / Day-7 ship without
    # a second click. (See /approvals — only Day-1 goes through the gate.)
    db.execute(
        """insert into crm.emails
             (lead_id, client_id, email_number, subject, body, to_address,
              status, scheduled_at, kind, needs_approval, approved_at, approved_by)
           values (%s, %s, %s, %s, %s, %s, 'scheduled', %s, 'cadence',
                   false, now(), 'cadence_inherit')""",
        (
            email_row["lead_id"],
            email_row["client_id"],
            next_step,
            draft["subject"],
            draft["body"],
            email_row.get("to_address"),
            next_at,
        ),
    )


# ── The tick ────────────────────────────────────────────────────────
def _ready_emails(limit: int = 50) -> list[dict]:
    """Pick up to N scheduled emails ready to ship (oldest first).

    Joins lead + client for the gate inputs. lead.status='replied' is the
    canonical 'lead has replied' signal — Reply Engine sets it when an
    inbound match lands.

    needs_approval=true rows are filtered here (not in policy) because
    they are explicitly *not ready* — they're sitting in /approvals
    waiting on the operator. They'll surface here on the next tick after
    approval, with no further gate logic required.
    """
    return db.fetch_all("""
        select e.id, e.lead_id, e.client_id, e.email_number, e.subject, e.body,
               e.to_address, e.scheduled_at, e.kind,
               l.status as lead_status,
               c.outreach_paused as client_paused
        from crm.emails e
        join crm.leads l on l.id = e.lead_id
        join crm.clients c on c.id = e.client_id
        where e.status = 'scheduled'
          and e.needs_approval = false
          and (e.scheduled_at is null or e.scheduled_at <= now())
        order by coalesce(e.scheduled_at, e.created_at)
        limit %s
    """, (limit,))


def _mark_sent(email_id: int, mailbox_id: int, message_id: str | None, mode: str) -> None:
    """Flip the email row. dry_run_ready in dry mode; sent in live."""
    new_status = "sent" if mode == "live" else "dry_run_ready"
    db.execute(
        """update crm.emails
           set status = %s,
               sent_at = now(),
               from_address = (select address from crm.mailboxes where id = %s),
               message_id = coalesce(%s, message_id)
           where id = %s""",
        (new_status, mailbox_id, message_id, email_id),
    )
    db.execute(
        """update crm.mailboxes
           set sent_today = sent_today + 1,
               last_send_at = now()
           where id = %s""",
        (mailbox_id,),
    )


def _send_live(mailbox: dict, email_row: dict, message_id: str) -> bool:
    """Actually open SMTP and send. Returns True on success.

    SMTP password comes from the env var named on the mailbox row. If the
    var is missing we abort with a log message rather than guessing —
    sending the wrong message to the wrong inbox is irreversible.
    """
    env_name = mailbox.get("smtp_pass_env_name")
    if not env_name:
        logger.error("Mailbox %s has no smtp_pass_env_name", mailbox["id"])
        return False
    password = os.environ.get(env_name)
    if not password:
        logger.error("Mailbox %s env var %s is unset", mailbox["id"], env_name)
        return False

    msg = EmailMessage()
    msg["From"] = f'{mailbox.get("from_name") or ""} <{mailbox["address"]}>'.strip()
    msg["To"] = email_row["to_address"]
    msg["Subject"] = email_row.get("subject") or ""
    msg["Message-ID"] = message_id
    # RFC 8058 one-click unsubscribe — required by Gmail/Yahoo for inbox
    # placement since Feb 2024. The signed token encodes the email id +
    # recipient so /unsubscribe can verify without a DB lookup.
    unsub_url = _unsubscribe_url(email_row["id"], email_row["to_address"])
    msg["List-Unsubscribe"] = f"<{unsub_url}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(email_row.get("body") or "")

    try:
        with smtplib.SMTP(mailbox["smtp_host"], mailbox["smtp_port"], timeout=20) as server:
            server.starttls()
            server.login(mailbox["smtp_user"], password)
            server.send_message(msg)
        return True
    except Exception:
        logger.exception("SMTP send failed for email %s via mailbox %s",
                         email_row["id"], mailbox["id"])
        return False


def tick(mode: str | None = None) -> dict:
    """Run one engine pass. Returns counts for logging.

    mode: 'dry_run' (default) | 'live'. Falls back to env OUTREACH_MODE.
    """
    if mode is None:
        mode = os.environ.get("OUTREACH_MODE", "dry_run")
    if mode not in ("dry_run", "live"):
        mode = "dry_run"

    settings = _load_settings()

    # System-wide gates short-circuit the whole tick.
    ok, reason = policy.check_system_pause(settings)
    if not ok:
        return {"counts": {"skipped_system": 1}, "reason": reason, "mode": mode}

    ok, reason = policy.check_sending_hours(settings)
    if not ok:
        return {"counts": {"skipped_hours": 1}, "reason": reason, "mode": mode}

    suppressed = _load_suppressed()

    counts = {"sent": 0, "would_send": 0, "skipped": 0, "cancelled": 0}
    emails = _ready_emails(limit=100)

    for e in emails:
        # Cadence step disabled?
        ok, reason = policy.check_cadence_enabled(settings, int(e["email_number"]))
        if not ok:
            _cancel(e["id"], reason)
            _audit(e["id"], e["lead_id"], e["client_id"], None, "cancelled", reason, mode)
            counts["cancelled"] += 1
            continue

        # Lead replied?
        ok, reason = policy.check_lead_not_replied(e.get("lead_status"), None)
        if not ok:
            _cancel(e["id"], reason)
            _audit(e["id"], e["lead_id"], e["client_id"], None, "cancelled", reason, mode)
            counts["cancelled"] += 1
            continue

        # Address suppressed?
        ok, reason = policy.check_address_not_suppressed(e.get("to_address") or "", suppressed)
        if not ok:
            _cancel(e["id"], reason)
            _audit(e["id"], e["lead_id"], e["client_id"], None, "cancelled", reason, mode)
            counts["cancelled"] += 1
            continue

        # Client paused? Leave scheduled; tick will retry next cycle.
        ok, reason = policy.check_client_pause({"outreach_paused": e.get("client_paused")})
        if not ok:
            _audit(e["id"], e["lead_id"], e["client_id"], None, "skipped", reason, mode)
            counts["skipped"] += 1
            continue

        # Pick a mailbox — honour stickiness on follow-ups so the lead
        # keeps seeing one continuous thread.
        mailbox = _pick_mailbox(e["client_id"], lead_id=e["lead_id"])
        if mailbox is None:
            # Differentiate "everyone's in jitter cooldown" vs "no capacity".
            reason = (policy.REASON_MAILBOX_JITTER
                      if _any_mailbox_paused_by_jitter(e["client_id"])
                      else policy.REASON_NO_MAILBOX_CAPACITY)
            _audit(e["id"], e["lead_id"], e["client_id"], None, "skipped", reason, mode)
            counts["skipped"] += 1
            continue

        # Ship it.
        message_id = f"<{uuid.uuid4()}@{mailbox['address'].split('@')[-1]}>"
        success = True
        if mode == "live":
            success = _send_live(mailbox, e, message_id)

        if success:
            _mark_sent(e["id"], mailbox["id"], message_id, mode)
            # Lock this lead to this mailbox for Day 3 + Day 7 — keeps
            # the recipient's thread intact across the cadence.
            _save_lead_preferred_mailbox(e["lead_id"], mailbox["id"])
            _maybe_schedule_followup(e, settings, mode)
            action = "sent" if mode == "live" else "would_send"
            _audit(e["id"], e["lead_id"], e["client_id"], mailbox["id"], action, None, mode)
            counts["sent" if mode == "live" else "would_send"] += 1
        else:
            db.execute("update crm.emails set status='failed' where id=%s", (e["id"],))
            _audit(e["id"], e["lead_id"], e["client_id"], mailbox["id"],
                   "skipped", "smtp_failed", mode)
            counts["skipped"] += 1

    return {"counts": counts, "mode": mode}
