"""Innovite CRM — Reply Engine (Service 4).

The 10-minute cron loop that watches active mailboxes' IMAP inboxes,
matches inbound to outbound, classifies sentiment, cancels the cadence
on positive/neutral replies, and routes DSN messages to the bounce
handler.

Why one engine for both replies and bounces
-------------------------------------------
DSNs arrive on the same IMAP connection as real replies. Running two
poll loops doubles the IMAP connection cost. The per-message router
branches on `Content-Type: multipart/report` first; same connection,
two handlers.

Modes
-----
REPLY_MODE=dry_run (default):
  - Worker skips IMAP entirely. The reply engine is a no-op so the
    box can be cold-bootstrapped without Zoho creds.
REPLY_MODE=live:
  - Opens IMAP per mailbox, fetches UIDs newer than last seen, processes
    each. Failures are logged per-mailbox and don't poison the loop.

Modules
-------
- matcher.py    — header-based outbound match (Message-ID / In-Reply-To)
- sentiment.py  — Anthropic Haiku classifier (positive/neutral/negative/ooo)
- bounce.py     — RFC 3464 DSN parser + auto-suppress hard bounces
- engine.py     — IMAP poller + tick(), uses the three above
"""
