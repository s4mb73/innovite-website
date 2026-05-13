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

POLL_INTERVAL_S = int(os.environ.get("WORKER_POLL_INTERVAL_S", "5"))
OUTREACH_TICK_S = int(os.environ.get("WORKER_OUTREACH_TICK_S", "300"))
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


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info("crm-worker starting (poll=%ss, outreach_tick=%ss)",
                POLL_INTERVAL_S, OUTREACH_TICK_S)
    schedule_state: dict = {}
    outreach_state: dict = {"last_tick_at": 0.0}

    while _running:
        # Once-per-minute: enqueue any clients whose daily schedule fires now.
        _maybe_run_schedule(schedule_state)

        # Every OUTREACH_TICK_S (default 5 min): pass through the
        # scheduled-emails queue. Default mode is dry-run — see
        # outreach/engine.py module doc for the switch.
        now_mono = time.monotonic()
        if now_mono - outreach_state["last_tick_at"] >= OUTREACH_TICK_S:
            outreach_state["last_tick_at"] = now_mono
            try:
                result = outreach_engine.tick()
                logger.info("outreach tick %s", result)
            except Exception:
                logger.exception("outreach tick failed — will retry in %ss",
                                 OUTREACH_TICK_S)

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
