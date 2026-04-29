"""Postgres connection helper + query functions for Innovite CRM.

Uses Supabase's transaction pooler (port 6543) via DATABASE_URL.
Raw SQL via psycopg — no ORM. All read functions return list[dict].
"""
import os
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get('DATABASE_URL')


# ── Connection ───────────────────────────────────────────────────────
@contextmanager
def get_conn():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL not set. Copy from Supabase → Project Settings → "
            "Database → Connection string → Transaction pooler."
        )
    with psycopg.connect(DATABASE_URL) as conn:
        yield conn


def fetch_all(sql: str, params: tuple = ()) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def fetch_one(sql: str, params: tuple = ()) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchone()


def execute(sql: str, params: tuple = ()) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()


# ── Dashboard queries (Step 3) ───────────────────────────────────────
def dashboard_metrics() -> dict:
    """Four headline numbers + change vs prior period."""
    sql = """
        select
          (select count(*) from crm.leads)                                                                                  as total_leads_now,
          (select count(*) from crm.leads where created_at <  now() - interval '7 days')                                    as total_leads_prev,

          (select count(*) from crm.emails where status = 'sent' and sent_at >= now() - interval '7 days')                  as emails_week_now,
          (select count(*) from crm.emails where status = 'sent' and sent_at >= now() - interval '14 days'
                                              and sent_at <  now() - interval '7 days')                                    as emails_week_prev,

          (select count(*) from crm.emails where status = 'sent' and sent_at  >= now() - interval '7 days')                 as sent_now,
          (select count(*) from crm.emails where replied_at is not null and replied_at >= now() - interval '7 days')        as repl_now,
          (select count(*) from crm.emails where status = 'sent' and sent_at >= now() - interval '14 days'
                                              and sent_at <  now() - interval '7 days')                                    as sent_prev,
          (select count(*) from crm.emails where replied_at is not null
                                              and replied_at >= now() - interval '14 days'
                                              and replied_at <  now() - interval '7 days')                                  as repl_prev,

          (select count(*) from crm.leads where status = 'meeting' and updated_at >= now() - interval '30 days')            as meetings_now,
          (select count(*) from crm.leads where status = 'meeting' and updated_at >= now() - interval '60 days'
                                              and updated_at <  now() - interval '30 days')                                as meetings_prev
    """
    r = fetch_one(sql) or {}

    def rate(replies: int, sent: int) -> float:
        return round(replies / sent * 100, 1) if sent else 0.0

    return {
        'total_leads':       r.get('total_leads_now', 0),
        'total_leads_delta': r.get('total_leads_now', 0) - r.get('total_leads_prev', 0),
        'emails_week':       r.get('emails_week_now', 0),
        'emails_week_delta': r.get('emails_week_now', 0) - r.get('emails_week_prev', 0),
        'reply_rate':        rate(r.get('repl_now', 0), r.get('sent_now', 0)),
        'reply_rate_delta':  round(
            rate(r.get('repl_now', 0),  r.get('sent_now', 0))
          - rate(r.get('repl_prev', 0), r.get('sent_prev', 0)),
            1,
        ),
        'meetings_month':       r.get('meetings_now', 0),
        'meetings_month_delta': r.get('meetings_now', 0) - r.get('meetings_prev', 0),
    }


def leads_per_day(days: int = 7) -> tuple[list[str], list[int]]:
    """Returns (labels, values) — zero-filled per day, oldest first."""
    sql = """
        select day::date as day, coalesce(c, 0) as leads
        from generate_series(
                 now()::date - make_interval(days => %s - 1),
                 now()::date,
                 '1 day') as day
        left join (
            select date_trunc('day', created_at)::date as d, count(*) as c
            from crm.leads
            where created_at >= now()::date - make_interval(days => %s - 1)
            group by 1
        ) leads on leads.d = day::date
        order by day
    """
    rows = fetch_all(sql, (days, days))
    labels = [r['day'].strftime('%a %d') for r in rows]
    values = [int(r['leads']) for r in rows]
    return labels, values


def recent_activity(limit: int = 20) -> list[dict]:
    """Latest activity_log rows, decorated with display label + colour."""
    sql = """
        select id, action, detail, created_at
        from crm.activity_log
        order by created_at desc
        limit %s
    """
    rows = fetch_all(sql, (limit,))
    for r in rows:
        r['label']    = ACTION_LABEL.get(r['action'], r['action'].replace('_', ' ').capitalize())
        r['colour']   = ACTION_COLOUR.get(r['action'], 'blue')
        r['relative'] = relative_time(r['created_at'])
    return rows


# ── Display helpers ──────────────────────────────────────────────────
ACTION_LABEL = {
    'lead_created':   'Lead created',
    'email_sent':     'Email sent',
    'reply_received': 'Reply received',
    'meeting_booked': 'Meeting booked',
    'inbound_lead':   'Inbound lead',
    'pipeline_run':   'Pipeline run',
    'lead_won':       'Lead won',
    'lead_lost':      'Lead lost',
}

ACTION_COLOUR = {
    'reply_received': 'green',
    'meeting_booked': 'green',
    'lead_won':       'green',
    'inbound_lead':   'amber',
}


def relative_time(dt: datetime) -> str:
    """Human-readable delta — '4m ago', '2h ago', '3d ago'."""
    now = datetime.now(dt.tzinfo or timezone.utc)
    secs = max(0, int((now - dt).total_seconds()))
    if secs < 60:    return f'{secs}s ago'
    if secs < 3600:  return f'{secs // 60}m ago'
    if secs < 86400: return f'{secs // 3600}h ago'
    return f'{secs // 86400}d ago'
