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

import psycopg

import db
from pipeline import runner

POLL_INTERVAL_S = int(os.environ.get("WORKER_POLL_INTERVAL_S", "5"))

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

    logger.info("crm-worker starting (poll=%ss)", POLL_INTERVAL_S)

    while _running:
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
