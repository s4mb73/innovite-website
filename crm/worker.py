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


def _reload_proxies(signum, frame):
    """SIGHUP handler — re-read /etc/innovite/proxies.list so the
    operator can add/rotate proxies without restarting the worker."""
    try:
        from scraper.proxy_pool import reload_pool
        n = reload_pool()
        logger.info("Reloaded proxy pool on SIGHUP — %d proxies active", n)
    except Exception:
        logger.exception("SIGHUP proxy reload failed")


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
    Also: recompute daily_cap from the warmup ramp curve, and run the
    bounce-rate circuit breaker. All three are once-per-day jobs that
    naturally coincide.

    Throttled to once per local date. The first tick after midnight
    Europe/London does the work; subsequent ticks the same day are
    no-ops. Cheap: one row in state[] tracks the last-reset date.
    """
    today = datetime.now(LONDON).date().isoformat()
    if state.get("last_reset_date") == today:
        return

    # On worker startup the reset would fire immediately even on a quiet
    # afternoon if state is empty. Boot-time hydration: if every mailbox
    # already has sent_today=0, presume today's reset already ran.
    if state.get("last_reset_date") is None:
        zeros = db.fetch_one(
            "select count(*) filter (where sent_today > 0) as nonzero from crm.mailboxes"
        )
        if zeros and zeros.get("nonzero", 0) == 0:
            state["last_reset_date"] = today
            _run_warmup_ramp()
            _run_mailbox_circuit_breaker()
            return

    try:
        db.execute("update crm.mailboxes set sent_today = 0 where sent_today > 0")
        state["last_reset_date"] = today
        logger.info("Reset mailboxes.sent_today for %s", today)
    except Exception:
        logger.exception("Failed to reset sent_today — will retry on next tick")
        return

    _run_warmup_ramp()
    _run_mailbox_circuit_breaker()


def _run_warmup_ramp() -> None:
    """Recompute daily_cap from the warmup curve once per day.

    Effective cap = min(target_daily_cap, 5 + 3 * days_since_warming_started).
    Backfilled mailboxes have warming_started_at set 30 days back, so
    `5 + 3*30 = 95` and `min(target, 95)` collapses to `target`. New
    mailboxes ramp 5 → target over ~14 days.

    Done at the daily reset rather than on every tick so the operator
    sees a stable cap throughout the day.
    """
    try:
        db.execute("""
            update crm.mailboxes
               set daily_cap = least(
                 coalesce(target_daily_cap, daily_cap),
                 5 + 3 * greatest(
                   0,
                   extract(day from (now() - warming_started_at))::int
                 )
               )
             where warming_started_at is not null
               and target_daily_cap   is not null
        """)
        logger.info("Warmup ramp daily_cap recomputed")
    except Exception:
        logger.exception("Warmup ramp update failed — caps unchanged for today")


# Circuit-breaker thresholds. 3% bounce rate = WARN (operator should
# investigate); 10% = AUTO-PAUSE (we stop sending immediately so the
# damage to sender reputation doesn't escalate). Both based on rolling
# 7-day windows so we don't trip on a single bad day.
_BOUNCE_PAUSE_PCT   = 10.0
_BOUNCE_WARN_PCT    = 3.0
_BOUNCE_MIN_SAMPLE  = 20  # don't trip thresholds on tiny samples


def _run_mailbox_circuit_breaker() -> None:
    """Compute rolling 7-day bounce rate per mailbox and auto-pause if
    it's above the danger threshold. Logs warnings at the lower threshold.

    Reads from crm.emails.from_address rather than mailbox_id because
    bounces matched on address are more authoritative — the recipient's
    ESP knows what From they actually saw, even if our internal records
    routed the send through a different mailbox row."""
    try:
        rows = db.fetch_all("""
            with sends as (
              select lower(from_address) as addr, count(*) as sent_n
                from crm.emails
               where sent_at >= now() - interval '7 days'
                 and from_address is not null
               group by lower(from_address)
            ),
            bounces as (
              select lower(from_address) as addr, count(*) as bounce_n
                from crm.emails
               where status = 'bounced'
                 and sent_at >= now() - interval '7 days'
                 and from_address is not null
               group by lower(from_address)
            )
            select s.addr,
                   s.sent_n,
                   coalesce(b.bounce_n, 0) as bounce_n,
                   case when s.sent_n > 0
                        then 100.0 * coalesce(b.bounce_n, 0) / s.sent_n
                        else 0 end as bounce_pct
              from sends s
              left join bounces b on b.addr = s.addr
             where s.sent_n >= %s
        """, (_BOUNCE_MIN_SAMPLE,))
    except Exception:
        logger.exception("Circuit breaker query failed — no mailboxes touched")
        return

    for r in rows:
        pct = float(r['bounce_pct'])
        addr = r['addr']
        if pct >= _BOUNCE_PAUSE_PCT:
            try:
                db.execute("""
                    update crm.mailboxes
                       set paused = true,
                           health_state = 'paused'
                     where lower(address) = %s
                       and paused = false
                """, (addr,))
                logger.warning(
                    "CIRCUIT BREAKER PAUSED %s — bounce_pct=%.1f%% (sent=%s, bounced=%s)",
                    addr, pct, r['sent_n'], r['bounce_n'],
                )
            except Exception:
                logger.exception("Failed to pause %s", addr)
        elif pct >= _BOUNCE_WARN_PCT:
            # Warn-level log — surfaces in journalctl without escalating
            # to an auto-pause. Operator decides.
            logger.warning(
                "MAILBOX BOUNCE WARN %s — bounce_pct=%.1f%% (sent=%s, bounced=%s)",
                addr, pct, r['sent_n'], r['bounce_n'],
            )


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
    signal.signal(signal.SIGHUP, _reload_proxies)

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
