"""Outreach Engine — gate policies.

Each gate is a function that returns (ok: bool, reason: str | None).
The engine runs them in a fixed order; the first failing gate is
recorded as the reason on the outreach_actions row.

Reason constants are documented here in one place so the audit table
is self-describing and queries can group by them.
"""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")

# Reason codes for outreach_actions.reason. Stable strings — used in
# UI groupings, log filters, and reporting.
REASON_SYSTEM_PAUSED       = "system_paused"
REASON_OUT_OF_HOURS        = "out_of_hours"
REASON_WEEKEND_SKIPPED     = "weekend_skipped"
REASON_CLIENT_PAUSED       = "client_paused"
REASON_LEAD_REPLIED        = "lead_replied"
REASON_SUPPRESSED_ADDRESS  = "suppressed_address"
REASON_NO_MAILBOX_CAPACITY = "no_mailbox_capacity"
REASON_MAILBOX_JITTER      = "mailbox_jitter"
REASON_CADENCE_DISABLED    = "cadence_disabled"


def check_system_pause(settings: dict) -> tuple[bool, str | None]:
    """settings: the full crm.settings k/v dict (key → value)."""
    if settings.get("system_outreach_paused") is True:
        return False, REASON_SYSTEM_PAUSED
    return True, None


def check_sending_hours(settings: dict, now: datetime | None = None) -> tuple[bool, str | None]:
    """Honour the configured sending window in Europe/London.

    settings.sending_hours = {start, end, tz, skip_weekends}.
    Out-of-hours sends roll forward to the next valid window (not an error).
    """
    hours = settings.get("sending_hours") or {}
    tz_name = hours.get("tz") or "Europe/London"
    tz = ZoneInfo(tz_name) if tz_name != "Europe/London" else LONDON
    if now is None:
        now = datetime.now(tz)
    else:
        now = now.astimezone(tz)

    if hours.get("skip_weekends") and now.weekday() >= 5:
        return False, REASON_WEEKEND_SKIPPED

    start_s = hours.get("start") or "09:00"
    end_s = hours.get("end") or "17:00"
    try:
        sh, sm = (int(x) for x in start_s.split(":"))
        eh, em = (int(x) for x in end_s.split(":"))
    except (ValueError, TypeError):
        sh, sm, eh, em = 9, 0, 17, 0

    t = now.time()
    if t < time(sh, sm) or t >= time(eh, em):
        return False, REASON_OUT_OF_HOURS
    return True, None


def check_client_pause(client_row: dict) -> tuple[bool, str | None]:
    """client_row: a crm.clients row dict including outreach_paused."""
    if client_row.get("outreach_paused") is True:
        return False, REASON_CLIENT_PAUSED
    return True, None


def check_lead_not_replied(lead_status: str | None, replied_at) -> tuple[bool, str | None]:
    """Lead status of 'replied' or a populated replied_at means we stop.

    Belt-and-braces: the Reply Engine sets lead.status='replied' AND
    flips the email row. We check both because a follow-up may have
    been queued between detection and cancellation.
    """
    if lead_status == "replied":
        return False, REASON_LEAD_REPLIED
    if replied_at is not None:
        return False, REASON_LEAD_REPLIED
    return True, None


def check_address_not_suppressed(to_address: str, suppressed: set[str]) -> tuple[bool, str | None]:
    """Suppressed addresses are blocked across all clients (US-007)."""
    if not to_address:
        return True, None  # No address to check; engine will skip on another gate.
    if to_address.lower() in suppressed:
        return False, REASON_SUPPRESSED_ADDRESS
    return True, None


def check_cadence_enabled(settings: dict, email_number: int) -> tuple[bool, str | None]:
    """Cadence toggles on settings (day1_enabled, etc.) gate scheduling AND sending.

    Disabling Day 7 mid-flight means any queued Day-7 stays scheduled but
    never sends — the engine cancels it with reason 'cadence_disabled' on
    its tick.
    """
    cad = settings.get("cadence") or {}
    key = f"day{email_number}_enabled"
    if cad.get(key) is False:
        return False, REASON_CADENCE_DISABLED
    return True, None
