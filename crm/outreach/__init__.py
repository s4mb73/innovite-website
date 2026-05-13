"""Innovite CRM — Outreach Engine (Service 3).

The 5-minute cron loop that turns crm.emails rows with status='scheduled'
into actual sends (or, in dry-run mode, into status='dry_run_ready'
plus an audit row).

Design pillars
--------------
1. Dry-run by default. OUTREACH_MODE env var = 'dry_run' | 'live'.
   Dry-run writes everything to crm.outreach_actions so we can verify
   rotation + caps + gates without burning a single email.

2. Single tick function with explicit gate ordering. Each gate is a
   pure function in policy.py that returns (ok: bool, reason: str).
   New gates slot in without touching the rest of the engine.

3. Cadence is part of the engine, not a separate cron. When a Day-1
   ships (dry-run or live), the Day-3 row is written inline; Day-3
   ships → Day-7 row. Auto-schedule + reply-cancellation + suppression
   all share one place to update the email's status.

Two modules
-----------
- policy.py — gate functions + reason constants.
- engine.py — tick(), mailbox pick, send dispatch, cadence chaining.
"""
