"""Innovite CRM background worker.

Single long-running Python process. Today it just runs the pipeline
runner; Outreach (Service 3), Reply (Service 4), and Reporting (Service 5)
will be added as apscheduler jobs in the same process.

Polling strategy
----------------
Every 5 seconds, claim the oldest pending pipeline_runs row using:

    update crm.pipeline_runs set status='running'
    where id = (
      select id from crm.pipeline_runs
      where status='pending'
      order by created_at
      for update skip locked
      limit 1
    )
    returning id

SKIP LOCKED + UPDATE-RETURNING in one statement is the standard "claim
one job atomically" pattern. Safe under multiple workers (we will only
ever have one for the foreseeable future, but the pattern is cheap).

Health
------
The worker writes a heartbeat row every 30 seconds to a tiny state
table (deferred — for v1 we rely on systemd's Watchdog instead).

Run as: python -m worker
Systemd unit: /etc/systemd/system/crm-worker.service
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg

import db
from pipeline import runner
from outreach import engine as outreach_engine
from reply import engine as reply_engine

POLL_INTERVAL_S = int(os.environ.get("WORKER_POLL_INTERVAL_S", "5"))
OUTREACH_TICK_S = int(os.environ.get("WORKER_OUTREACH_TICK_S", "300"))
REPLY_TICK_S    = int(os.environ.get("WORKER_REPLY_TICK_S", "600"))
LONDON = ZoneInfo("Europe/London")

logger = logging.getLogger("crm.worker")


_running = True


def _shutdown(signum, frame):
    global _running
    logger.info("Received signal %s — finishing current job then exiting", signum)
    _running = False


def _claim_next_run() -> int | None:
    """Atomically claim one pending pipeline run. Returns run_id or None."""
    sql = """
        update crm.pipeline_runs
        set status = 'running'
        where id = (
            select id
            from crm.pipeline_runs
            where status = 'pending'
            order by created_at
            for update skip locked
            limit 1
        )
        returning id
    """
    row = db.fetch_one(sql)
    return row["id"] if row else None


def _maybe_reset_sent_today(state: dict) -> None:
    """Reset all mailboxes.sent_today to 0 at the start of each London day.

    Throttled to once per local date. The first tick after midnight
    Europe/London resets; subsequent ticks the same day are no-ops.
    Cheap: one row in state[] tracks the last-reset date.
    """
    today = datetime.now(LONDON).date().isoformat()
    if state.get("last_reset_date") == today:
        return

    # On worker startup the reset would fire immediately even on a quiet
    # afternoon if state is empty. Boot-time hydration from DB: if every
    # mailbox already has sent_today=0 we mark today as already-reset to
    # avoid a spurious reset on startup.
    if state.get("last_reset_date") is None:
        zeros = db.fetch_one(
            "select count(*) filter (where sent_today > 0) as nonzero from crm.mailboxes"
        )
        if zeros and zeros.get("nonzero", 0) == 0:
            state["last_reset_date"] = today
            return

    try:
        db.execute("update crm.mailboxes set sent_today = 0 where sent_today > 0")
        state["last_reset_date"] = today
        logger.info("Reset mailboxes.sent_today for %s", today)
    except Exception:
        logger.exception("Failed to reset sent_today — will retry on next tick")


def _maybe_run_schedule(state: dict) -> None:
    """Enqueue pipeline runs for clients whose daily schedule fires now (US-002).

    Throttled to once per minute via state['last_check_minute'] — the worker
    poll cadence is 5s but the schedule resolution is per-minute, so most
    ticks are no-ops.

    Idempotent: skips clients that already have a pipeline_run today
    (in Europe/London local date). Belt-and-braces against (a) the
    worker restarting mid-minute, (b) clock skew, (c) operator manually
    triggering a run earlier in the day.
    """
    now = datetime.now(LONDON)
    minute_key = now.strftime("%Y-%m-%dT%H:%M")
    if state.get("last_check_minute") == minute_key:
        return
    state["last_check_minute"] = minute_key

    sql = """
        select c.id, c.name
        from crm.clients c
        where c.status = 'active'
          and c.pipeline_paused = false
          and c.daily_pipeline_run_at is not null
          and to_char(c.daily_pipeline_run_at, 'HH24:MI') = %s
          and not exists (
            select 1 from crm.pipeline_runs pr
            where pr.client_id = c.id
              and (pr.created_at at time zone 'Europe/London')::date
                  = (now() at time zone 'Europe/London')::date
          )
    """
    try:
        due = db.fetch_all(sql, (now.strftime("%H:%M"),))
    except Exception:
        logger.exception("Schedule check failed — will retry next minute")
        return

    for r in due:
        try:
            db.execute(
                "insert into crm.pipeline_runs (client_id, status, mode, triggered_by) "
                "values (%s, 'pending', 'scheduled', 'cron')",
                (r["id"],),
            )
            logger.info("Scheduled pipeline_run enqueued for client %s (%s)",
                        r["id"], r["name"])
        except Exception:
            logger.exception("Failed to enqueue scheduled run for client %s", r["id"])


def _mark_failed(run_id: int, err: str) -> None:
    try:
        db.execute(
            """update crm.pipeline_runs
               set status='failed', error_msg=%s, finished_at=now()
               where id=%s""",
            (err[:500], run_id),
        )
    except Exception:
        logger.exception("Failed to mark run %s as failed", run_id)


def _write_heartbeat() -> None:
    """Stamp crm.settings.worker_heartbeat once per tick. Never raises —
    a DB blip mustn't take the loop down."""
    try:
        db.settings_set("worker_heartbeat", {
            "last_seen": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
        })
    except Exception:
        logger.exception("heartbeat write failed (continuing)")


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info("crm-worker starting (poll=%ss, outreach_tick=%ss, reply_tick=%ss)",
                POLL_INTERVAL_S, OUTREACH_TICK_S, REPLY_TICK_S)
    schedule_state: dict = {}
    outreach_state: dict = {"last_tick_at": 0.0}
    reply_state:    dict = {"last_tick_at": 0.0}
    reset_state:    dict = {}

    while _running:
        # Heartbeat first thing — if anything below crashes the loop, the
        # last_seen still reflects when we were last alive.
        _write_heartbeat()

        # Once-per-day at the first tick of a new London date.
        _maybe_reset_sent_today(reset_state)

        # Once-per-minute: enqueue any clients whose daily schedule fires now.
        _maybe_run_schedule(schedule_state)

        now_mono = time.monotonic()

        # Every OUTREACH_TICK_S (default 5 min).
        if now_mono - outreach_state["last_tick_at"] >= OUTREACH_TICK_S:
            outreach_state["last_tick_at"] = now_mono
            try:
                result = outreach_engine.tick()
                logger.info("outreach tick %s", result)
            except Exception:
                logger.exception("outreach tick failed — will retry in %ss",
                                 OUTREACH_TICK_S)

        # Every REPLY_TICK_S (default 10 min). Dry-run by default —
        # skips IMAP entirely until REPLY_MODE=live + creds are wired.
        if now_mono - reply_state["last_tick_at"] >= REPLY_TICK_S:
            reply_state["last_tick_at"] = now_mono
            try:
                result = reply_engine.tick()
                logger.info("reply tick %s", result)
            except Exception:
                logger.exception("reply tick failed — will retry in %ss",
                                 REPLY_TICK_S)

        try:
            run_id = _claim_next_run()
        except psycopg.OperationalError as e:
            logger.error("DB connection error claiming job: %s — sleeping 10s", e)
            time.sleep(10)
            continue
        except Exception:
            logger.exception("Unexpected error claiming job — sleeping 10s")
            time.sleep(10)
            continue

        if run_id is None:
            time.sleep(POLL_INTERVAL_S)
            continue

        logger.info("Claimed pipeline_run %s", run_id)
        started = datetime.now(timezone.utc)
        try:
            result = runner.run(run_id)
            logger.info("pipeline_run %s done: %s in %.1fs",
                        run_id, result, (datetime.now(timezone.utc) - started).total_seconds())
        except Exception as e:
            tb = traceback.format_exc()
            logger.error("pipeline_run %s failed: %s\n%s", run_id, e, tb)
            _mark_failed(run_id, str(e))

    logger.info("crm-worker exiting cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
