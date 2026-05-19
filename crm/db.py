"""Postgres connection helper + query functions for Innovite CRM.

Uses Supabase's transaction pooler (port 6543) via DATABASE_URL.
Raw SQL via psycopg — no ORM. All read functions return list[dict].
"""
import json
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


# ── Scraper observability ───────────────────────────────────────────
# Two helpers powering the Website intel UI:
#  - scraper_health_summary(N): aggregate of the last N attempted scrapes
#    → fuels the Overview "Scraper health" widget so the operator sees
#    pool degradation before it eats a campaign.
#  - scraper_offline_now(): true when the most recent attempts all came
#    back 'disabled' → fuels the leads-list banner. 'disabled' is set
#    when wreq isn't importable or the proxy pool is empty, so it's a
#    binary "the worker can't scrape at all" signal, not "this one lead
#    was unlucky". Sticky to the most recent run to avoid flapping.

def scraper_health_summary(window: int = 200) -> dict:
    """Counts of each website_scrape_status in the last `window` leads
    that were *attempted* (i.e. status is not null). Returns:
      {window: int, total: int, by_status: {status: count}, ok_pct,
       blocked_pct, no_website_pct, last_attempt_at}.
    Empty pipeline → total=0 and the template renders an empty state."""
    sql = """
        with recent as (
          select website_scrape_status as s, website_scraped_at as at
          from crm.leads
          where website_scrape_status is not null
          order by coalesce(website_scraped_at, created_at) desc
          limit %s
        )
        select s, count(*) as n, max(at) as last_at
        from recent
        group by s
    """
    rows = fetch_all(sql, (window,))
    by_status: dict[str, int] = {s: 0 for s in
        ('ok','blocked','timeout','no_website','parse_failed','disabled')}
    total = 0
    last_attempt_at = None
    for r in rows:
        s = r['s']
        n = int(r['n'])
        by_status[s] = by_status.get(s, 0) + n
        total += n
        if r['last_at'] is not None and (last_attempt_at is None
                                         or r['last_at'] > last_attempt_at):
            last_attempt_at = r['last_at']

    def pct(n: int) -> int:
        return round(n / total * 100) if total else 0

    return {
        'window':           window,
        'total':            total,
        'by_status':        by_status,
        'ok_pct':           pct(by_status.get('ok', 0)),
        'blocked_pct':      pct(by_status.get('blocked', 0)
                                + by_status.get('timeout', 0)
                                + by_status.get('parse_failed', 0)),
        'no_website_pct':   pct(by_status.get('no_website', 0)),
        'disabled_pct':     pct(by_status.get('disabled', 0)),
        'last_attempt_at':  last_attempt_at,
        'last_relative':    relative_time(last_attempt_at) if last_attempt_at else None,
    }


def scraper_offline_now() -> bool:
    """True when the worker scraper is currently broken — defined as
    'the last 5 attempted scrapes all came back disabled'. Stricter
    than checking just the most recent so a one-off race (e.g. proxy
    pool reload mid-fetch) doesn't trigger an alarm banner."""
    rows = fetch_all("""
        select website_scrape_status as s
        from crm.leads
        where website_scrape_status is not null
        order by coalesce(website_scraped_at, created_at) desc
        limit 5
    """)
    if len(rows) < 5:
        return False
    return all(r['s'] == 'disabled' for r in rows)


# ── Display helpers ──────────────────────────────────────────────────
ACTION_LABEL = {
    'lead_created':         'Lead created',
    'email_sent':           'Email sent',
    'reply_received':       'Reply received',
    'meeting_booked':       'Meeting booked',
    'inbound_lead':         'Inbound lead',
    'pipeline_run':         'Pipeline run',
    'lead_won':             'Lead won',
    'lead_lost':            'Lead lost',
    'lead_status_changed':  'Status changed',
    'lead_status_undone':   'Status reverted',
    'outreach_paused':      'Outreach paused',
    'outreach_resumed':     'Outreach resumed',
    'outreach_batch':       'Outreach scheduled',
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


# ── Clients page (Step 4) ────────────────────────────────────────────
def list_clients() -> list[dict]:
    """All clients with embedded 7-day metrics + operational state.

    Operational signals (US-019) — used to render a contextual third column
    instead of retainer / pricing tier:
      pending_today      — emails queued for today
      last_lead_added    — most recent lead.created_at (any lead, lifetime)
      outreach_paused    — campaign-level kill switch
      target_locations   — preserved to render under the client name as a
                           "is targeting still right?" sanity check
      targeting_empty    — derived: true if industries OR locations is null/[]
    """
    sql = """
        select
          c.id, c.name, c.industry, c.contact_name, c.contact_email,
          c.target_industries, c.target_locations, c.outreach_paused,
          c.monthly_fee, c.pricing_tier, c.status, c.created_at, c.onboarded_at,
          (select count(*) from crm.leads l
             where l.client_id = c.id and l.created_at >= now() - interval '7 days')          as leads_7d,
          (select count(*) from crm.leads l
             where l.client_id = c.id and l.created_at >= now() - interval '14 days'
                                      and l.created_at <  now() - interval '7 days')         as leads_7d_prev,
          (select count(*) from crm.emails e
             where e.client_id = c.id and e.status = 'sent'
                                      and e.sent_at  >= now() - interval '7 days')           as sent_7d,
          (select count(*) from crm.emails e
             where e.client_id = c.id and e.replied_at is not null
                                      and e.replied_at >= now() - interval '7 days')         as replied_7d,
          (select count(*) from crm.emails e
             where e.client_id = c.id and e.status = 'sent'
                                      and e.sent_at  >= now() - interval '14 days'
                                      and e.sent_at  <  now() - interval '7 days')           as sent_7d_prev,
          (select count(*) from crm.emails e
             where e.client_id = c.id and e.replied_at is not null
                                      and e.replied_at >= now() - interval '14 days'
                                      and e.replied_at <  now() - interval '7 days')         as replied_7d_prev,
          (select count(*) from crm.emails e
             where e.client_id = c.id and e.status = 'scheduled'
                                      and e.scheduled_at::date = current_date)               as pending_today,
          (select max(l.created_at) from crm.leads l
             where l.client_id = c.id)                                                       as last_lead_added
        from crm.clients c
    """
    # Note: ORDER BY is applied in Python after op_state is computed below,
    # so the surface-problems-first sort can use the derived state directly
    # without redoing the same logic in SQL.
    rows = fetch_all(sql)
    now  = datetime.now(timezone.utc)
    for r in rows:
        sent       = r.get('sent_7d', 0) or 0
        replied    = r.get('replied_7d', 0) or 0
        sent_prev  = r.get('sent_7d_prev', 0) or 0
        replied_pv = r.get('replied_7d_prev', 0) or 0
        r['reply_rate']       = round(replied   / sent      * 100, 1) if sent      else 0.0
        r['reply_rate_prev']  = round(replied_pv / sent_prev * 100, 1) if sent_prev else 0.0
        r['reply_rate_delta'] = round(r['reply_rate'] - r['reply_rate_prev'], 1)
        r['leads_7d_delta']   = (r.get('leads_7d') or 0) - (r.get('leads_7d_prev') or 0)
        since_dt              = r.get('onboarded_at') or r.get('created_at')
        r['since']            = since_dt.strftime('%b %Y') if since_dt else ''

        # Operational state for the contextual third column.
        industries = r.get('target_industries') or []
        locations  = r.get('target_locations')  or []
        r['targeting_empty'] = not industries or not locations
        # Show first 2 locations on the meta line; UI handles the "+N more".
        r['locations_preview'] = locations[:2] if isinstance(locations, list) else []
        r['locations_extra']   = max(0, (len(locations) - 2)) if isinstance(locations, list) else 0

        last = r.get('last_lead_added')
        if last is not None:
            secs = max(0, int((now - last).total_seconds()))
            r['last_find_relative'] = (
                f'{secs // 3600}h ago' if secs >= 3600
                else f'{max(1, secs // 60)}m ago' if secs >= 60
                else 'just now'
            )
            r['last_find_days'] = secs // 86400
        else:
            r['last_find_relative'] = None
            r['last_find_days']     = None

        # Single derived state slug — drives both the third-column copy and
        # the left-border colour. Order matters: more-specific first.
        created_at = r.get('created_at')
        age_secs   = int((now - created_at).total_seconds()) if created_at else 0
        if r['targeting_empty']:
            state = 'needs_targeting'
        elif r.get('outreach_paused'):
            state = 'paused'
        elif age_secs < 86400 and (r.get('leads_7d') or 0) == 0:
            state = 'just_added'
        elif r['last_find_days'] is not None and r['last_find_days'] >= 7:
            state = 'stale'
        else:
            state = 'healthy'
        r['op_state'] = state

    # Surface problems first, healthy last — the whole point of the
    # left-border colour-coding is wasted if you have to scan past 12
    # green rows to find the one amber. Ordering rules:
    #   1. Lifecycle status — active above paused above churned
    #   2. Within active: needs_targeting > paused > stale > just_added > healthy
    #   3. Within state: most-stale or most-recently-created first
    op_priority = {
        'needs_targeting': 0,
        'paused':          1,
        'stale':           2,
        'just_added':      3,
        'healthy':         4,
    }
    lifecycle_priority = {'active': 0, 'paused': 1, 'churned': 2}
    rows.sort(key=lambda r: (
        lifecycle_priority.get(r.get('status'), 9),
        op_priority.get(r.get('op_state'), 9),
        # Most-stale first within stale; newest first for everything else.
        -(r.get('last_find_days') or 0) if r.get('op_state') == 'stale'
            else -((r.get('created_at').timestamp()) if r.get('created_at') else 0),
    ))
    return rows


# ── Client detail (Step 4 part 2) ────────────────────────────────────
def get_client(client_id: int) -> dict | None:
    sql = """
        select id, name, industry, contact_name, contact_email,
               monthly_fee, pricing_tier, status,
               target_industries, target_locations, targeting_filters,
               daily_pipeline_run_at, pipeline_paused,
               onboarded_at, created_at
        from crm.clients
        where id = %s
    """
    r = fetch_one(sql, (client_id,))
    if r:
        since_dt = r.get('onboarded_at') or r.get('created_at')
        r['since'] = since_dt.strftime('%b %Y') if since_dt else ''
    return r


def client_stats(client_id: int) -> dict:
    """Headline numbers for one client. Mirrors dashboard_metrics() scoped."""
    sql = """
        select
          (select count(*) from crm.leads
             where client_id = %(cid)s)                                                                    as total_leads,
          (select count(*) from crm.leads
             where client_id = %(cid)s and created_at >= now() - interval '7 days')                        as leads_7d,
          (select count(*) from crm.leads
             where client_id = %(cid)s and created_at >= now() - interval '14 days'
                                       and created_at <  now() - interval '7 days')                       as leads_7d_prev,

          (select count(*) from crm.emails
             where client_id = %(cid)s and status = 'sent'
                                       and sent_at >= now() - interval '7 days')                          as sent_7d,
          (select count(*) from crm.emails
             where client_id = %(cid)s and replied_at is not null
                                       and replied_at >= now() - interval '7 days')                       as repl_7d,
          (select count(*) from crm.emails
             where client_id = %(cid)s and status = 'sent'
                                       and sent_at >= now() - interval '14 days'
                                       and sent_at <  now() - interval '7 days')                          as sent_7d_prev,
          (select count(*) from crm.emails
             where client_id = %(cid)s and replied_at is not null
                                       and replied_at >= now() - interval '14 days'
                                       and replied_at <  now() - interval '7 days')                       as repl_7d_prev,

          (select count(*) from crm.leads
             where client_id = %(cid)s and status = 'meeting'
                                       and updated_at >= now() - interval '30 days')                      as meetings_30d,
          (select count(*) from crm.leads
             where client_id = %(cid)s and status = 'meeting'
                                       and updated_at >= now() - interval '60 days'
                                       and updated_at <  now() - interval '30 days')                      as meetings_30d_prev
    """
    r = fetch_one(sql, {'cid': client_id}) or {}

    def rate(num: int, den: int) -> float:
        return round(num / den * 100, 1) if den else 0.0

    sent      = r.get('sent_7d', 0) or 0
    repl      = r.get('repl_7d', 0) or 0
    sent_prev = r.get('sent_7d_prev', 0) or 0
    repl_prev = r.get('repl_7d_prev', 0) or 0

    return {
        'total_leads':        r.get('total_leads', 0) or 0,
        'total_leads_delta':  (r.get('leads_7d', 0) or 0) - (r.get('leads_7d_prev', 0) or 0),
        'reply_rate':         rate(repl, sent),
        'reply_rate_delta':   round(rate(repl, sent) - rate(repl_prev, sent_prev), 1),
        'meetings_month':     r.get('meetings_30d', 0) or 0,
        'meetings_month_delta': (r.get('meetings_30d', 0) or 0) - (r.get('meetings_30d_prev', 0) or 0),
    }


GRADE_COLOUR = {'A': 'green', 'B': 'blue', 'C': 'amber', 'D': 'red', 'F': 'grey'}
STATUS_COLOUR = {
    'new':       'grey',
    'contacted': 'grey',
    'replied':   'blue',
    'meeting':   'green',
    'won':       'green',
    'lost':      'red',
}


def client_recent_leads(client_id: int, limit: int = 10) -> list[dict]:
    sql = """
        select id, business_name, grade, status, revenue_gap_estimate,
               decision_maker_name, created_at
        from crm.leads
        where client_id = %s
        order by created_at desc
        limit %s
    """
    rows = fetch_all(sql, (client_id, limit))
    for r in rows:
        r['grade_colour']  = GRADE_COLOUR.get(r.get('grade') or '', 'grey')
        r['status_colour'] = STATUS_COLOUR.get(r.get('status') or '', 'grey')
        r['relative']      = relative_time(r['created_at'])
    return rows


# ── Leads list (Step 5) ──────────────────────────────────────────────
# Operator-facing statuses (US-020). 'closed' is dropped from the UI but
# stays in the schema CHECK constraint to avoid a destructive migration.
LEAD_STATUSES = ['new', 'contacted', 'replied', 'meeting', 'won', 'lost']

SORT_SQL = {
    # Default — needs-attention first (US-021). 'replied' on top because
    # it's the operator's morning action queue; in-cadence and fresh after,
    # terminal at the bottom. Stable secondary sort by updated_at so the
    # most recently moved leads appear first within each bucket.
    'triage': """
        case l.status
          when 'replied'   then 1
          when 'contacted' then 2
          when 'new'       then 3
          when 'meeting'   then 4
          when 'won'       then 5
          when 'lost'      then 6
          else 7
        end,
        coalesce(l.updated_at, l.created_at) desc
    """,
    'recent': 'l.created_at desc',
    'grade':  "case l.grade when 'A' then 1 when 'B' then 2 when 'C' then 3 when 'D' then 4 when 'F' then 5 else 6 end, l.created_at desc",
    'score':  'coalesce(l.overall_score, 0) desc, l.created_at desc',
    # Pain rollup (US-021 follow-up). NULL pain_score sorts last via coalesce(0).
    'pain':   'coalesce(l.pain_score, 0) desc, l.created_at desc',
}

# Maps lead.status → row-border colour slug used by the Leads page.
# Mirrors the op-state idiom from the Clients roster (US-019). Only
# 'replied' (needs response) and 'meeting' (booked, in motion) get a
# coloured stripe; everything else is muted or borderless.
LEAD_OP_STATE = {
    'replied':   'replied',    # amber — needs response
    'meeting':   'meeting',    # green — in motion
    'new':       'new',        # grey  — fresh, no action yet
    'contacted': 'contacted',  # no border — in active cadence
    'won':       'won',        # grey dim — terminal positive
    'lost':      'lost',       # grey dim — terminal negative
}


def _stage_time_label(stage_started: datetime | None, now: datetime | None = None) -> str:
    """How long the lead has been in its current status. Rough magnitude
    only — 'just now' / 'Xm' / 'Xh' / 'Xd'. Fed by updated_at, which is
    'last touched' (not strictly 'last status change'), but close enough
    in practice — most writes are status updates."""
    if not stage_started:
        return '—'
    now = now or datetime.now(timezone.utc)
    secs = max(0, int((now - stage_started).total_seconds()))
    if secs < 60:    return 'just now'
    if secs < 3600:  return f'{secs // 60}m'
    if secs < 86400: return f'{secs // 3600}h'
    return f'{secs // 86400}d'


def _stage_time_hours(stage_started: datetime | None, now: datetime | None = None) -> int | None:
    """Hours-in-stage as a number — drives row-level urgency colouring
    and the conditional 'needs response' / 'overdue' CTAs on replied
    rows. None when we have no timestamp to compare against."""
    if not stage_started:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0, int((now - stage_started).total_seconds()) // 3600)


def _stage_urgency(hours: int | None, op_state: str) -> str:
    """Bucket stage-time hours into a colour class for the row's stage
    cell. Terminal states (won/lost) and contacted (still in cadence)
    don't carry urgency — they map to the neutral default."""
    if hours is None or op_state in ('won', 'lost', 'contacted'):
        return 'default'
    if hours < 24:   return 'green'
    if hours < 96:   return 'default'
    if hours < 168:  return 'amber'
    return 'red'


def leads_search(
    *,
    status: str | None = None,
    client_id: int | None = None,
    search: str | None = None,
    sort: str = 'recent',
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """List leads with filters, search, sort, pagination — and the
    counts per status for the filter tabs. One round-trip would be
    nicer; for clarity we keep three queries (rows / total / status counts).
    """
    where = ['1=1']
    params: dict = {}
    if status and status in LEAD_STATUSES:
        where.append('l.status = %(status)s')
        params['status'] = status
    if client_id:
        where.append('l.client_id = %(client_id)s')
        params['client_id'] = client_id
    if search:
        where.append('(l.business_name ilike %(q)s or l.decision_maker_name ilike %(q)s)')
        params['q'] = f'%{search}%'

    where_sql = ' and '.join(where)
    order_sql = SORT_SQL.get(sort, SORT_SQL['triage'])
    page = max(1, page)
    offset = (page - 1) * page_size
    params['limit'] = page_size
    params['offset'] = offset

    # Wider SELECT for the £2k-platform Leads table — adds pain signals,
    # hook context, deliverability state, and per-lead activity counts.
    # Subqueries for last_contact_at and reply_count are correlated but
    # cheap given existing indexes on emails.lead_id and replies.lead_id.
    rows = fetch_all(f"""
        select l.id, l.business_name, l.grade, l.status, l.overall_score,
               l.pain_score, l.hook_type,
               l.decision_maker_name, l.decision_maker_title,
               l.email, l.linkedin_url,
               l.city, l.google_review_count,
               l.companies_house_revenue_band as revenue_band,
               l.companies_house_months_to_year_end as months_to_year_end,
               l.companies_house_accounts_overdue   as accounts_overdue,
               l.companies_house_confirmation_overdue as confirmation_overdue,
               l.companies_house_recent_director_change as director_change,
               l.created_at, l.updated_at,
               c.id as client_id, c.name as client_name,
               (select max(e.sent_at) from crm.emails e
                 where e.lead_id = l.id
                   and e.status in ('sent','dry_run_ready')) as last_contact_at,
               (select count(*) from crm.replies r
                 where r.lead_id = l.id) as reply_count
          from crm.leads l
          left join crm.clients c on c.id = l.client_id
         where {where_sql}
         order by {order_sql}
         limit %(limit)s offset %(offset)s
    """, params)

    now = datetime.now(timezone.utc)
    for r in rows:
        r['grade_colour']  = GRADE_COLOUR.get(r.get('grade') or '', 'grey')
        r['status_colour'] = STATUS_COLOUR.get(r.get('status') or '', 'grey')
        r['relative']      = relative_time(r['created_at'])
        # US-021 — triage row decoration.
        stage_started = r.get('updated_at') or r.get('created_at')
        hours_in_stage = _stage_time_hours(stage_started, now)
        r['stage_time_label'] = _stage_time_label(stage_started, now)
        r['stage_time_hours'] = hours_in_stage
        r['op_state']         = LEAD_OP_STATE.get(r.get('status') or '', 'new')
        r['stage_urgency']    = _stage_urgency(hours_in_stage, r['op_state'])

        # Display helpers — keep template logic minimal.
        pain = r.get('pain_score') or 0
        r['pain_tier'] = (
            'hot'  if pain >= 60 else
            'warm' if pain >= 30 else
            'cool' if pain >  0  else 'none'
        )
        m = r.get('months_to_year_end')
        if m is None:
            r['year_end_label'] = '—'
            r['year_end_urgency'] = 'none'
        elif m <= 0:
            r['year_end_label'] = 'this month'
            r['year_end_urgency'] = 'hot'
        elif m <= 3:
            r['year_end_label'] = f'{m}m'
            r['year_end_urgency'] = 'warm'
        else:
            r['year_end_label'] = f'{m}m'
            r['year_end_urgency'] = 'cool'
        last = r.get('last_contact_at')
        r['last_contact_label'] = relative_time(last) if last else 'never'

    total_row = fetch_one(f"""
        select count(*) as total
          from crm.leads l
         where {where_sql}
    """, params) or {'total': 0}
    total = int(total_row['total'])

    # Status counts ignore the *status* filter (so the tabs always show
    # how many you'd see if you switched to that tab) but DO honour the
    # other filters (client, search).
    other_where = ['1=1']
    other_params: dict = {}
    if client_id:
        other_where.append('l.client_id = %(client_id)s')
        other_params['client_id'] = client_id
    if search:
        other_where.append('(l.business_name ilike %(q)s or l.decision_maker_name ilike %(q)s)')
        other_params['q'] = f'%{search}%'
    counts_rows = fetch_all(f"""
        select coalesce(l.status, 'new') as status, count(*) as n
          from crm.leads l
         where {' and '.join(other_where)}
         group by 1
    """, other_params)
    counts = {s: 0 for s in LEAD_STATUSES}
    for c in counts_rows:
        if c['status'] in counts:
            counts[c['status']] = int(c['n'])
    counts['all'] = sum(counts.values())

    pages = max(1, (total + page_size - 1) // page_size)
    return {
        'rows':       rows,
        'total':      total,
        'counts':     counts,
        'page':       page,
        'pages':      pages,
        'page_size':  page_size,
        'page_start': offset + 1 if total else 0,
        'page_end':   min(offset + page_size, total),
    }


def all_clients_min() -> list[dict]:
    """Tiny client list for the leads-page filter dropdown."""
    if not DATABASE_URL:
        return [{'id': c['id'], 'name': c['name']}
                for c in _REPORTS_CLIENTS_FIXTURE]
    return fetch_all("select id, name from crm.clients order by name")


def quick_search(q: str, limit_per_kind: int = 8) -> dict:
    """Cross-resource search for the ⌘K palette.

    Returns {'clients': [...], 'leads': [...]} — small, ranked lists
    suitable for an interactive picker. Empty query → empty results
    (recent items are surfaced by the palette frontend separately if
    we ever add that)."""
    q = (q or '').strip()
    if not q:
        return {'clients': [], 'leads': []}
    pattern = f'%{q}%'
    clients = fetch_all("""
        select id, name
          from crm.clients
         where name ilike %s
         order by name
         limit %s
    """, (pattern, limit_per_kind))
    leads = fetch_all("""
        select l.id, l.business_name, l.decision_maker_name, l.status,
               c.name as client_name
          from crm.leads l
          left join crm.clients c on c.id = l.client_id
         where l.business_name        ilike %s
            or l.decision_maker_name  ilike %s
            or l.email                ilike %s
         order by l.created_at desc
         limit %s
    """, (pattern, pattern, pattern, limit_per_kind))
    return {'clients': clients, 'leads': leads}


def leads_count_per_client(*, status: str | None = None,
                           search: str | None = None) -> list[dict]:
    """Active clients with their lead counts under the current visible
    filter set (status + search), so the per-client tabs show exactly
    how many rows each tab would render. Churned clients are excluded.

    Honours `status` and `search` but NOT `client_id` — the whole point
    is to show what each client looks like under the current view."""
    if not DATABASE_URL:
        # Fixture mode: deterministic counts so the tabs render with
        # plausible numbers in the local POC. Status/search filters are
        # ignored here since fixture leads aren't queryable.
        return [{'id': 1, 'name': 'Vidora Media',     'count': 24},
                {'id': 2, 'name': 'ROCA Accountants', 'count': 18}]
    lead_where = ['l.client_id = c.id']
    params: dict = {}
    if status and status in LEAD_STATUSES:
        lead_where.append('l.status = %(status)s')
        params['status'] = status
    if search:
        lead_where.append('(l.business_name ilike %(q)s or l.decision_maker_name ilike %(q)s)')
        params['q'] = f'%{search}%'

    sql = f"""
        select c.id, c.name,
               (select count(*)
                  from crm.leads l
                 where {' and '.join(lead_where)}) as count
          from crm.clients c
         where c.status != 'churned'
         order by c.name
    """
    rows = fetch_all(sql, params)
    return [{'id': int(r['id']), 'name': r['name'], 'count': int(r['count'])} for r in rows]


def bulk_change_status(lead_ids: list[int], new_status: str) -> dict:
    """Apply new_status to lead_ids, log every actual change to
    crm.activity_log, and return the rows that changed so the caller
    can offer Undo. Leads already on new_status are skipped silently."""
    if new_status not in LEAD_STATUSES:
        raise ValueError(f'Invalid status: {new_status!r}')
    if not lead_ids:
        return {'updated': 0, 'previous': []}
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            # Snapshot before we change anything (for audit + undo)
            cur.execute(
                "select id, status, client_id from crm.leads where id = any(%s)",
                (lead_ids,),
            )
            snapshot = cur.fetchall()
            changed = [s for s in snapshot if s['status'] != new_status]

            if not changed:
                conn.commit()
                return {'updated': 0, 'previous': []}

            changed_ids = [s['id'] for s in changed]
            cur.execute(
                "update crm.leads set status = %s where id = any(%s)",
                (new_status, changed_ids),
            )
            for s in changed:
                cur.execute(
                    "insert into crm.activity_log (client_id, lead_id, action, detail)"
                    " values (%s, %s, %s, %s)",
                    (s['client_id'], s['id'], 'lead_status_changed',
                     f"{s['status']} → {new_status}"),
                )
        conn.commit()
    return {
        'updated':  len(changed),
        'previous': [(s['id'], s['status']) for s in changed],
    }


# ── Lead detail (Step 6) ─────────────────────────────────────────────
def get_lead(lead_id: int) -> dict | None:
    sql = """
        select l.*, c.id as client_id_, c.name as client_name
        from crm.leads l
        left join crm.clients c on c.id = l.client_id
        where l.id = %s
    """
    r = fetch_one(sql, (lead_id,))
    if not r:
        return None
    r['grade_colour']  = GRADE_COLOUR.get(r.get('grade') or '', 'grey')
    r['status_colour'] = STATUS_COLOUR.get(r.get('status') or '', 'grey')
    return r


def lead_timeline(lead_id: int) -> list[dict]:
    """Outbound emails + inbound replies, merged chronologically.

    Each entry: {kind, ts, status, title, body, email_number, sentiment}
    where kind in {'email', 'reply'}, status flags filled vs hollow dot.
    """
    emails = fetch_all("""
        select id, email_number, subject, body, status,
               sent_at, scheduled_at, replied_at
        from crm.emails
        where lead_id = %s
    """, (lead_id,))
    replies = fetch_all("""
        select id, from_address, subject, body, sentiment, detected_at
        from crm.replies
        where lead_id = %s
    """, (lead_id,))

    items: list[dict] = []
    for e in emails:
        ts = e.get('sent_at') or e.get('scheduled_at')
        items.append({
            'kind':         'email',
            'ts':           ts,
            'sent':         bool(e.get('sent_at')),
            'status':       e.get('status'),
            'email_number': e.get('email_number'),
            'title':        f"Day {e.get('email_number')} {'sent' if e.get('sent_at') else 'scheduled'}",
            'subject':      e.get('subject') or '',
            'body':         (e.get('body') or '')[:240],
        })
    for r in replies:
        items.append({
            'kind':       'reply',
            'ts':         r.get('detected_at'),
            'sent':       True,
            'sentiment':  r.get('sentiment'),
            'title':      f"Reply received · {r.get('sentiment') or 'neutral'}",
            'subject':    r.get('subject') or '',
            'body':       (r.get('body') or '')[:240],
        })
    items.sort(key=lambda x: x['ts'] or datetime.min.replace(tzinfo=timezone.utc))
    for it in items:
        it['relative'] = relative_time(it['ts']) if it['ts'] else ''
        it['date']     = it['ts'].strftime('%-d %b') if it['ts'] else ''
    return items


def lead_activity(lead_id: int, limit: int = 30) -> list[dict]:
    rows = fetch_all("""
        select id, action, detail, created_at
        from crm.activity_log
        where lead_id = %s
        order by created_at desc
        limit %s
    """, (lead_id, limit))
    for r in rows:
        r['label']    = ACTION_LABEL.get(r['action'], r['action'].replace('_', ' ').capitalize())
        r['colour']   = ACTION_COLOUR.get(r['action'], 'blue')
        r['relative'] = relative_time(r['created_at'])
    return rows


def update_lead_status(lead_id: int, new_status: str) -> str | None:
    """Set status on a single lead and log the change. Returns the
    previous status (or None if no change / lead missing)."""
    if new_status not in LEAD_STATUSES:
        raise ValueError(f'Invalid status: {new_status!r}')
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("select status, client_id from crm.leads where id = %s", (lead_id,))
            row = cur.fetchone()
            if not row:
                raise LookupError(f'No lead {lead_id}')
            old = row['status']
            if old == new_status:
                return None
            cur.execute("update crm.leads set status = %s where id = %s", (new_status, lead_id))
            cur.execute(
                "insert into crm.activity_log (client_id, lead_id, action, detail)"
                " values (%s, %s, %s, %s)",
                (row['client_id'], lead_id, 'lead_status_changed', f'{old} → {new_status}'),
            )
        conn.commit()
    return old


def update_lead_notes(lead_id: int, notes: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("update crm.leads set notes = %s where id = %s", (notes or None, lead_id))
        conn.commit()


def bulk_revert_status(previous: list[tuple[int, str]]) -> int:
    """Revert each (lead_id, old_status) and log each reversion."""
    if not previous:
        return 0
    n = 0
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            for lid, old_status in previous:
                if old_status not in LEAD_STATUSES:
                    continue
                cur.execute(
                    "update crm.leads set status = %s where id = %s",
                    (old_status, lid),
                )
                if cur.rowcount:
                    n += 1
                    cur.execute(
                        "select client_id from crm.leads where id = %s",
                        (lid,),
                    )
                    row = cur.fetchone()
                    cur.execute(
                        "insert into crm.activity_log (client_id, lead_id, action, detail)"
                        " values (%s, %s, %s, %s)",
                        (row['client_id'] if row else None, lid,
                         'lead_status_undone', f'reverted to {old_status}'),
                    )
        conn.commit()
    return n


# ── Outreach (Step 7) ────────────────────────────────────────────────
OUTREACH_TABS = ('today', 'sent', 'followups', 'bounces')

# Day-N labels keyed by emails.email_number (1, 2, 3)
EMAIL_STEP_LABEL = {1: 'Day 1', 2: 'Day 3', 3: 'Day 7'}


def outreach_kpis() -> dict:
    """Four headline numbers for the Outreach page.

    Pending today is calendar-day Europe/London (matches operator
    morning-check mental model — at 4pm, a rolling-24h count would
    leak tomorrow's queue into today's number).
    """
    sql = """
        with
          today_window as (
            select date_trunc('day', now() at time zone 'Europe/London')
                     at time zone 'Europe/London'                as day_start,
                   (date_trunc('day', now() at time zone 'Europe/London')
                     + interval '1 day') at time zone 'Europe/London' as day_end
          )
        select
          (select count(*) from crm.emails, today_window
             where status = 'scheduled'
               and scheduled_at >= day_start and scheduled_at < day_end)                    as pending_today,
          (select count(*) from crm.emails
             where status = 'sent' and sent_at >= now() - interval '7 days')                as sent_7d,
          (select count(*) from crm.emails
             where status = 'sent' and sent_at >= now() - interval '7 days')                as sent_total_7d,
          (select count(*) from crm.emails
             where replied_at is not null and replied_at >= now() - interval '7 days')      as replied_7d,
          (select count(*) from crm.emails
             where status = 'bounced' and created_at >= now() - interval '7 days')          as bounced_7d
    """
    r = fetch_one(sql) or {}
    sent       = int(r.get('sent_7d') or 0)
    replied    = int(r.get('replied_7d') or 0)
    bounced    = int(r.get('bounced_7d') or 0)
    attempted  = sent + bounced  # rough denominator for bounce rate
    return {
        'pending_today': int(r.get('pending_today') or 0),
        'sent_7d':       sent,
        'reply_rate':    round(replied / sent * 100, 1) if sent else 0.0,
        'bounce_rate':   round(bounced / attempted * 100, 1) if attempted else 0.0,
    }


def outreach_tab_counts(client_id: int | None = None) -> dict:
    """Count per tab — driven by the same filters the lists use."""
    where_client = ''
    params: dict = {}
    if client_id:
        where_client = 'and client_id = %(cid)s'
        params['cid'] = client_id

    sql = f"""
        with today_window as (
            select date_trunc('day', now() at time zone 'Europe/London')
                     at time zone 'Europe/London'                as day_start,
                   (date_trunc('day', now() at time zone 'Europe/London')
                     + interval '1 day') at time zone 'Europe/London' as day_end
          )
        select
          (select count(*) from crm.emails, today_window
             where status = 'scheduled'
               and scheduled_at >= day_start and scheduled_at < day_end
               {where_client})                                                              as today,
          (select count(*) from crm.emails
             where status = 'sent' and sent_at >= now() - interval '30 days'
               {where_client})                                                              as sent,
          (select count(*) from crm.emails, today_window
             where status = 'scheduled' and email_number in (2, 3)
               and scheduled_at >= day_end
               and scheduled_at <  day_end + interval '7 days'
               {where_client})                                                              as followups,
          (select count(*) from crm.emails
             where status = 'bounced'
               {where_client})                                                              as bounces
    """
    r = fetch_one(sql, params) or {}
    return {k: int(r.get(k) or 0) for k in ('today', 'sent', 'followups', 'bounces')}


def outreach_clients_panel() -> list[dict]:
    """Active clients with their pause state + pending count for the strip."""
    sql = """
        with today_window as (
            select date_trunc('day', now() at time zone 'Europe/London')
                     at time zone 'Europe/London'                as day_start,
                   (date_trunc('day', now() at time zone 'Europe/London')
                     + interval '1 day') at time zone 'Europe/London' as day_end
          )
        select
          c.id, c.name, c.outreach_paused, c.status,
          (select count(*) from crm.emails e, today_window
             where e.client_id = c.id and e.status = 'scheduled'
               and e.scheduled_at >= day_start and e.scheduled_at < day_end)  as pending_today,
          (select count(*) from crm.emails e
             where e.client_id = c.id and e.status = 'scheduled'
               and e.scheduled_at >= now()
               and e.scheduled_at <  now() + interval '7 days')               as queued_7d
        from crm.clients c
        where c.status != 'churned'
        order by c.name
    """
    return fetch_all(sql)


EMPLOYEE_BANDS = ('1-10', '11-50', '51-200', '200+')


def clients_for_finder() -> list[dict]:
    """Picker rows for the Find new leads modal (US-018).
    Returns one row per non-churned client with:
      id, name, last_find_relative, last_lead_count_label, targeting_empty.
    Disabled rows (targeting_empty == True) render as a Set-targeting CTA
    instead of a checkable row.
    """
    sql = """
        select
          c.id, c.name, c.target_industries, c.target_locations,
          (select max(l.created_at) from crm.leads l
             where l.client_id = c.id)                         as last_lead_added,
          (select count(*)         from crm.leads l
             where l.client_id = c.id
               and l.created_at >= now() - interval '7 days')  as leads_7d
        from crm.clients c
        where c.status != 'churned'
        order by c.name
    """
    rows = fetch_all(sql)
    now = datetime.now(timezone.utc)
    for r in rows:
        industries = r.get('target_industries') or []
        locations  = r.get('target_locations')  or []
        r['targeting_empty'] = not industries or not locations
        last = r.get('last_lead_added')
        if last is not None:
            secs = max(0, int((now - last).total_seconds()))
            r['last_find_relative'] = (
                f'{secs // 86400}d ago' if secs >= 86400
                else f'{secs // 3600}h ago' if secs >= 3600
                else f'{max(1, secs // 60)}m ago' if secs >= 60
                else 'just now'
            )
        else:
            r['last_find_relative'] = 'never'
        r['last_lead_count_label'] = (
            f"{r.get('leads_7d', 0)} found · 7d" if r.get('leads_7d')
            else 'no leads yet'
        )
    return rows


def create_client(
    *,
    name: str,
    industry: str | None,
    contact_name: str | None,
    contact_email: str | None,
    target_industries: list[str],
    target_locations: list[str],
    targeting_filters: dict,
) -> int:
    """Insert a new client + targeting profile, return the new id.

    Status is fixed to 'active' so the client appears immediately on the
    Clients roster (US-019 surfaces fresh clients via the 'just_added'
    op_state when leads_7d == 0). The schema CHECK constraint only allows
    ('active','paused','churned') — there is no 'onboarding' status.

    Raises psycopg.errors.UniqueViolation if `name` already exists.
    Caller translates that to an inline form error.
    """
    import json
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                insert into crm.clients
                  (name, industry, contact_name, contact_email,
                   target_industries, target_locations, targeting_filters,
                   status, onboarded_at)
                values (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                        'active', now())
                returning id
                """,
                (
                    name,
                    industry or None,
                    contact_name or None,
                    contact_email or None,
                    json.dumps(target_industries),
                    json.dumps(target_locations),
                    json.dumps(targeting_filters),
                ),
            )
            new_id = cur.fetchone()['id']
            cur.execute(
                "insert into crm.activity_log (client_id, action, detail)"
                " values (%s, 'client_created', %s)",
                (
                    new_id,
                    f"{name} — added with {len(target_industries)} "
                    f"industry/industries, {len(target_locations)} location(s)",
                ),
            )
        conn.commit()
    return new_id


def suppress_address(address: str, reason: str = 'manual',
                     added_by: str | None = None,
                     detail: str | None = None) -> bool:
    """Add an address to the suppression list (US-007).

    Idempotent: if the address already exists, returns False without raising.
    Returns True if a new row was created.
    """
    if not address:
        return False
    norm = address.strip()
    if not norm:
        return False
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """insert into crm.suppressed_addresses (address, reason, added_by, detail)
                   values (%s, %s, %s, %s)
                   on conflict ((lower(address))) do nothing""",
                (norm, reason, added_by, detail),
            )
            created = cur.rowcount > 0
        conn.commit()
    return created


def get_client_schedule(client_id: int) -> dict | None:
    return fetch_one(
        "select id, name, daily_pipeline_run_at, pipeline_paused from crm.clients where id = %s",
        (client_id,),
    )


def set_client_schedule(client_id: int, run_at: str | None, paused: bool) -> None:
    """Update the daily pipeline schedule for a client.

    run_at: 'HH:MM' string (Europe/London local) or None to disable.
    paused: True to pause without losing the saved time.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            if run_at:
                cur.execute(
                    "update crm.clients set daily_pipeline_run_at = %s::time, "
                    "pipeline_paused = %s where id = %s",
                    (run_at, paused, client_id),
                )
            else:
                cur.execute(
                    "update crm.clients set daily_pipeline_run_at = null, "
                    "pipeline_paused = %s where id = %s",
                    (paused, client_id),
                )
            cur.execute(
                "insert into crm.activity_log (client_id, action, detail) "
                "values (%s, 'client_schedule_updated', %s)",
                (client_id, f"schedule={run_at or 'off'} paused={paused}"),
            )
        conn.commit()


def client_pipeline_runs(client_id: int, days: int = 30, limit: int = 100) -> list[dict]:
    """Recent pipeline_runs for a client. Newest first. Used by the client
    detail page Pipeline runs section (US-003)."""
    sql = """
        select id, status, mode, leads_added, leads_skipped, leads_errored,
               progress, error_msg,
               started_at, finished_at, created_at,
               case
                 when finished_at is not null and started_at is not null
                   then extract(epoch from (finished_at - started_at))::int
                 else null
               end as duration_s
        from crm.pipeline_runs
        where client_id = %s
          and created_at >= now() - make_interval(days => %s)
        order by created_at desc
        limit %s
    """
    rows = fetch_all(sql, (client_id, days, limit))
    for r in rows:
        # Decorate for the template — relative time + a short error hint.
        if r.get('progress') and isinstance(r['progress'], dict):
            errs = r['progress'].get('errors') or []
            if errs and not r.get('error_msg'):
                r['error_hint'] = errs[0]
            elif r.get('error_msg'):
                r['error_hint'] = r['error_msg']
            else:
                r['error_hint'] = None
        else:
            r['error_hint'] = r.get('error_msg')
    return rows


def update_client(
    client_id: int,
    *,
    name: str,
    industry: str | None,
    contact_name: str | None,
    contact_email: str | None,
    target_industries: list[str],
    target_locations: list[str],
    targeting_filters: dict,
) -> None:
    """Update an existing client's basics + targeting. Idempotent.

    The schema's updated_at trigger only fires on the leads table — clients
    has no updated_at column, so we don't try to maintain one. An activity_log
    entry records the edit for audit instead.
    """
    import json
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                update crm.clients
                set name              = %s,
                    industry          = %s,
                    contact_name      = %s,
                    contact_email     = %s,
                    target_industries = %s::jsonb,
                    target_locations  = %s::jsonb,
                    targeting_filters = %s::jsonb
                where id = %s
                """,
                (
                    name,
                    industry or None,
                    contact_name or None,
                    contact_email or None,
                    json.dumps(target_industries),
                    json.dumps(target_locations),
                    json.dumps(targeting_filters),
                    client_id,
                ),
            )
            if cur.rowcount == 0:
                raise LookupError(f'No client {client_id}')
            cur.execute(
                "insert into crm.activity_log (client_id, action, detail)"
                " values (%s, 'client_updated', %s)",
                (
                    client_id,
                    f"targeting edited — {len(target_industries)} "
                    f"industry/industries, {len(target_locations)} location(s)",
                ),
            )
        conn.commit()


def set_client_outreach_paused(client_id: int, paused: bool) -> dict:
    """Flip outreach_paused, log to activity_log. Returns previous + new state."""
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "select id, name, outreach_paused from crm.clients where id = %s",
                (client_id,),
            )
            row = cur.fetchone()
            if not row:
                raise LookupError(f'No client {client_id}')
            old = bool(row['outreach_paused'])
            if old == paused:
                return {'changed': False, 'paused': old, 'name': row['name']}
            cur.execute(
                "update crm.clients set outreach_paused = %s where id = %s",
                (paused, client_id),
            )
            cur.execute(
                "insert into crm.activity_log (client_id, action, detail)"
                " values (%s, %s, %s)",
                (client_id,
                 'outreach_paused' if paused else 'outreach_resumed',
                 f"{row['name']} — outreach {'paused' if paused else 'resumed'}"),
            )
        conn.commit()
    return {'changed': True, 'paused': paused, 'name': row['name']}


def _outreach_where(extra: list[str], params: dict, client_id: int | None,
                    search: str | None) -> str:
    """Shared WHERE-clause builder for the four outreach lists."""
    if client_id:
        extra.append('e.client_id = %(cid)s'); params['cid'] = client_id
    if search:
        extra.append('(e.subject ilike %(q)s or l.business_name ilike %(q)s)')
        params['q'] = f'%{search}%'
    return ' and '.join(extra) if extra else '1=1'


_OUTREACH_SELECT = """
    select e.id, e.email_number, e.subject, e.status,
           e.scheduled_at, e.sent_at, e.opened_at, e.replied_at,
           e.bounce_reason, e.to_address,
           l.id   as lead_id,    l.business_name,
           c.id   as client_id,  c.name as client_name
      from crm.emails e
      join crm.leads   l on l.id = e.lead_id
      join crm.clients c on c.id = e.client_id
"""


def outreach_today(client_id: int | None = None, search: str | None = None) -> list[dict]:
    """Scheduled sends for today (calendar day, Europe/London).

    Excludes rows still awaiting approval — those live in /approvals,
    not in the Outreach scheduled queue, so the operator doesn't see
    them in two places."""
    extra = [
        "e.status = 'scheduled'",
        "e.needs_approval = false",
        "e.scheduled_at >= date_trunc('day', now() at time zone 'Europe/London') at time zone 'Europe/London'",
        "e.scheduled_at <  (date_trunc('day', now() at time zone 'Europe/London') + interval '1 day') at time zone 'Europe/London'",
    ]
    params: dict = {}
    where = _outreach_where(extra, params, client_id, search)
    rows = fetch_all(_OUTREACH_SELECT + f"where {where} order by e.scheduled_at asc", params)
    for r in rows:
        r['step_label']    = EMAIL_STEP_LABEL.get(r.get('email_number'), '—')
        r['scheduled_hm']  = r['scheduled_at'].strftime('%H:%M') if r.get('scheduled_at') else ''
    return rows


def outreach_sent(client_id: int | None = None, search: str | None = None,
                  limit: int = 200) -> list[dict]:
    """Recently sent emails — last 30 days, capped at `limit` rows.

    Includes dry_run_ready rows (the outreach engine's output while it's
    in dry-run mode) so the operator can verify the engine end-to-end
    without flipping live. Real sends and dry-runs share the same row
    shape; the template decorates dry-run rows with a pill.
    """
    extra = [
        "e.status in ('sent','dry_run_ready')",
        "e.sent_at >= now() - interval '30 days'",
    ]
    params: dict = {'lim': limit}
    where = _outreach_where(extra, params, client_id, search)
    rows = fetch_all(
        _OUTREACH_SELECT + f"where {where} order by e.sent_at desc limit %(lim)s",
        params,
    )
    for r in rows:
        r['step_label'] = EMAIL_STEP_LABEL.get(r.get('email_number'), '—')
        r['sent_at_short'] = r['sent_at'].strftime('%-d %b %H:%M') if r.get('sent_at') else ''
        r['relative'] = relative_time(r['sent_at']) if r.get('sent_at') else ''
        r['is_dry_run'] = (r.get('status') == 'dry_run_ready')
    return rows


def outreach_followups(client_id: int | None = None, search: str | None = None) -> list[dict]:
    """Day-3 / Day-7 emails scheduled within the next 7 days (excluding today).

    Follow-ups inherit approval from the thread (Day 1 already passed the
    operator's gate) so they're always needs_approval=false."""
    extra = [
        "e.status = 'scheduled'",
        "e.needs_approval = false",
        "e.email_number in (2, 3)",
        "e.scheduled_at >= (date_trunc('day', now() at time zone 'Europe/London') + interval '1 day') at time zone 'Europe/London'",
        "e.scheduled_at <  (date_trunc('day', now() at time zone 'Europe/London') + interval '8 days') at time zone 'Europe/London'",
    ]
    params: dict = {}
    where = _outreach_where(extra, params, client_id, search)
    rows = fetch_all(_OUTREACH_SELECT + f"where {where} order by e.scheduled_at asc", params)
    for r in rows:
        r['step_label']     = EMAIL_STEP_LABEL.get(r.get('email_number'), '—')
        r['scheduled_short'] = r['scheduled_at'].strftime('%a %-d %b · %H:%M') if r.get('scheduled_at') else ''
    return rows


def outreach_bounces(client_id: int | None = None, search: str | None = None,
                     limit: int = 200) -> list[dict]:
    """All bounced sends, newest first."""
    extra = ["e.status = 'bounced'"]
    params: dict = {'lim': limit}
    where = _outreach_where(extra, params, client_id, search)
    rows = fetch_all(
        _OUTREACH_SELECT + f"where {where} order by coalesce(e.sent_at, e.created_at) desc limit %(lim)s",
        params,
    )
    for r in rows:
        r['step_label']    = EMAIL_STEP_LABEL.get(r.get('email_number'), '—')
        when = r.get('sent_at')
        r['bounced_short'] = when.strftime('%-d %b %H:%M') if when else '—'
        r['relative']      = relative_time(when) if when else ''
        # First word before the em-dash → severity (Hard / Soft) for the pill
        reason = (r.get('bounce_reason') or '').strip()
        head   = reason.split('—', 1)[0].strip().lower() if '—' in reason else ''
        r['severity'] = head if head in ('hard', 'soft') else 'unknown'
    return rows


# ── Approvals (Week 2) ───────────────────────────────────────────────
# The gate between drafted Day-1 emails and the outreach engine. New
# drafts default to needs_approval=true (column default in migration
# 0018). Day-3 / Day-7 follow-ups inherit approval from the thread.
#
# The approvals queue surfaces rows where status='scheduled' AND
# needs_approval=true. The outreach engine refuses to ship them until
# approved. Bulk approve flips needs_approval=false + stamps approved_at;
# the engine picks them up on its next tick.

# ── Campaigns (Batch A) ──────────────────────────────────────────────
# A Campaign ties (client, hook_type) to a collection of saved templates.
# The pipeline runner resolves which campaign a freshly-drafted email
# belongs to BEFORE calling the drafter, so the drafter can seed Claude
# with the campaign's best-performing template for this step. The
# Campaigns UI surfaces approvals + scheduled + sent + bounces per
# campaign (the Outreach page merges into here).


def campaigns_for_client(client_id: int) -> list[dict]:
    """All non-archived campaigns for one client, with rolled-up KPIs.
    KPIs are computed from crm.emails over the last 30 days."""
    return fetch_all("""
        select
          c.id, c.name, c.hook_type, c.status,
          (select count(*) from crm.emails e
            where e.campaign_id = c.id
              and e.sent_at >= now() - interval '30 days'
              and e.status in ('sent','dry_run_ready')) as sent_30d,
          (select count(*) from crm.emails e
            join crm.replies r on r.email_id = e.id
            where e.campaign_id = c.id
              and r.detected_at >= now() - interval '30 days') as replied_30d,
          (select count(*) from crm.emails e
            where e.campaign_id = c.id
              and e.status = 'scheduled'
              and e.needs_approval = true) as pending_approval,
          (select count(*) from crm.campaign_templates t
            where t.campaign_id = c.id) as template_count
        from crm.campaigns c
        where c.client_id = %s
          and c.status != 'archived'
        order by c.hook_type nulls first, c.name
    """, (client_id,))


def find_or_create_campaign(client_id: int, hook_type: str | None,
                            *, name: str | None = None) -> int:
    """Get the active campaign for (client, hook_type), creating one if
    missing. Called by the pipeline runner before drafting; cheap (a
    single read for the common case, an insert only on the cold path).

    Names default to 'Hook · <hook_type>' or 'Default — all hooks' so
    the campaign list reads sensibly without operator setup.
    """
    existing = fetch_one("""
        select id from crm.campaigns
         where client_id = %s
           and coalesce(hook_type, '') = coalesce(%s, '')
           and status != 'archived'
         limit 1
    """, (client_id, hook_type))
    if existing:
        return int(existing["id"])

    if not name:
        if hook_type:
            name = f"Hook · {hook_type.replace('_', ' ')}"
        else:
            name = "Default — all hooks"

    row = fetch_one("""
        insert into crm.campaigns (client_id, name, hook_type, status)
        values (%s, %s, %s, 'active')
        returning id
    """, (client_id, name, hook_type))
    return int(row["id"]) if row else 0


def best_template(campaign_id: int, step: int) -> dict | None:
    """Pick the template the drafter should seed with.

    Order: explicit default first, then highest reply rate among saved
    templates with at least 3 uses, else None (drafter writes cold)."""
    default = fetch_one("""
        select id, subject_template, body_template, times_used, times_replied
          from crm.campaign_templates
         where campaign_id = %s and step = %s and is_default = true
         limit 1
    """, (campaign_id, step))
    if default:
        return default
    ranked = fetch_one("""
        select id, subject_template, body_template, times_used, times_replied
          from crm.campaign_templates
         where campaign_id = %s and step = %s and times_used >= 3
         order by (times_replied::float / nullif(times_used, 0)) desc nulls last,
                  times_used desc
         limit 1
    """, (campaign_id, step))
    return ranked


def save_email_as_template(email_id: int, *, set_as_default: bool = True,
                           operator: str = 'operator') -> dict:
    """Promote an approved draft to a saved campaign template.

    Reads the email's (campaign_id, email_number, subject, body), inserts
    a row into crm.campaign_templates, and optionally sets it as the
    default for that (campaign_id, step). Defaults are exclusive — if
    set_as_default=True, any other default for the same step is demoted.
    """
    email = fetch_one("""
        select id, campaign_id, email_number, subject, body
          from crm.emails
         where id = %s
    """, (email_id,))
    if not email:
        raise LookupError(f'No email {email_id}')
    if not email.get("campaign_id"):
        raise ValueError('email_has_no_campaign')
    if not email.get("subject") or not email.get("body"):
        raise ValueError('email_missing_content')

    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            if set_as_default:
                cur.execute("""
                    update crm.campaign_templates
                       set is_default = false
                     where campaign_id = %s and step = %s and is_default = true
                """, (email["campaign_id"], email["email_number"]))
            cur.execute("""
                insert into crm.campaign_templates
                  (campaign_id, step, subject_template, body_template,
                   is_default, source_email_id)
                values (%s, %s, %s, %s, %s, %s)
                returning id
            """, (
                email["campaign_id"],
                email["email_number"],
                email["subject"],
                email["body"],
                set_as_default,
                email_id,
            ))
            template_id = cur.fetchone()["id"]
            cur.execute(
                "insert into crm.activity_log (client_id, lead_id, action, detail) "
                "values ((select client_id from crm.emails where id = %s), "
                "        (select lead_id from crm.emails where id = %s), "
                "        'template_saved', %s)",
                (email_id, email_id,
                 f"template {template_id} saved from email {email_id} by {operator}"),
            )
        conn.commit()
    return {"template_id": int(template_id), "campaign_id": int(email["campaign_id"])}


def approvals_pending(client_id: int | None = None, search: str | None = None,
                      limit: int = 200) -> list[dict]:
    """Drafts waiting on a human. Newest first — the operator catches up
    on the most recent pipeline run first."""
    extra = [
        "e.status = 'scheduled'",
        "e.needs_approval = true",
    ]
    params: dict = {'lim': limit}
    where = _outreach_where(extra, params, client_id, search)
    rows = fetch_all(f"""
        select e.id, e.email_number, e.subject, e.body, e.to_address,
               e.scheduled_at, e.created_at,
               l.id as lead_id, l.business_name, l.decision_maker_name,
               l.decision_maker_title, l.hook_type, l.grade,
               c.id as client_id, c.name as client_name
          from crm.emails e
          join crm.leads   l on l.id = e.lead_id
          join crm.clients c on c.id = e.client_id
         where {where}
         order by e.created_at desc
         limit %(lim)s
    """, params)
    for r in rows:
        r['relative'] = relative_time(r['created_at']) if r.get('created_at') else ''
        body = (r.get('body') or '').strip()
        # Snippet: first 220 chars of body, single-spaced. The full body
        # is on the row for the inline editor; this is for the list view.
        r['snippet'] = ' '.join(body.split())[:220]
    return rows


def approvals_count_per_client() -> list[dict]:
    """Per-client pending counts for the client filter pills.

    Returns only clients that have at least one pending row — keeps the
    pill row tight and avoids zero-count clutter."""
    return fetch_all("""
        select c.id, c.name, count(*) as pending
          from crm.emails e
          join crm.clients c on c.id = e.client_id
         where e.status = 'scheduled'
           and e.needs_approval = true
         group by c.id, c.name
         order by pending desc, c.name asc
    """)


def approvals_total() -> int:
    """Total pending approvals across all clients — used by the sidebar
    badge and the page header count."""
    row = fetch_one("""
        select count(*) as n
          from crm.emails
         where status = 'scheduled'
           and needs_approval = true
    """)
    return int(row['n']) if row else 0


def approvals_bulk_approve(email_ids: list[int],
                           operator: str = 'operator') -> dict:
    """Flip needs_approval=false on N rows. Stamps approved_at/approved_by
    and writes an activity_log row per email. Returns {approved: int}.

    Idempotent: rows already approved are no-ops (the where clause
    filters them out)."""
    if not email_ids:
        return {'approved': 0}
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                update crm.emails
                   set needs_approval = false,
                       approved_at    = now(),
                       approved_by    = %s
                 where id = any(%s)
                   and needs_approval = true
                   and status = 'scheduled'
                returning id, lead_id, client_id
            """, (operator, email_ids))
            updated = cur.fetchall()
            for r in updated:
                cur.execute(
                    "insert into crm.activity_log (client_id, lead_id, action, detail) "
                    "values (%s, %s, 'email_approved', %s)",
                    (r['client_id'], r['lead_id'], f"Day-1 draft approved (email {r['id']})"),
                )
        conn.commit()
    return {'approved': len(updated)}


def approvals_skip(email_id: int, operator: str = 'operator',
                   reason: str = 'operator_skipped') -> dict:
    """Cancel a queued draft without sending. Used by the Skip action.

    The lead stays in 'new' status — the operator just decided this one
    draft wasn't worth sending; they can re-run the pipeline or change
    the lead status manually."""
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                update crm.emails
                   set status        = 'cancelled',
                       cancel_reason = %s,
                       skip_reason   = %s,
                       approved_at   = now(),
                       approved_by   = %s
                 where id = %s
                   and status = 'scheduled'
                returning id, lead_id, client_id
            """, (reason, reason, operator, email_id))
            row = cur.fetchone()
            if not row:
                raise LookupError(f'No pending email {email_id}')
            cur.execute(
                "insert into crm.activity_log (client_id, lead_id, action, detail) "
                "values (%s, %s, 'email_skipped', %s)",
                (row['client_id'], row['lead_id'], f"draft skipped — {reason}"),
            )
        conn.commit()
    return {'skipped': 1, 'email_id': email_id}


def approvals_edit_and_approve(email_id: int, subject: str, body: str,
                               operator: str = 'operator') -> dict:
    """Update subject + body, then approve in one transaction. The most
    common flow when a draft is 80% right and needs a line tweaked."""
    if not (subject or '').strip():
        raise ValueError('subject_required')
    if not (body or '').strip():
        raise ValueError('body_required')
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                update crm.emails
                   set subject        = %s,
                       body           = %s,
                       needs_approval = false,
                       approved_at    = now(),
                       approved_by    = %s
                 where id = %s
                   and needs_approval = true
                   and status = 'scheduled'
                returning id, lead_id, client_id
            """, (subject.strip(), body.strip(), operator, email_id))
            row = cur.fetchone()
            if not row:
                raise LookupError(f'No pending email {email_id}')
            cur.execute(
                "insert into crm.activity_log (client_id, lead_id, action, detail) "
                "values (%s, %s, 'email_edited_approved', %s)",
                (row['client_id'], row['lead_id'],
                 f"draft edited and approved (email {row['id']})"),
            )
        conn.commit()
    return {'approved': 1, 'email_id': email_id}


def leads_for_csv(
    *,
    status: str | None = None,
    client_id: int | None = None,
    search: str | None = None,
) -> list[dict]:
    """Same filters as the list page, no pagination — for CSV export."""
    where = ['1=1']
    params: dict = {}
    if status and status in LEAD_STATUSES:
        where.append('l.status = %(status)s'); params['status'] = status
    if client_id:
        where.append('l.client_id = %(client_id)s'); params['client_id'] = client_id
    if search:
        where.append('(l.business_name ilike %(q)s or l.decision_maker_name ilike %(q)s)')
        params['q'] = f'%{search}%'
    return fetch_all(f"""
        select l.business_name, c.name as client_name, l.grade, l.status,
               l.overall_score, l.google_review_count, l.decision_maker_name,
               l.email, l.phone, l.website, l.city,
               l.created_at
          from crm.leads l
          left join crm.clients c on c.id = l.client_id
         where {' and '.join(where)}
         order by l.created_at desc
    """, params)


# ── Inbound (Step 8) ─────────────────────────────────────────────────
# Pipeline state model:
#   score   ∈ {hot, warm, cold}             — qualifier verdict from form
#   status  ∈ {new, contacted, called,      — manual triage state
#              proposal, won, lost}
# Tab mapping (UI → SQL):
#   all       → no filter
#   hot       → score = 'hot'
#   new       → status = 'new'
#   progress  → status in (contacted, called, proposal)
#   closed    → status in (won, lost)

INBOUND_TABS         = ('all', 'hot', 'new', 'progress', 'closed')
INBOUND_SCORES       = ('hot', 'warm', 'cold')
INBOUND_STATUSES     = ('new', 'contacted', 'called', 'proposal', 'won', 'lost')
INBOUND_STATUS_LABEL = {
    'new':       'New',
    'contacted': 'Contacted',
    'called':    'Booked',
    'proposal':  'Proposal',
    'won':       'Won',
    'lost':      'Lost',
}
# Statuses that count as "in motion" (not new, not closed)
INBOUND_PROGRESS_STATUSES = ('contacted', 'called', 'proposal')
INBOUND_CLOSED_STATUSES   = ('won', 'lost')


def _inbound_local_fixture() -> list[dict]:
    """Mirror of migrations/0004 — used when DATABASE_URL is unset so the
    POC localhost demo renders without a Supabase connection. Order
    matters: row #1 is the most recent, #14 the oldest. The seed SQL
    inserts the same set in the same order with `now() - interval`,
    so the production page (with the migration applied) renders the
    same content with real timestamps.
    """
    now  = datetime.now(timezone.utc)
    h    = lambda hours:   now - _td(hours=hours)
    d    = lambda days:    now - _td(days=days)
    return [
        # ── Hot ──
        dict(id=1,  name='Marcus Webb',    company='Webb & Co Solicitors',
             email='marcus.webb@webbco.uk',     phone='+44 20 7946 0312',
             industry='Legal services',         deal_value='£5-10k',
             current_method='Word of mouth and a referral partner — ad hoc, no system.',
             clients_wanted='4-6 a month',
             score='hot',  status='new',
             auto_response_sent=True,  auto_response_sent_at=h(3) + _td(seconds=11),
             notes='[demo] Read ROCA case study before submitting.',
             created_at=h(3)),
        dict(id=2,  name='Sarah Patel',    company='Verde Strategy',
             email='sarah@verde-strategy.co.uk', phone='+44 161 408 2210',
             industry='Strategy consulting',     deal_value='£2-5k',
             current_method='LinkedIn outbound by hand, ~30 messages a week, low conversion.',
             clients_wanted='3 a month',
             score='hot',  status='new',
             auto_response_sent=True,  auto_response_sent_at=h(5) + _td(seconds=14),
             notes='[demo]',
             created_at=h(5)),
        dict(id=3,  name='David Kim',      company='Kim Architects',
             email='david@kimarch.co.uk',       phone='+44 117 902 4456',
             industry='Architecture',            deal_value='£2-5k',
             current_method='Cold calls + Architects Journal directory listings.',
             clients_wanted='2 a month',
             score='hot',  status='contacted',
             auto_response_sent=True,  auto_response_sent_at=h(18) + _td(seconds=9),
             notes='[demo] Replied with a meeting request — booking pending.',
             created_at=h(18)),
        dict(id=4,  name='Olivia Bennett', company='Bennett & Cole Accountants',
             email='olivia@bennettcole.co.uk',  phone='+44 113 555 8821',
             industry='Accountancy',             deal_value='£2-5k',
             current_method='Referrals plus paid Google ads — CPL is climbing.',
             clients_wanted='5 a month',
             score='hot',  status='called',
             auto_response_sent=True,  auto_response_sent_at=d(2) + _td(seconds=13),
             notes='[demo] Discovery call done — fits ROCA mould.',
             created_at=d(2)),
        # ── Warm ──
        dict(id=5,  name='Rachel Hughes',  company='Hughes Recruitment',
             email='rachel@hughesrecruit.co.uk', phone='+44 151 408 7733',
             industry='Recruitment',             deal_value='£2-5k',
             current_method='LinkedIn Recruiter + cold email on Apollo.',
             clients_wanted='6-8 a month',
             score='warm', status='proposal',
             auto_response_sent=True,  auto_response_sent_at=d(6) + _td(seconds=10),
             notes='[demo] Proposal sent — chasing for sign-off this week.',
             created_at=d(6)),
        dict(id=6,  name='James Whitfield',company='Whitfield Wealth Advisors',
             email='james@whitfieldwealth.co.uk',phone='+44 131 558 4490',
             industry='Financial advisory',      deal_value='£5-10k',
             current_method='Print ads in The Scotsman + a quarterly seminar.',
             clients_wanted='2 a month',
             score='warm', status='won',
             auto_response_sent=True,  auto_response_sent_at=d(12) + _td(seconds=17),
             notes='[demo] Signed £3.5k retainer — Sammy is lead.',
             created_at=d(12)),
        dict(id=7,  name="Daniel O'Brien", company="O'Brien & Murphy LLP",
             email='daniel@obrienmurphy.co.uk', phone='+44 20 7100 5544',
             industry='Legal services',          deal_value='£5-10k',
             current_method='Repeat clients only — zero outbound.',
             clients_wanted='3 a month',
             score='warm', status='contacted',
             auto_response_sent=True,  auto_response_sent_at=d(3) + _td(seconds=12),
             notes='[demo] Sent the legal-vertical case study.',
             created_at=d(3)),
        dict(id=8,  name='Hannah Wright',  company='Wright Bookkeeping',
             email='hannah@wrightbooks.co.uk',  phone='+44 29 2055 1190',
             industry='Accountancy / bookkeeping', deal_value='£1-2k',
             current_method='Yell + Facebook page, mostly local search.',
             clients_wanted='3 a month',
             score='warm', status='new',
             auto_response_sent=True,  auto_response_sent_at=h(9) + _td(seconds=15),
             notes='[demo]',
             created_at=h(9)),
        dict(id=9,  name='Liam Foster',    company='Foster Marketing',
             email='liam@fostermarketing.co.uk',phone='+44 114 282 9905',
             industry='Marketing agency',        deal_value='£2-5k',
             current_method='Inbound from existing portfolio + LinkedIn content.',
             clients_wanted='4 a month',
             score='warm', status='contacted',
             auto_response_sent=True,  auto_response_sent_at=d(4) + _td(seconds=8),
             notes='[demo] Slightly cautious — competitor in our stack.',
             created_at=d(4)),
        dict(id=10, name='Charlotte Reed', company='Reed & Park Insurance Brokers',
             email='charlotte@reedpark.co.uk',  phone='+44 161 408 2188',
             industry='Insurance brokerage',     deal_value='£5-10k',
             current_method='Networking + a paid SEO retainer.',
             clients_wanted='5 a month',
             score='warm', status='called',
             auto_response_sent=True,  auto_response_sent_at=d(5) + _td(seconds=11),
             notes='[demo] Discovery call done — sending proposal Monday.',
             created_at=d(5)),
        dict(id=11, name='Amara Okonkwo',  company='Okonkwo Cardiology',
             email='amara@okonkwocardio.co.uk', phone='+44 20 7946 8842',
             industry='Private healthcare',      deal_value='£5-10k',
             current_method='GP referrals + a small Google Ads budget.',
             clients_wanted='2-3 a month',
             score='warm', status='new',
             auto_response_sent=True,  auto_response_sent_at=h(11) + _td(seconds=12),
             notes='[demo]',
             created_at=h(11)),
        # ── Cold ──
        dict(id=12, name='Tom Davies',     company='Solo IT',
             email='tom@soloit.uk',             phone='+44 121 555 0099',
             industry='IT support (1-person)',   deal_value='<£1k',
             current_method='Word of mouth — looking for £500/mo packages.',
             clients_wanted='1 a month',
             score='cold', status='lost',
             auto_response_sent=True,  auto_response_sent_at=d(8) + _td(seconds=14),
             notes='[demo] DQ — budget below floor.',
             created_at=d(8)),
        dict(id=13, name='Priya Nair',     company='Nair Design Studio',
             email='priya@nairdesign.co.uk',    phone='+44 20 7946 1133',
             industry='Design agency',           deal_value='£1-2k',
             current_method='Behance + Dribbble + referrals.',
             clients_wanted='2 a month',
             score='cold', status='lost',
             auto_response_sent=False, auto_response_sent_at=None,
             notes='[demo] DQ — wrong segment, no auto-reply triggered.',
             created_at=d(14)),
        dict(id=14, name='Ben Holloway',   company='Holloway Construction',
             email='ben@hollowayconstruct.uk', phone='+44 191 408 5566',
             industry='Construction',            deal_value='£2-5k',
             current_method='Trade press + tender platforms.',
             clients_wanted='1 a month',
             score='cold', status='new',
             auto_response_sent=False, auto_response_sent_at=None,
             notes='[demo] No auto-reply triggered — qualifier flagged segment.',
             created_at=h(6)),
    ]


# datetime helpers used only by the fixture above
from datetime import timedelta as _td


def _inbound_use_fixture() -> bool:
    """True when no DATABASE_URL — POC local demo uses in-memory rows."""
    return not DATABASE_URL


# ── Replies fixture (for /inbox demo without DB) ─────────────────────
# Mirrors crm.replies + parent lead/client so the reply drawer (US-024)
# is demoable on localhost without a Supabase connection. Tied tightly
# to a small set of leads so the thread fetch can compose realistic
# Day 1 / Day 3 outbound bodies for context.

def _replies_local_fixture() -> list[dict]:
    now = datetime.now(timezone.utc)
    h = lambda hours: now - _td(hours=hours)
    d = lambda days:  now - _td(days=days)
    return [
        # Needs you — positive reply, fresh
        dict(id=101, lead_id=9001, client_id=1, processed=False,
             from_address='sarah@coleandreeves.co.uk',
             subject='Re: Helping Cole & Reeves cut prospecting time',
             body=(
                 "Hi Sammy,\n\n"
                 "This is on point — we're spending way too long on outbound "
                 "and the conversion is patchy. The ROCA case study is "
                 "exactly the kind of result I'd want.\n\n"
                 "Happy to do a 25-min intro this week. Tuesday or Thursday "
                 "afternoon work?\n\n"
                 "Sarah"
             ),
             sentiment='positive', detected_at=h(2),
             business_name='Cole & Reeves Accountants',
             decision_maker_name='Sarah Cole',
             decision_maker_email='sarah@coleandreeves.co.uk',
             lead_status='replied',
             client_name='ROCA Accountants'),
        # Needs you — neutral / question
        dict(id=102, lead_id=9002, client_id=2, processed=False,
             from_address='james@whitfordproperty.co.uk',
             subject='Re: Whitford — qualified leads on autopilot',
             body=(
                 "Thanks for reaching out. Before I commit to a call, can "
                 "you send across pricing and the kind of volume you typically "
                 "deliver in property?\n\n"
                 "Cheers, James"
             ),
             sentiment='neutral', detected_at=h(7),
             business_name='Whitford Property Group',
             decision_maker_name='James Whitford',
             decision_maker_email='james@whitfordproperty.co.uk',
             lead_status='replied',
             client_name='Vidora Media'),
        # Done — already processed (becomes a meeting)
        dict(id=103, lead_id=9003, client_id=1, processed=True,
             from_address='nina@harborlegal.co.uk',
             subject='Re: Harbor Legal — three new instructions a month',
             body=(
                 "Booked you in for Wednesday at 14:00. Looking forward to it.\n\n"
                 "Nina"
             ),
             sentiment='positive', detected_at=d(3),
             business_name='Harbor Legal',
             decision_maker_name='Nina Patel',
             decision_maker_email='nina@harborlegal.co.uk',
             lead_status='meeting',
             client_name='ROCA Accountants'),
    ]


def _reply_thread_fixture(reply_id: int) -> dict | None:
    """Compose a realistic thread for a fixture reply: two outbound
    messages (Day 1 + Day 3) followed by the lead's reply. Used only
    when DATABASE_URL is unset."""
    rows = {r['id']: r for r in _replies_local_fixture()}
    head = rows.get(reply_id)
    if not head:
        return None

    sending = settings_get('sending_email') or {}
    from_addr = sending.get('from_address') or 'sammy@innoviteai.com'
    detected = head['detected_at']
    day1_at  = detected - _td(days=4, hours=2)
    day3_at  = detected - _td(days=1, hours=4)

    business = head['business_name']
    name     = head['decision_maker_name'].split(' ')[0]

    day1_body = (
        f"Hi {name},\n\n"
        f"Saw {business} comes up at the top of search for your area — "
        f"impressive presence. Quick context: we're an outbound system "
        f"built for service firms in your sector. Our ROCA engagement "
        f"is doing 3× pipeline in 60 days, zero hours of prospecting "
        f"on their side.\n\n"
        f"Worth a 25-min intro to see if the shape fits {business}?\n\n"
        f"— Sammy"
    )
    day3_body = (
        f"Hi {name},\n\n"
        f"Just bumping the note from earlier — happy to send the ROCA "
        f"deck across first if useful, or jump straight to a call.\n\n"
        f"— Sammy"
    )

    messages = [
        {'kind':'out', 'subject': head['subject'].replace('Re: ', ''),
         'body': day1_body, 'from_address': from_addr,
         'to_address': head['decision_maker_email'],
         'at': day1_at.isoformat(), 'relative': relative_time(day1_at),
         'step': 'Day 1'},
        {'kind':'out', 'subject': '',
         'body': day3_body, 'from_address': from_addr,
         'to_address': head['decision_maker_email'],
         'at': day3_at.isoformat(), 'relative': relative_time(day3_at),
         'step': 'Day 3'},
        {'kind':'in', 'subject': head['subject'],
         'body': head['body'], 'from_address': head['from_address'],
         'at': detected.isoformat(), 'relative': relative_time(detected),
         'sentiment': head['sentiment']},
    ]

    head_subject = head['subject']
    reply_subject = head_subject if head_subject.lower().startswith('re:') else f'Re: {head_subject}'
    quoted = '\n\n'.join(f'> {line}' for line in (head['body'] or '').splitlines())

    return {
        'reply_id':       head['id'],
        'lead_id':        head['lead_id'],
        'client_id':      head['client_id'],
        'client_name':    head['client_name'],
        'business_name':  head['business_name'],
        'lead_name':      head['decision_maker_name'],
        'lead_status':    head['lead_status'],
        'sentiment':      head['sentiment'],
        'reply_relative': relative_time(detected),
        'processed':      head['processed'],
        'messages':       messages,
        'composer': {
            'from':    from_addr,
            'to':      head['from_address'],
            'subject': reply_subject,
            'quoted':  quoted,
        },
    }


# ── Inbox (Epic 9 / US-022) ───────────────────────────────────────────
# One page that answers "did anything come back?" — unifies outbound
# replies (crm.replies) and inbound form submissions (crm.inbound_leads)
# into a single shape. Drafts tab is stubbed — AI auto-reply generation
# is a separate story when the backend lands.
INBOX_TABS = ('needs_you', 'done')  # Drafts tab deferred until AI auto-reply backend exists (US-023)


def _inbox_snippet(text: str | None, limit: int = 140) -> str:
    if not text:
        return ''
    s = ' '.join(text.split())
    return s if len(s) <= limit else s[:limit - 1].rstrip() + '…'


def _reply_to_inbox_item(r: dict) -> dict:
    """Normalise a crm.replies + parent lead/client row into the inbox shape.

    Orphan replies (no matched lead_id) still render — the matcher didn't
    find an outbound to attach them to, so the operator handles them
    manually. We badge them 'Orphan' and link to /inbox itself rather
    than a 404'ing lead page.
    """
    sentiment = r.get('sentiment') or 'neutral'
    is_orphan = r.get('lead_id') is None
    return {
        'kind':         'reply',
        'id':           f"r:{r['id']}",
        'href':         '/inbox' if is_orphan else f"/leads/{r.get('lead_id')}",
        'display_name': (r.get('decision_maker_name')
                         or r.get('business_name')
                         or r.get('from_address')
                         or 'Unknown sender'),
        'company':      r.get('business_name') or ('Orphan reply' if is_orphan else ''),
        'subject':      r.get('subject') or '',
        'snippet':      _inbox_snippet(r.get('body')),
        'received_at':  r.get('detected_at'),
        'relative':     relative_time(r['detected_at']) if r.get('detected_at') else '—',
        'signal_label': 'Orphan' if is_orphan else sentiment.capitalize(),
        'signal_class': 'orphan' if is_orphan else sentiment,
        'client_name':  r.get('client_name') or '',
        'lead_status':  r.get('lead_status') or ('orphan' if is_orphan else ''),
    }


def _form_to_inbox_item(r: dict) -> dict:
    """Normalise a crm.inbound_leads row into the inbox shape."""
    score = r.get('score') or 'cold'
    msg   = r.get('current_method') or r.get('notes') or 'Form submission from innoviteai.com'
    return {
        'kind':         'form',
        'id':           f"f:{r['id']}",
        'href':         f"/inbox#form-{r['id']}",
        'display_name': r.get('name') or 'Anonymous',
        'company':      r.get('company') or '',
        'subject':      'Submitted via innoviteai.com',
        'snippet':      _inbox_snippet(msg),
        'received_at':  r.get('created_at'),
        'relative':     relative_time(r['created_at']) if r.get('created_at') else '—',
        'signal_label': score.capitalize(),
        'signal_class': score,                # hot / warm / cold
        'client_name':  '',
        'inbound_id':   r.get('id'),
    }


def inbox_tab_counts() -> dict:
    """Counts for the visible Inbox tabs (needs_you, done).

    Drafts tab deferred until AI auto-reply backend exists (US-023)."""
    counts = {'needs_you': 0, 'done': 0}
    if _inbound_use_fixture():
        forms = _inbound_local_fixture()
        replies = _replies_local_fixture()
        counts['needs_you'] = (
            sum(1 for r in forms   if r.get('status') == 'new')
            + sum(1 for r in replies if r.get('lead_status') == 'replied')
        )
        counts['done'] = (
            sum(1 for r in forms   if r.get('status') != 'new')
            + sum(1 for r in replies if r.get('lead_status') in ('meeting','won','lost'))
        )
        return counts
    try:
        rep = fetch_one("""
            select
              count(*) filter (where l.status = 'replied')                       as needs_you,
              count(*) filter (where l.status in ('meeting','won','lost'))       as done
              from crm.replies r
              join crm.leads   l on l.id = r.lead_id
        """) or {}
        forms = fetch_one("""
            select
              count(*) filter (where status = 'new')                             as needs_you,
              count(*) filter (where status in ('contacted','called','proposal','won','lost'))
                                                                                  as done
              from crm.inbound_leads
        """) or {}
        counts['needs_you'] = int(rep.get('needs_you') or 0) + int(forms.get('needs_you') or 0)
        counts['done']      = int(rep.get('done')      or 0) + int(forms.get('done')      or 0)
    except Exception:
        # Schema gap during early POC — fall back to forms-only.
        pass
    return counts


def inbox_items(*, tab: str = 'needs_you', limit: int = 200) -> list[dict]:
    """Unified inbox: replies + form submissions.

    Drafts tab deferred until AI auto-reply backend exists (US-023);
    anything that arrives with tab='drafts' is normalised to 'needs_you'
    via the INBOX_TABS membership check below."""
    if tab not in INBOX_TABS:
        tab = 'needs_you'
    if _inbound_use_fixture():
        forms = _inbound_local_fixture()
        replies = _replies_local_fixture()
        if tab == 'needs_you':
            forms   = [r for r in forms   if r.get('status') == 'new']
            replies = [r for r in replies if r.get('lead_status') == 'replied']
        else:
            forms   = [r for r in forms   if r.get('status') != 'new']
            replies = [r for r in replies if r.get('lead_status') in ('meeting','won','lost')]
        items: list[dict] = []
        items.extend(_reply_to_inbox_item(r) for r in replies)
        items.extend(_form_to_inbox_item(_inbound_decorate(dict(r))) for r in forms)
        items.sort(key=lambda x: x.get('received_at') or datetime.min, reverse=True)
        return items[:limit]

    # Replies — left-join through to lead + client so orphan replies
    # (no matched lead, from the reply engine matcher) still surface in
    # 'Needs you' for manual triage.
    if tab == 'needs_you':
        reply_where = "(l.status = 'replied' or r.lead_id is null)"
        form_where  = "status = 'new'"
    else:  # done
        reply_where = "l.status in ('meeting','won','lost')"
        form_where  = "status in ('contacted','called','proposal','won','lost')"

    items: list[dict] = []
    try:
        replies = fetch_all(f"""
            select r.id, r.lead_id, r.subject, r.body, r.sentiment, r.detected_at,
                   r.from_address,
                   l.business_name, l.decision_maker_name, l.status as lead_status,
                   l.client_id, c.name as client_name
              from crm.replies r
              left join crm.leads   l on l.id = r.lead_id
              left join crm.clients c on c.id = l.client_id
             where {reply_where}
             order by r.detected_at desc
             limit %(lim)s
        """, {'lim': limit})
        items.extend(_reply_to_inbox_item(r) for r in replies)
    except Exception:
        pass

    try:
        forms = fetch_all(f"""
            select id, name, company, email, score, status, current_method,
                   notes, created_at
              from crm.inbound_leads
             where {form_where}
             order by created_at desc
             limit %(lim)s
        """, {'lim': limit})
        items.extend(_form_to_inbox_item(r) for r in forms)
    except Exception:
        pass

    items.sort(key=lambda x: x.get('received_at') or datetime.min, reverse=True)
    return items[:limit]


def reply_thread(reply_id: int) -> dict | None:
    """Full conversation around a single inbound reply. Returns lead +
    client metadata for the drawer header, the latest reply (the one the
    operator clicked), and a chronological message list mixing sent
    outbound emails (`crm.emails`) with received replies (`crm.replies`).

    Returns None if the reply id is not found. Used by US-024
    (reply-from-Inbox flow) — the drawer renders the thread above the
    composer so the operator sees the full back-and-forth in context.
    """
    if _inbound_use_fixture():
        return _reply_thread_fixture(reply_id)

    head = fetch_one("""
        select r.id, r.lead_id, r.from_address, r.subject, r.body,
               r.sentiment, r.detected_at, r.processed,
               l.business_name, l.decision_maker_name,
               l.decision_maker_email, l.status as lead_status,
               l.client_id, c.name as client_name
          from crm.replies r
          join crm.leads   l on l.id = r.lead_id
          join crm.clients c on c.id = l.client_id
         where r.id = %(rid)s
    """, {'rid': reply_id})
    if not head:
        return None

    lead_id = head['lead_id']
    sent = fetch_all("""
        select id, subject, body, from_address, to_address,
               sent_at, email_number
          from crm.emails
         where lead_id = %(lid)s and sent_at is not null
         order by sent_at asc
    """, {'lid': lead_id}) or []
    received = fetch_all("""
        select id, subject, body, from_address, detected_at, sentiment
          from crm.replies
         where lead_id = %(lid)s
         order by detected_at asc
    """, {'lid': lead_id}) or []

    messages: list[dict] = []
    for e in sent:
        ts = e.get('sent_at')
        messages.append({
            'kind':         'out',
            'subject':      e.get('subject') or '',
            'body':         e.get('body') or '',
            'from_address': e.get('from_address') or '',
            'to_address':   e.get('to_address') or '',
            'at':           ts.isoformat() if ts else None,
            'relative':     relative_time(ts) if ts else '—',
            'step':         f"Day {1 if e.get('email_number') == 1 else 3 if e.get('email_number') == 2 else 7}",
        })
    for r in received:
        ts = r.get('detected_at')
        messages.append({
            'kind':         'in',
            'subject':      r.get('subject') or '',
            'body':         r.get('body') or '',
            'from_address': r.get('from_address') or '',
            'at':           ts.isoformat() if ts else None,
            'relative':     relative_time(ts) if ts else '—',
            'sentiment':    r.get('sentiment') or 'neutral',
        })
    messages.sort(key=lambda m: m.get('at') or '')

    head_subject = head.get('subject') or ''
    reply_subject = head_subject if head_subject.lower().startswith('re:') else f'Re: {head_subject}'
    quoted = '\n\n'.join(f'> {line}' for line in (head.get('body') or '').splitlines())

    sending = settings_get('sending_email') or {}
    return {
        'reply_id':       head['id'],
        'lead_id':        lead_id,
        'client_id':      head['client_id'],
        'client_name':    head.get('client_name') or '',
        'business_name':  head.get('business_name') or '',
        'lead_name':      head.get('decision_maker_name') or head.get('business_name') or 'Unknown',
        'lead_status':    head.get('lead_status') or '',
        'sentiment':      head.get('sentiment') or 'neutral',
        'reply_relative': relative_time(head['detected_at']) if head.get('detected_at') else '—',
        'processed':      bool(head.get('processed')),
        'messages':       messages,
        'composer': {
            'from':    sending.get('from_address') or '',
            'to':      head.get('from_address') or head.get('decision_maker_email') or '',
            'subject': reply_subject,
            'quoted':  quoted,
        },
    }


def reply_send(reply_id: int, *, body: str, subject: str,
               status_action: str | None = None) -> dict:
    """Record a manual reply sent from the Inbox composer (US-024).

    POC scope: writes to `crm.emails` (kind isn't a constraint, status is —
    we use 'sent' and set sent_at), marks the originating reply processed,
    optionally advances the lead status, writes an activity_log entry.
    Actual SMTP send is deferred until the email-engine backend lands;
    when it does, this function is the single insertion point.

    `status_action` may be 'meeting' or 'lost' to drive the quick-action
    buttons; anything else (or None) leaves the lead at 'replied'.
    """
    if _inbound_use_fixture():
        # POC demo: pretend it sent. Real send + persistence requires
        # the email-engine backend (Epic 2) and DB. Here we just confirm
        # the call so the UI completes its success path.
        rows = {r['id']: r for r in _replies_local_fixture()}
        head = rows.get(reply_id)
        if not head:
            raise LookupError(f'reply {reply_id} not found')
        new_status = 'meeting' if status_action == 'meeting' else \
                     'lost'    if status_action == 'lost'    else None
        return {
            'ok':         True,
            'reply_id':   reply_id,
            'lead_id':    head['lead_id'],
            'new_status': new_status,
            'demo':       True,
        }

    head = fetch_one("""
        select r.id, r.lead_id, r.from_address,
               l.client_id, l.decision_maker_email
          from crm.replies r
          join crm.leads   l on l.id = r.lead_id
         where r.id = %(rid)s
    """, {'rid': reply_id})
    if not head:
        raise LookupError(f'reply {reply_id} not found')

    sending = settings_get('sending_email') or {}
    from_addr = sending.get('from_address') or ''
    to_addr   = head.get('from_address') or head.get('decision_maker_email') or ''

    execute("""
        insert into crm.emails
          (lead_id, client_id, email_number, subject, body,
           from_address, to_address, status, sent_at)
        values
          (%(lid)s, %(cid)s, 1, %(subj)s, %(body)s,
           %(from)s, %(to)s, 'sent', now())
    """, {
        'lid': head['lead_id'], 'cid': head['client_id'],
        'subj': subject, 'body': body, 'from': from_addr, 'to': to_addr,
    })

    execute("update crm.replies set processed = true where id = %(rid)s",
            {'rid': reply_id})

    new_status = None
    if status_action == 'meeting':
        new_status = 'meeting'
    elif status_action == 'lost':
        new_status = 'lost'
    if new_status:
        execute("""
            update crm.leads set status = %(s)s, updated_at = now()
             where id = %(lid)s
        """, {'s': new_status, 'lid': head['lead_id']})

    execute("""
        insert into crm.activity_log (client_id, lead_id, action, detail)
             values (%(cid)s, %(lid)s, 'manual_reply_sent', %(detail)s)
    """, {
        'cid': head['client_id'], 'lid': head['lead_id'],
        'detail': _json.dumps({
            'reply_id': reply_id,
            'status_change': new_status,
            'subject': subject[:200],
        }),
    })

    return {
        'ok':         True,
        'reply_id':   reply_id,
        'lead_id':    head['lead_id'],
        'new_status': new_status,
    }


def _inbound_decorate(row: dict) -> dict:
    """Add UI-derived fields to an inbound row in place: relative time,
    short timestamp, status label, auto-reply preview, why-this-grade."""
    if not row:
        return row
    created = row.get('created_at')
    if created:
        row['relative']      = relative_time(created)
        row['created_short'] = created.strftime('%-d %b · %H:%M')
    else:
        row['relative'] = '—'
        row['created_short'] = '—'
    row['status_label']    = INBOUND_STATUS_LABEL.get(row.get('status') or '', '—')
    auto_at = row.get('auto_response_sent_at')
    if auto_at and created:
        delta_s = max(0, int((auto_at - created).total_seconds()))
        row['auto_reply_lag'] = f'{delta_s}s after submission'
    else:
        row['auto_reply_lag'] = ''
    return row


def _inbound_compose_auto_reply(row: dict) -> str:
    """Synthesize the AI auto-reply preview shown in the drawer.
    Real production replies come from the marketing site's Anthropic
    call (see api/submit.js); here we render a faithful template so
    the demo reads like the real thing without round-tripping the API."""
    first = (row.get('name') or '').split(' ', 1)[0] or 'there'
    deal  = row.get('deal_value') or 'the range you mentioned'
    return (
        f"Hi {first}, thanks for the detail — really useful context.\n\n"
        f"Based on what you've shared (currently: {row.get('current_method') or 'your existing approach'}), "
        f"and a target of {row.get('clients_wanted') or 'a steady pipeline'}, I think we can help. "
        f"Our system runs three pillars in parallel — outbound, content, and paid — "
        f"so the pipeline isn't dependent on any one channel.\n\n"
        f"For the {deal} range, the closest live engagement is ROCA (3× pipeline in 60 days). "
        f"I'd suggest a 25-min call this week to walk through how we'd shape it for "
        f"{row.get('company') or 'your business'}. Calendar link below — pick what works.\n\n"
        f"— Sammy"
    )


def _inbound_why_grade(row: dict) -> list[str]:
    """One-line bullets explaining the score, shown in the drawer."""
    score   = row.get('score')
    deal    = row.get('deal_value') or '—'
    method  = row.get('current_method') or '—'
    wanted  = row.get('clients_wanted') or '—'
    sector  = row.get('industry') or '—'
    head = {
        'hot':  'Hot — high-intent, in-bracket budget, qualified sector',
        'warm': 'Warm — solid fit but lower urgency or smaller deal',
        'cold': 'Cold — segment or budget below qualification floor',
    }.get(score or '', 'Unscored')
    return [
        head,
        f'Deal range: {deal}',
        f'Sector: {sector}',
        f'Currently: {method}',
        f'Target volume: {wanted}',
    ]


def _inbound_filter_tab(rows: list[dict], tab: str) -> list[dict]:
    if tab == 'hot':
        return [r for r in rows if r.get('score') == 'hot']
    if tab == 'new':
        return [r for r in rows if r.get('status') == 'new']
    if tab == 'progress':
        return [r for r in rows if r.get('status') in INBOUND_PROGRESS_STATUSES]
    if tab == 'closed':
        return [r for r in rows if r.get('status') in INBOUND_CLOSED_STATUSES]
    return list(rows)


def _inbound_filter_extras(rows: list[dict], score: str | None,
                           search: str | None) -> list[dict]:
    out = rows
    if score in INBOUND_SCORES:
        out = [r for r in out if r.get('score') == score]
    if search:
        q = search.lower()
        out = [r for r in out
               if q in (r.get('name') or '').lower()
               or q in (r.get('company') or '').lower()]
    return out


def inbound_kpis() -> dict:
    """Four headline numbers for the Inbound page."""
    if _inbound_use_fixture():
        rows = _inbound_local_fixture()
        now  = datetime.now(timezone.utc)
        new_7d  = sum(1 for r in rows if (now - r['created_at']).days < 7)
        new_prev = sum(1 for r in rows if 7 <= (now - r['created_at']).days < 14)
        hot_7d  = sum(1 for r in rows if r['score'] == 'hot'
                                       and (now - r['created_at']).days < 7)
        # Avg first-response = auto_response lag in seconds (POC proxy)
        lags = [(r['auto_response_sent_at'] - r['created_at']).total_seconds()
                for r in rows
                if r.get('auto_response_sent') and r.get('auto_response_sent_at')]
        avg_lag_s = int(sum(lags) / len(lags)) if lags else 0
        # Book rate = (status in called/proposal/won) / total — last 30d
        last_30 = [r for r in rows if (now - r['created_at']).days < 30]
        booked  = sum(1 for r in last_30
                      if r['status'] in ('called', 'proposal', 'won'))
        book_rate = round(booked / len(last_30) * 100, 1) if last_30 else 0.0
        return {
            'new_7d':         new_7d,
            'new_7d_delta':   new_7d - new_prev,
            'hot_count':      hot_7d,
            'hot_pct':        round(hot_7d / new_7d * 100) if new_7d else 0,
            'avg_response':   _format_response_lag(avg_lag_s),
            'avg_response_s': avg_lag_s,
            'book_rate':      book_rate,
        }

    sql = """
        select
          (select count(*) from crm.inbound_leads
             where created_at >= now() - interval '7 days')                     as new_7d,
          (select count(*) from crm.inbound_leads
             where created_at >= now() - interval '14 days'
               and created_at <  now() - interval '7 days')                     as new_prev,
          (select count(*) from crm.inbound_leads
             where score = 'hot' and created_at >= now() - interval '7 days')   as hot_7d,
          coalesce((select extract(epoch from avg(auto_response_sent_at - created_at))
             from crm.inbound_leads
             where auto_response_sent is true
               and auto_response_sent_at is not null), 0)                       as avg_lag_s,
          (select count(*) from crm.inbound_leads
             where status in ('called','proposal','won')
               and created_at >= now() - interval '30 days')                    as booked_30,
          (select count(*) from crm.inbound_leads
             where created_at >= now() - interval '30 days')                    as total_30
    """
    r = fetch_one(sql) or {}
    new_7d   = int(r.get('new_7d') or 0)
    new_prev = int(r.get('new_prev') or 0)
    hot_7d   = int(r.get('hot_7d') or 0)
    booked   = int(r.get('booked_30') or 0)
    total_30 = int(r.get('total_30') or 0)
    avg_lag_s = int(r.get('avg_lag_s') or 0)
    return {
        'new_7d':         new_7d,
        'new_7d_delta':   new_7d - new_prev,
        'hot_count':      hot_7d,
        'hot_pct':        round(hot_7d / new_7d * 100) if new_7d else 0,
        'avg_response':   _format_response_lag(avg_lag_s),
        'avg_response_s': avg_lag_s,
        'book_rate':      round(booked / total_30 * 100, 1) if total_30 else 0.0,
    }


def _format_response_lag(seconds: int) -> str:
    if seconds <= 0:    return '—'
    if seconds < 60:    return f'{seconds}s'
    if seconds < 3600:  return f'{seconds // 60}m {seconds % 60:02d}s'
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f'{h}h {m:02d}m'


def inbound_tab_counts() -> dict:
    """Counts per tab — drives the tab pills."""
    if _inbound_use_fixture():
        rows = _inbound_local_fixture()
        return {
            'all':      len(rows),
            'hot':      sum(1 for r in rows if r['score']  == 'hot'),
            'new':      sum(1 for r in rows if r['status'] == 'new'),
            'progress': sum(1 for r in rows if r['status'] in INBOUND_PROGRESS_STATUSES),
            'closed':   sum(1 for r in rows if r['status'] in INBOUND_CLOSED_STATUSES),
        }
    r = fetch_one("""
        select
          count(*)                                                       as all_count,
          count(*) filter (where score = 'hot')                          as hot_count,
          count(*) filter (where status = 'new')                         as new_count,
          count(*) filter (where status in ('contacted','called','proposal'))
                                                                         as progress_count,
          count(*) filter (where status in ('won','lost'))               as closed_count
        from crm.inbound_leads
    """) or {}
    return {
        'all':      int(r.get('all_count')      or 0),
        'hot':      int(r.get('hot_count')      or 0),
        'new':      int(r.get('new_count')      or 0),
        'progress': int(r.get('progress_count') or 0),
        'closed':   int(r.get('closed_count')   or 0),
    }


def inbound_needs_response(limit: int = 5) -> list[dict]:
    """Hot/warm submissions still in 'new' — the priority strip."""
    if _inbound_use_fixture():
        rows = [r for r in _inbound_local_fixture()
                if r['status'] == 'new' and r['score'] in ('hot', 'warm')]
        rows.sort(key=lambda r: ({'hot': 0, 'warm': 1}.get(r['score'], 2),
                                 r['created_at']))
        return [_inbound_decorate(dict(r)) for r in rows[:limit]]
    rows = fetch_all("""
        select id, name, company, score, status, deal_value, created_at,
               auto_response_sent, auto_response_sent_at
          from crm.inbound_leads
         where status = 'new' and score in ('hot', 'warm')
         order by case score when 'hot' then 0 when 'warm' then 1 else 2 end,
                  created_at asc
         limit %(lim)s
    """, {'lim': limit})
    return [_inbound_decorate(r) for r in rows]


def inbound_list(*, tab: str = 'all', score: str | None = None,
                 search: str | None = None, limit: int = 200) -> list[dict]:
    """Main inbound table query — filtered by tab + facet filters."""
    if tab not in INBOUND_TABS:
        tab = 'all'
    if _inbound_use_fixture():
        rows = _inbound_local_fixture()
        rows = _inbound_filter_tab(rows, tab)
        rows = _inbound_filter_extras(rows, score, search)
        rows.sort(key=lambda r: r['created_at'], reverse=True)
        return [_inbound_decorate(dict(r)) for r in rows[:limit]]

    where: list[str] = ['1=1']
    params: dict = {'lim': limit}
    if tab == 'hot':
        where.append("score = 'hot'")
    elif tab == 'new':
        where.append("status = 'new'")
    elif tab == 'progress':
        where.append("status in ('contacted','called','proposal')")
    elif tab == 'closed':
        where.append("status in ('won','lost')")
    if score in INBOUND_SCORES:
        where.append('score = %(score)s'); params['score'] = score
    if search:
        where.append('(name ilike %(q)s or company ilike %(q)s)')
        params['q'] = f'%{search}%'
    rows = fetch_all(f"""
        select id, name, company, email, phone, industry, deal_value,
               current_method, clients_wanted,
               score, status, auto_response_sent, auto_response_sent_at,
               notes, created_at
          from crm.inbound_leads
         where {' and '.join(where)}
         order by created_at desc
         limit %(lim)s
    """, params)
    return [_inbound_decorate(r) for r in rows]


def inbound_get(inbound_id: int) -> dict | None:
    """One inbound lead with derived fields (auto-reply preview + why-grade)."""
    if _inbound_use_fixture():
        match = next((r for r in _inbound_local_fixture()
                      if r['id'] == inbound_id), None)
        if not match:
            return None
        row = dict(match)
    else:
        row = fetch_one("""
            select id, name, company, email, phone, industry, deal_value,
                   current_method, clients_wanted,
                   score, status, auto_response_sent, auto_response_sent_at,
                   notes, created_at
              from crm.inbound_leads
             where id = %s
        """, (inbound_id,))
        if not row:
            return None
    _inbound_decorate(row)
    row['auto_reply_text'] = _inbound_compose_auto_reply(row)
    row['why_grade']       = _inbound_why_grade(row)
    return row


def update_inbound_status(inbound_id: int, new_status: str) -> dict:
    """Change one inbound lead's status. Returns {old, new, name} or raises."""
    if new_status not in INBOUND_STATUSES:
        raise ValueError(f'Invalid status: {new_status}')
    if _inbound_use_fixture():
        # No persistence in fixture mode — just echo the requested change so
        # the UI can confirm the action visually. Production hits the DB.
        row = next((r for r in _inbound_local_fixture()
                    if r['id'] == inbound_id), None)
        if not row:
            raise LookupError(f'No inbound lead {inbound_id}')
        return {'old': row['status'], 'new': new_status,
                'name': row['name'], 'persisted': False}
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                'select id, name, status from crm.inbound_leads where id = %s',
                (inbound_id,),
            )
            row = cur.fetchone()
            if not row:
                raise LookupError(f'No inbound lead {inbound_id}')
            old = row['status']
            if old == new_status:
                return {'old': old, 'new': new_status,
                        'name': row['name'], 'persisted': False}
            cur.execute(
                'update crm.inbound_leads set status = %s where id = %s',
                (new_status, inbound_id),
            )
            cur.execute(
                'insert into crm.activity_log (action, detail) values (%s, %s)',
                ('inbound_status_change',
                 f"{row['name']} — inbound status {old} → {new_status}"),
            )
        conn.commit()
    return {'old': old, 'new': new_status,
            'name': row['name'], 'persisted': True}


# ── Reports (Step 9) ─────────────────────────────────────────────────
# Per-client performance reports — what Sammy sends to a paying client.
# Period: '7d' | '30d' | '90d' (default 30d). All queries scoped to
# client_id; previous-period delta uses the same window length shifted
# back by one window.

REPORTS_PERIODS = (
    ('7d',  'Last 7 days',  7),
    ('30d', 'Last 30 days', 30),
    ('90d', 'Last 90 days', 90),
)

LEAD_GRADES = ('A', 'B', 'C', 'D', 'F')


def reports_period_days(period: str) -> int:
    for slug, _label, days in REPORTS_PERIODS:
        if slug == period:
            return days
    return 30


def _reports_use_fixture() -> bool:
    return not DATABASE_URL


# ── Fixture (POC localhost) ───────────────────────────────────────
# Two clients, three periods, full payloads. Numbers are tuned to look
# like a healthy mid-tier outbound engagement (ROCA-grade) so the demo
# reads as a real client report rather than a stress test.

_REPORTS_CLIENTS_FIXTURE: list[dict] = [
    {'id': 1, 'name': 'Vidora Media',     'industry': 'Content production',
     'contact_name': 'Adam Bimpson', 'contact_email': 'adam@vidoramedia.com',
     'pricing_tier': '£3,500',  'monthly_fee': 3500,
     'onboarded_at': datetime(2025, 11, 12, tzinfo=timezone.utc)},
    {'id': 2, 'name': 'ROCA Accountants', 'industry': 'Professional services',
     'contact_name': 'Hannah Cole', 'contact_email': 'hannah@roca.co.uk',
     'pricing_tier': '£3,500',  'monthly_fee': 3500,
     'onboarded_at': datetime(2026, 1, 8, tzinfo=timezone.utc)},
]


def _reports_seed(client_id: int, days: int) -> dict:
    """Deterministic per-(client, period) bundle — same call returns
    same data so screenshots are stable. Differentiated by client so
    Vidora and ROCA tell different stories.

    `avg_deal_value` is the per-meeting pipeline assumption (UK B2B
    service midpoint), used to compute reportable pipeline value.
    Belongs on the client record long-term — see a future story for
    the schema migration. Until then it lives here so the report can
    show the outcome metric clients actually care about.
    """
    base = {
        # Vidora — content/creator focus, broader top of funnel
        1: {'leads_per_day': 6.1, 'sends_mult': 2.2, 'reply_rate': 11.8,
            'open_rate': 47.0, 'meeting_rate': 4.9,
            'won_rate': 0.18,  # of meetings → won
            'avg_deal_value': 4500,
            'grade_mix': {'A': 0.18, 'B': 0.34, 'C': 0.31, 'D': 0.13, 'F': 0.04}},
        # ROCA — accountancy, tighter qualification, higher reply rate
        2: {'leads_per_day': 4.7, 'sends_mult': 2.8, 'reply_rate': 14.2,
            'open_rate': 51.0, 'meeting_rate': 7.1,
            'won_rate': 0.25,
            'avg_deal_value': 8200,
            'grade_mix': {'A': 0.24, 'B': 0.38, 'C': 0.26, 'D': 0.10, 'F': 0.02}},
    }.get(client_id, {'leads_per_day': 5.0, 'sends_mult': 2.4,
                      'reply_rate': 12.0, 'open_rate': 48.0,
                      'meeting_rate': 5.5, 'won_rate': 0.20,
                      'avg_deal_value': 6000,
                      'grade_mix': {'A': 0.20, 'B': 0.35, 'C': 0.30,
                                    'D': 0.12, 'F': 0.03}})

    leads_now  = int(round(base['leads_per_day']  * days))
    leads_prev = int(round(base['leads_per_day']  * days * 0.86))
    sent_now   = int(round(leads_now  * base['sends_mult']))
    sent_prev  = int(round(leads_prev * base['sends_mult']))
    reply_rate_now  = base['reply_rate']
    reply_rate_prev = round(base['reply_rate'] - 1.4, 1)
    meetings_now    = int(round(leads_now  * (base['meeting_rate'] / 100)))
    meetings_prev   = int(round(leads_prev * (base['meeting_rate'] / 100)))
    won_now         = int(round(meetings_now  * base['won_rate']))
    won_prev        = int(round(meetings_prev * base['won_rate']))
    avg_deal        = base['avg_deal_value']

    return {
        'leads_now': leads_now, 'leads_prev': leads_prev,
        'sent_now':  sent_now,  'sent_prev':  sent_prev,
        'reply_rate_now':  reply_rate_now, 'reply_rate_prev': reply_rate_prev,
        'meetings_now':    meetings_now,   'meetings_prev':   meetings_prev,
        'won_now':         won_now,        'won_prev':        won_prev,
        'open_rate':       base['open_rate'],
        'avg_deal_value':  avg_deal,
        'pipeline_value_now':  meetings_now * avg_deal,
        'pipeline_value_prev': meetings_prev * avg_deal,
        'grade_mix_pct':   base['grade_mix'],
    }


def _reports_chart_fixture(client_id: int, days: int) -> dict:
    """Daily sent + replies series for the chart. Uses a deterministic
    pseudo-random walk seeded by (client_id, days) so screenshots are
    stable across reloads."""
    import math
    s    = _reports_seed(client_id, days)
    avg_sent_day = s['sent_now'] / days if days else 0
    labels:  list[str] = []
    sent:    list[int] = []
    replies: list[int] = []
    today = datetime.now(timezone.utc).date()
    for i in range(days):
        d = today - timedelta(days=days - 1 - i)
        labels.append(d.strftime('%-d %b'))
        # Weekend dip (Sat/Sun = 0.4x), rest 0.85–1.15 wave
        wkday = d.weekday()
        wk    = 0.4 if wkday >= 5 else 1.0
        wave  = 0.92 + 0.18 * math.sin((i + client_id * 3) / 2.7)
        v     = max(0, int(round(avg_sent_day * wk * wave)))
        sent.append(v)
        replies.append(int(round(v * (s['reply_rate_now'] / 100))))
    return {'labels': labels, 'sent': sent, 'replies': replies}


def _reports_funnel_fixture(client_id: int, days: int) -> list[dict]:
    s        = _reports_seed(client_id, days)
    leads    = s['leads_now']
    contacted = int(round(leads * 0.78))
    replied   = int(round(leads * (s['reply_rate_now'] / 100) * 2.5))
    meetings  = s['meetings_now']
    won       = max(1, int(round(meetings * 0.22)))
    return [
        {'label': 'Leads sourced',  'value': leads,     'pct_of_prev': None},
        {'label': 'Contacted',      'value': contacted, 'pct_of_prev': round(contacted/leads*100) if leads else 0},
        {'label': 'Replied',        'value': replied,   'pct_of_prev': round(replied/contacted*100) if contacted else 0},
        {'label': 'Meeting booked', 'value': meetings,  'pct_of_prev': round(meetings/replied*100) if replied else 0},
        {'label': 'Won',            'value': won,       'pct_of_prev': round(won/meetings*100) if meetings else 0},
    ]


def _reports_sequence_fixture(client_id: int, days: int) -> list[dict]:
    s = _reports_seed(client_id, days)
    sent_total = s['sent_now']
    # Distribution Day1 / Day3 / Day7 ≈ 0.40 / 0.36 / 0.24
    splits = [(1, 'Day 1', 0.40, 1.00, 0.34),
              (2, 'Day 3', 0.36, 0.92, 0.49),
              (3, 'Day 7', 0.24, 0.85, 0.65)]
    base_open  = s['open_rate']
    base_reply = s['reply_rate_now']
    out = []
    for n, label, share, open_mult, reply_mult in splits:
        sent      = int(round(sent_total * share))
        opens     = int(round(sent * (base_open  * open_mult) / 100))
        replies   = int(round(sent * (base_reply * reply_mult) / 100))
        out.append({
            'step':       n,
            'label':      label,
            'sent':       sent,
            'opens':      opens,
            'replies':    replies,
            'open_rate':  round(opens   / sent * 100, 1) if sent else 0.0,
            'reply_rate': round(replies / sent * 100, 1) if sent else 0.0,
        })
    return out


def _reports_grade_mix_fixture(client_id: int, days: int) -> list[dict]:
    s     = _reports_seed(client_id, days)
    leads = s['leads_now']
    out = []
    for g in LEAD_GRADES:
        pct = s['grade_mix_pct'].get(g, 0)
        out.append({
            'grade': g,
            'count': int(round(leads * pct)),
            'pct':   round(pct * 100, 1),
        })
    return out


def _format_days_ago(d: int) -> str:
    if d <= 0:  return 'today'
    if d == 1:  return 'yesterday'
    if d < 7:   return f'{d} days ago'
    if d < 14:  return '1 week ago'
    if d < 30:  return f'{d // 7} weeks ago'
    if d < 60:  return '1 month ago'
    return f'{d // 30} months ago'


# Named meetings + deals per client. First row in each list is the lead
# also surfaced on the Inbox demo (Sarah Cole, James Whitford, Nina Patel)
# so the prospect demo tells one story across pages. Padded with realistic
# UK B2B contacts. Days_ago controls which rows fall inside the picked
# period (7d / 30d / 90d).
_REPORTS_WINS_FIXTURE: dict[int, list[tuple]] = {
    # Vidora Media — content / creator economy
    1: [
        ('meeting', 'James Whitford', 'Whitford Property Group',     4),
        ('meeting', 'Priya Shah',     'Shah Studios',                6),
        ('won',     'Daniel Webb',    'Webb Creator Network',        9),
        ('meeting', 'Aaron Pyke',     'Pyke & Co Films',            18),
        ('meeting', 'Helena Marsh',   'Marsh Creative House',       33),
        ('won',     'Ross Caldwell',  'Caldwell Brothers Media',    52),
    ],
    # ROCA Accountants — accountancy / professional services
    2: [
        ('meeting', 'Nina Patel',     'Harbor Legal',                 3),
        ('meeting', 'Sarah Cole',     'Cole & Reeves Accountants',    5),
        ('meeting', 'David Marsh',    'Marsh & Trent Audit',         11),
        ('won',     'Olivia Bennett', 'Bennett Tax Group',           19),
        ('meeting', 'Tom Reeves',     'Coastal Bookkeeping Ltd',     34),
        ('won',     'Marcus Hill',    'Hill & Daughter Audit',       48),
    ],
}


def _reports_wins_fixture(client_id: int, days: int) -> list[dict]:
    avg = _reports_seed(client_id, days)['avg_deal_value']
    rows = _REPORTS_WINS_FIXTURE.get(client_id, [])
    out: list[dict] = []
    for outcome, name, business, days_ago in rows:
        if days_ago > days:
            continue
        out.append({
            'outcome':    outcome,
            'contact':    name,
            'business':   business,
            'days_ago':   days_ago,
            'when':       _format_days_ago(days_ago),
            'deal_value': avg if outcome == 'won' else None,
        })
    return out


# ── Public reports API ───────────────────────────────────────────
def reports_clients_min() -> list[dict]:
    """Client picker for the reports header."""
    if _reports_use_fixture():
        return [{'id': c['id'], 'name': c['name']}
                for c in _REPORTS_CLIENTS_FIXTURE]
    return all_clients_min()


def reports_hook_cohorts(client_id: int, days: int) -> list[dict]:
    """Reply rate broken down by lead.hook_type for the selected client
    and period.

    The pipeline already tags every lead with a hook_type (year_end_imminent,
    accounts_overdue, director_change_recent, etc.). Without measuring
    reply rate per hook, we're paying Anthropic for personalised hooks
    and optimising blind — this is the cheapest visibility win on the
    backend.

    Rows: {hook_type, leads_sent, leads_replied, reply_rate (%)}.
    Sort by reply_rate desc so the operator sees the winners first.
    Returns [] in fixture mode (the demo dataset doesn't carry hook_type
    correlations).
    """
    if _reports_use_fixture():
        return []
    return fetch_all("""
        with w as (
          select now() - make_interval(days => %(d)s) as t_start
        ),
        sent as (
          select l.hook_type,
                 count(distinct e.lead_id) as leads_sent
            from crm.emails e
            join crm.leads   l on l.id = e.lead_id
            join w on true
           where e.client_id      = %(cid)s
             and e.email_number   = 1
             and e.status         in ('sent','dry_run_ready')
             and e.sent_at       >= w.t_start
             and l.hook_type     is not null
           group by l.hook_type
        ),
        replied as (
          select l.hook_type,
                 count(distinct r.lead_id) as leads_replied
            from crm.replies r
            join crm.leads   l on l.id = r.lead_id
            join w on true
           where l.client_id     = %(cid)s
             and r.detected_at  >= w.t_start
             and l.hook_type    is not null
           group by l.hook_type
        )
        select s.hook_type,
               s.leads_sent,
               coalesce(r.leads_replied, 0) as leads_replied,
               case when s.leads_sent > 0
                    then round(100.0 * coalesce(r.leads_replied, 0) / s.leads_sent, 1)
                    else 0 end as reply_rate
          from sent s
          left join replied r on r.hook_type = s.hook_type
         order by reply_rate desc, s.leads_sent desc
    """, {'cid': client_id, 'd': days})


def reports_rollup(days: int = 30) -> dict:
    """Cross-client headline numbers for the top of /reports.

    Deliberately a thin counts+sum query — not a replacement for the
    per-client reports_kpis, just the 'how is the agency doing overall'
    glance that the previous IA forced operators to compute by clicking
    through every client tab.
    """
    if _reports_use_fixture():
        # Sum the fixture across clients so the demo strip lights up too.
        meetings = 0
        won = 0
        pipeline = 0
        for cid in (1, 2):
            for row in _reports_wins_fixture(cid, days):
                if row['outcome'] == 'meeting':
                    meetings += 1
                elif row['outcome'] == 'won':
                    won += 1
                    pipeline += int(row.get('deal_value') or 0)
        return {
            'clients':        len(_REPORTS_CLIENTS_FIXTURE),
            'meetings':       meetings,
            'won':            won,
            'pipeline_value': pipeline,
            'days':           days,
        }

    # No deal_value column on crm.leads — pipeline is estimated as
    # won_count × avg deal value, the same convention used in the
    # per-client reports_kpis path.
    row = fetch_one("""
        with w as (select now() - make_interval(days => %(d)s) as t_start)
        select
          (select count(*) from crm.clients)                                  as clients,
          (select count(*) from crm.leads, w
              where status = 'meeting' and updated_at >= t_start)             as meetings,
          (select count(*) from crm.leads, w
              where status = 'won'     and updated_at >= t_start)             as won
    """, {'d': days}) or {}
    won = int(row.get('won') or 0)
    return {
        'clients':        int(row.get('clients') or 0),
        'meetings':       int(row.get('meetings') or 0),
        'won':            won,
        'pipeline_value': won * REPORTS_DEFAULT_AVG_DEAL_VALUE,
        'days':           days,
    }


def reports_client_summary(client_id: int) -> dict | None:
    """Header info: name, contact_email (for mailto), tier, since."""
    if _reports_use_fixture():
        match = next((c for c in _REPORTS_CLIENTS_FIXTURE
                      if c['id'] == client_id), None)
        if not match:
            return None
        row = dict(match)
        row['since'] = row['onboarded_at'].strftime('%b %Y')
        return row
    return get_client(client_id)


def reports_wins(client_id: int, days: int) -> list[dict]:
    """Named meetings + deals for the 'Wins this period' card. Reads
    from `crm.leads` where status in ('meeting','won') and the most
    recent status change (`updated_at`) falls inside the window. Capped
    at 6 most-recent rows so the card stays scannable."""
    if _reports_use_fixture():
        return _reports_wins_fixture(client_id, days)

    sql = """
        with w as (select now() - make_interval(days => %(d)s) as t_start)
        select id,
               decision_maker_name,
               business_name,
               status,
               updated_at
          from crm.leads, w
         where client_id = %(cid)s
           and status in ('meeting', 'won')
           and updated_at >= t_start
         order by updated_at desc
         limit 6
    """
    rows = fetch_all(sql, {'cid': client_id, 'd': days}) or []
    avg = REPORTS_DEFAULT_AVG_DEAL_VALUE
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for r in rows:
        updated  = r.get('updated_at')
        days_ago = (now - updated).days if updated else 0
        outcome  = r.get('status')
        out.append({
            'outcome':    outcome,
            'contact':    r.get('decision_maker_name') or '(unknown)',
            'business':   r.get('business_name')      or '(unknown)',
            'days_ago':   days_ago,
            'when':       _format_days_ago(days_ago),
            # POC: avg estimate until US-031 lands a per-lead deal_value column
            'deal_value': avg if outcome == 'won' else None,
        })
    return out


REPORTS_DEFAULT_AVG_DEAL_VALUE = 6000  # GBP — UK B2B service midpoint, see _reports_seed
REPORTS_TARGETS_MONTHLY = {
    'meetings': 5,        # target meetings per 30 days
    'reply_rate': 8.0,    # target reply % (period-independent)
}


def reports_targets(days: int) -> dict:
    """Period-scaled goals for the report KPI tiles.

    Reply-rate target is a ratio so it does not scale by window. Meetings
    target is monthly; pro-rate it for the period the operator picked.
    Living in code for the POC — when client-specific goals land, this
    moves to a `goals` JSONB on `crm.clients` and the route fetches it.
    """
    monthly_mtg = REPORTS_TARGETS_MONTHLY['meetings']
    return {
        'meetings':   max(1, int(round(monthly_mtg * days / 30))),
        'reply_rate': REPORTS_TARGETS_MONTHLY['reply_rate'],
    }


def reports_narrative(client_id: int, days: int, kpis: dict) -> str:
    """Auto-generated 'what we did this period' paragraph that sits at
    the top of the report. POC version composes from the KPIs already
    computed; a future story persists an operator-edited version per
    (client, period). The paragraph is what makes the report feel like
    work was done, not just a dashboard screenshot.
    """
    leads = kpis.get('leads', 0)
    sent  = kpis.get('sent', 0)
    reps  = int(round(sent * (kpis.get('reply_rate', 0) / 100)))
    mtgs  = kpis.get('meetings', 0)
    won   = kpis.get('won', 0)
    rr    = kpis.get('reply_rate', 0)

    if leads == 0:
        return ('No outbound activity this period — onboarding still in '
                'progress, or the campaign is paused.')

    parts = [
        f"Sourced {leads:,} leads against your targeting profile",
        f"sent {sent:,} emails across the Day 1 / 3 / 7 cadence",
        f"recorded {reps} replies ({rr}% reply rate)",
    ]
    if mtgs:
        parts.append(f"booked {mtgs} meeting{'s' if mtgs != 1 else ''}")
    if won:
        parts.append(f"closed {won} deal{'s' if won != 1 else ''}")

    return ', '.join(parts) + '.'


def reports_kpis(client_id: int, days: int) -> dict:
    """Headline numbers + deltas vs the previous window. Includes the
    outcome metrics clients actually care about (meetings, deals won,
    pipeline value) alongside the activity ones (leads, sent, reply
    rate) so the route can render either as primary tiles or as the
    Activity subline below."""
    if _reports_use_fixture():
        s = _reports_seed(client_id, days)
        return {
            'leads':       s['leads_now'],
            'leads_delta': s['leads_now'] - s['leads_prev'],
            'sent':        s['sent_now'],
            'sent_delta':  s['sent_now'] - s['sent_prev'],
            'reply_rate':       s['reply_rate_now'],
            'reply_rate_delta': round(s['reply_rate_now'] - s['reply_rate_prev'], 1),
            'meetings':       s['meetings_now'],
            'meetings_delta': s['meetings_now'] - s['meetings_prev'],
            'won':            s['won_now'],
            'won_delta':      s['won_now']  - s['won_prev'],
            'pipeline_value':       s['pipeline_value_now'],
            'pipeline_value_delta': s['pipeline_value_now'] - s['pipeline_value_prev'],
            'avg_deal_value':       s['avg_deal_value'],
        }
    sql = """
        with windows as (
            select
              now() - make_interval(days => %(d_now)s)  as t_now_start,
              now()                                      as t_now_end,
              now() - make_interval(days => %(d_prev)s) as t_prev_start,
              now() - make_interval(days => %(d_now)s)  as t_prev_end
        )
        select
          (select count(*) from crm.leads, windows
             where client_id = %(cid)s
               and created_at >= t_now_start and created_at < t_now_end)            as leads_now,
          (select count(*) from crm.leads, windows
             where client_id = %(cid)s
               and created_at >= t_prev_start and created_at < t_prev_end)          as leads_prev,
          (select count(*) from crm.emails, windows
             where client_id = %(cid)s and status = 'sent'
               and sent_at >= t_now_start and sent_at < t_now_end)                  as sent_now,
          (select count(*) from crm.emails, windows
             where client_id = %(cid)s and status = 'sent'
               and sent_at >= t_prev_start and sent_at < t_prev_end)                as sent_prev,
          (select count(*) from crm.emails, windows
             where client_id = %(cid)s and replied_at is not null
               and replied_at >= t_now_start and replied_at < t_now_end)            as repl_now,
          (select count(*) from crm.emails, windows
             where client_id = %(cid)s and replied_at is not null
               and replied_at >= t_prev_start and replied_at < t_prev_end)          as repl_prev,
          (select count(*) from crm.leads, windows
             where client_id = %(cid)s and status = 'meeting'
               and updated_at >= t_now_start and updated_at < t_now_end)            as mtg_now,
          (select count(*) from crm.leads, windows
             where client_id = %(cid)s and status = 'meeting'
               and updated_at >= t_prev_start and updated_at < t_prev_end)          as mtg_prev,
          (select count(*) from crm.leads, windows
             where client_id = %(cid)s and status = 'won'
               and updated_at >= t_now_start and updated_at < t_now_end)            as won_now,
          (select count(*) from crm.leads, windows
             where client_id = %(cid)s and status = 'won'
               and updated_at >= t_prev_start and updated_at < t_prev_end)          as won_prev
    """
    r = fetch_one(sql, {
        'cid': client_id,
        'd_now':  days,
        'd_prev': days * 2,
    }) or {}
    sent_now  = int(r.get('sent_now')  or 0)
    sent_prev = int(r.get('sent_prev') or 0)
    repl_now  = int(r.get('repl_now')  or 0)
    repl_prev = int(r.get('repl_prev') or 0)
    rr_now    = round(repl_now  / sent_now  * 100, 1) if sent_now  else 0.0
    rr_prev   = round(repl_prev / sent_prev * 100, 1) if sent_prev else 0.0
    leads_now    = int(r.get('leads_now')  or 0)
    leads_prev   = int(r.get('leads_prev') or 0)
    mtg_now      = int(r.get('mtg_now')    or 0)
    mtg_prev     = int(r.get('mtg_prev')   or 0)
    won_now      = int(r.get('won_now')    or 0)
    won_prev     = int(r.get('won_prev')   or 0)
    avg_deal     = REPORTS_DEFAULT_AVG_DEAL_VALUE
    return {
        'leads':            leads_now,
        'leads_delta':      leads_now - leads_prev,
        'sent':             sent_now,
        'sent_delta':       sent_now - sent_prev,
        'reply_rate':       rr_now,
        'reply_rate_delta': round(rr_now - rr_prev, 1),
        'meetings':         mtg_now,
        'meetings_delta':   mtg_now - mtg_prev,
        'won':              won_now,
        'won_delta':        won_now  - won_prev,
        'pipeline_value':       mtg_now  * avg_deal,
        'pipeline_value_delta': (mtg_now - mtg_prev) * avg_deal,
        'avg_deal_value':       avg_deal,
    }


def reports_chart_series(client_id: int, days: int) -> dict:
    """Daily sent + replies for the chart."""
    if _reports_use_fixture():
        return _reports_chart_fixture(client_id, days)
    sql = """
        with d as (
          select generate_series(
            (date_trunc('day', now()) - make_interval(days => %(span)s))::date,
            date_trunc('day', now())::date - 1,
            '1 day'
          )::date as day
        )
        select to_char(d.day, 'FMDD Mon') as label,
               coalesce((select count(*) from crm.emails
                          where client_id = %(cid)s and status = 'sent'
                            and date_trunc('day', sent_at) = d.day), 0)         as sent,
               coalesce((select count(*) from crm.emails
                          where client_id = %(cid)s
                            and replied_at is not null
                            and date_trunc('day', replied_at) = d.day), 0)      as replies
          from d order by d.day
    """
    rows = fetch_all(sql, {'cid': client_id, 'span': days})
    return {
        'labels':  [r['label']        for r in rows],
        'sent':    [int(r['sent'])    for r in rows],
        'replies': [int(r['replies']) for r in rows],
    }


def reports_funnel(client_id: int, days: int) -> list[dict]:
    """5-row funnel: leads → contacted → replied → meeting → won."""
    if _reports_use_fixture():
        return _reports_funnel_fixture(client_id, days)
    sql = """
        with w as (select now() - make_interval(days => %(d)s) as t_start)
        select
          (select count(*) from crm.leads, w
             where client_id = %(cid)s and created_at >= t_start)               as leads,
          (select count(*) from crm.leads, w
             where client_id = %(cid)s and created_at >= t_start
               and status not in ('new'))                                       as contacted,
          (select count(*) from crm.leads, w
             where client_id = %(cid)s and created_at >= t_start
               and status in ('replied','meeting','won','lost','closed'))       as replied,
          (select count(*) from crm.leads, w
             where client_id = %(cid)s and created_at >= t_start
               and status in ('meeting','won'))                                 as meeting,
          (select count(*) from crm.leads, w
             where client_id = %(cid)s and created_at >= t_start
               and status = 'won')                                              as won
    """
    r = fetch_one(sql, {'cid': client_id, 'd': days}) or {}
    leads     = int(r.get('leads')     or 0)
    contacted = int(r.get('contacted') or 0)
    replied   = int(r.get('replied')   or 0)
    meeting   = int(r.get('meeting')   or 0)
    won       = int(r.get('won')       or 0)

    def pct(num, den):
        return round(num / den * 100) if den else 0
    return [
        {'label': 'Leads sourced',  'value': leads,     'pct_of_prev': None},
        {'label': 'Contacted',      'value': contacted, 'pct_of_prev': pct(contacted, leads)},
        {'label': 'Replied',        'value': replied,   'pct_of_prev': pct(replied, contacted)},
        {'label': 'Meeting booked', 'value': meeting,   'pct_of_prev': pct(meeting, replied)},
        {'label': 'Won',            'value': won,       'pct_of_prev': pct(won, meeting)},
    ]


def reports_sequence(client_id: int, days: int) -> list[dict]:
    """Per-step (Day 1/3/7) sent / open / reply rates."""
    if _reports_use_fixture():
        return _reports_sequence_fixture(client_id, days)
    sql = """
        with w as (select now() - make_interval(days => %(d)s) as t_start)
        select email_number,
               count(*) filter (where status = 'sent')                          as sent,
               count(*) filter (where opened_at is not null)                    as opens,
               count(*) filter (where replied_at is not null)                   as replies
          from crm.emails, w
         where client_id = %(cid)s
           and sent_at >= t_start
         group by email_number
         order by email_number
    """
    rows = fetch_all(sql, {'cid': client_id, 'd': days})
    out = []
    for r in rows:
        sent = int(r.get('sent') or 0)
        opens = int(r.get('opens') or 0)
        replies = int(r.get('replies') or 0)
        n = int(r.get('email_number') or 0)
        out.append({
            'step':  n,
            'label': EMAIL_STEP_LABEL.get(n, f'Step {n}'),
            'sent':       sent,
            'opens':      opens,
            'replies':    replies,
            'open_rate':  round(opens   / sent * 100, 1) if sent else 0.0,
            'reply_rate': round(replies / sent * 100, 1) if sent else 0.0,
        })
    return out


def reports_grade_mix(client_id: int, days: int) -> list[dict]:
    """Lead-grade distribution for leads sourced in the period."""
    if _reports_use_fixture():
        return _reports_grade_mix_fixture(client_id, days)
    sql = """
        with w as (select now() - make_interval(days => %(d)s) as t_start)
        select coalesce(grade, 'F') as grade, count(*) as count
          from crm.leads, w
         where client_id = %(cid)s and created_at >= t_start
         group by coalesce(grade, 'F')
    """
    rows  = fetch_all(sql, {'cid': client_id, 'd': days})
    by_g  = {r['grade']: int(r['count']) for r in rows}
    total = sum(by_g.values()) or 1
    return [{'grade': g,
             'count': by_g.get(g, 0),
             'pct':   round(by_g.get(g, 0) / total * 100, 1)}
            for g in LEAD_GRADES]


def reports_csv_rows(client_id: int, days: int) -> list[list[str]]:
    """Flat rows for CSV export — one section per metric block."""
    client = reports_client_summary(client_id) or {'name': '—'}
    kpis   = reports_kpis(client_id, days)
    funnel = reports_funnel(client_id, days)
    seq    = reports_sequence(client_id, days)
    grades = reports_grade_mix(client_id, days)

    rows: list[list[str]] = [
        ['Innovite — Performance Report'],
        ['Client', client['name']],
        ['Period', f'Last {days} days'],
        ['Generated', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')],
        [],
        ['## Headline'],
        ['Leads sourced',  str(kpis['leads']),       f"{kpis['leads_delta']:+d} vs prev"],
        ['Emails sent',    str(kpis['sent']),        f"{kpis['sent_delta']:+d} vs prev"],
        ['Reply rate',     f"{kpis['reply_rate']}%", f"{kpis['reply_rate_delta']:+.1f}pp vs prev"],
        ['Meetings',       str(kpis['meetings']),    f"{kpis['meetings_delta']:+d} vs prev"],
        [],
        ['## Funnel'],
        ['Stage', 'Count', 'Conv from previous'],
    ]
    for f in funnel:
        rows.append([f['label'], str(f['value']),
                     f"{f['pct_of_prev']}%" if f['pct_of_prev'] is not None else '—'])
    rows.append([])
    rows.append(['## Sequence'])
    rows.append(['Step', 'Sent', 'Opens', 'Open %', 'Replies', 'Reply %'])
    for s in seq:
        rows.append([s['label'], str(s['sent']), str(s['opens']),
                     f"{s['open_rate']}%", str(s['replies']), f"{s['reply_rate']}%"])
    rows.append([])
    rows.append(['## Lead grade mix'])
    rows.append(['Grade', 'Count', 'Share'])
    for g in grades:
        rows.append([g['grade'], str(g['count']), f"{g['pct']}%"])
    return rows


# Make timedelta available for the chart fixture's date math
from datetime import timedelta


# ── Settings (Step 10) ───────────────────────────────────────────────
# Single k/v store backed by crm.settings (jsonb values). When DATABASE_URL
# is unset (POC localhost) values live in an in-process dict so the demo
# can edit/save without a Supabase round-trip.

import json as _json
import os as _os
import threading as _threading
import time as _time

SETTINGS_DEFAULTS: dict = {
    'profile': {
        'name':        'Sammy Bimpson',
        'email':       'sammy@innoviteai.com',
        'booking_url': 'https://cal.com/sammy/innovite-strategy',
        'signature':   '— Sammy / Innovite',
    },
    'sending_email': {
        'smtp_host':    'smtp.zoho.eu',
        'smtp_port':    587,
        'imap_host':    'imap.zoho.eu',
        'imap_port':    993,
        'from_address': 'sammy@innoviteai.com',
    },
    'sending_hours': {
        'start':         '09:00',
        'end':           '17:00',
        'tz':            'Europe/London',
        'skip_weekends': True,
    },
    'daily_send_cap': 120,
    'cadence': {
        'day1_enabled':  True,
        'day3_enabled':  True,
        'day7_enabled':  True,
        'skip_weekends': True,
    },
    'system_outreach_paused': False,
}

# In-process store for fixture mode. Survives only as long as the Flask
# process — that's fine for a demo; production rounds-trips Supabase.
_settings_local: dict = {}


def _settings_use_fixture() -> bool:
    return not DATABASE_URL


def settings_get(key: str):
    """Return the stored value for `key`, falling back to the default."""
    if _settings_use_fixture():
        if key in _settings_local:
            return _settings_local[key]
        return SETTINGS_DEFAULTS.get(key)
    row = fetch_one('select value from crm.settings where key = %s', (key,))
    if row and row.get('value') is not None:
        return row['value']
    return SETTINGS_DEFAULTS.get(key)


def settings_set(key: str, value) -> None:
    """Upsert a setting. Value is JSON-serialised before write."""
    if _settings_use_fixture():
        _settings_local[key] = value
        return
    execute(
        """insert into crm.settings (key, value, updated_at)
                values (%s, %s::jsonb, now())
           on conflict (key) do update
                set value = excluded.value, updated_at = now()""",
        (key, _json.dumps(value)),
    )


def settings_all() -> dict:
    """All known settings, with defaults filled in for anything missing."""
    return {k: settings_get(k) for k in SETTINGS_DEFAULTS}


# ── Worker heartbeat ─────────────────────────────────────────────────
# The crm-worker process upserts crm.settings.worker_heartbeat once per
# poll tick (every 5s). This is the only signal /settings has that the
# background loop is alive. /healthz deliberately stays DB-free, so we
# can't piggyback on that.

WORKER_HEARTBEAT_STALE_S = 180  # 3 minutes — generous given a 5s tick


def worker_heartbeat() -> dict:
    """Return {'last_seen': datetime|None, 'pid': int|None, 'stale': bool}.

    Stale when last_seen is older than WORKER_HEARTBEAT_STALE_S or
    missing entirely. Never raises — settings_get returns None on fixture
    mode and we treat it as stale.
    """
    val = None
    try:
        val = settings_get('worker_heartbeat')
    except Exception:
        pass

    if not isinstance(val, dict) or not val.get('last_seen'):
        return {'last_seen': None, 'pid': None, 'stale': True}

    last_seen: datetime | None = None
    try:
        raw = val['last_seen']
        # Postgres can hand back already-parsed datetime via jsonb -> Python
        # depending on the driver path; handle both.
        if isinstance(raw, datetime):
            last_seen = raw
        else:
            last_seen = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except Exception:
        last_seen = None

    if last_seen is None:
        return {'last_seen': None, 'pid': val.get('pid'), 'stale': True}

    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)

    age = (datetime.now(timezone.utc) - last_seen).total_seconds()
    return {
        'last_seen': last_seen,
        'pid':       val.get('pid'),
        'stale':     age > WORKER_HEARTBEAT_STALE_S,
    }


# ── System status ────────────────────────────────────────────────────
# Surfaced read-only on the Settings page. Avoid heavy queries here —
# the page is a glance-and-go status board, not a metrics dashboard.

def system_status() -> dict:
    """Compose a system-status snapshot. Each block fails soft."""
    snap = {
        'web_service':  {'state': 'healthy', 'detail': 'Render web · gunicorn'},
        'database':     {'state': 'healthy', 'detail': '—'},
        'worker':       {'state': 'idle',    'detail': 'No worker yet — pipeline runs in-process'},
        'email_queue':  {'state': 'healthy', 'detail': '0 pending · 0 failed'},
        'build':        {'state': 'healthy', 'detail': _build_signature()},
        'app_version':  {'state': 'healthy', 'detail': 'v0.10.0'},
    }
    # DB ping
    if _settings_use_fixture():
        snap['database'] = {'state': 'idle',
                            'detail': 'No DATABASE_URL — fixture mode (local POC)'}
    else:
        try:
            t0 = datetime.now()
            fetch_one('select 1 as ok')
            ms = int((datetime.now() - t0).total_seconds() * 1000)
            snap['database'] = {'state': 'healthy',
                                'detail': f'Connected · supabase pooler · {ms}ms'}
        except Exception as e:
            snap['database'] = {'state': 'down',
                                'detail': str(e).splitlines()[0][:120]}

    # Email queue (only if DB is up)
    if not _settings_use_fixture():
        try:
            r = fetch_one("""
                select
                  count(*) filter (where status = 'scheduled')                  as pending,
                  count(*) filter (where status = 'failed')                     as failed,
                  max(sent_at)                                                  as last_sent
                from crm.emails
            """) or {}
            pending = int(r.get('pending') or 0)
            failed  = int(r.get('failed')  or 0)
            state   = 'down' if failed else ('warn' if pending > 200 else 'healthy')
            snap['email_queue'] = {
                'state':  state,
                'detail': f'{pending:,} pending · {failed:,} failed',
            }
        except Exception:
            pass

    # Worker — heartbeat is the source of truth. The activity_log fallback
    # only mattered when there was no worker process; now that crm-worker
    # runs continuously, a missing heartbeat is a real signal it's down.
    if not _settings_use_fixture():
        hb = worker_heartbeat()
        if hb['stale']:
            detail = 'No heartbeat in 3 min — check `systemctl status crm-worker`'
            if hb['last_seen']:
                detail = f'Stale · last seen {relative_time(hb["last_seen"])}'
            snap['worker'] = {'state': 'down', 'detail': detail}
        else:
            pid_suffix = f' · pid {hb["pid"]}' if hb.get('pid') else ''
            snap['worker'] = {
                'state':  'healthy',
                'detail': f'Last beat {relative_time(hb["last_seen"])}{pid_suffix}',
            }

    return snap


def _build_signature() -> str:
    """Best-effort short build identifier — git short SHA + commit time."""
    try:
        import subprocess
        out = subprocess.run(
            ['git', '-C', _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
             'log', '-1', '--format=%h · %ar'],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return 'unknown · local'


# ── Integrations / API keys panel ────────────────────────────────────

# (env_name, friendly label, hostname/short identifier, prefix retained)
_INTEGRATION_KEYS = [
    ('ANTHROPIC_API_KEY',       'Anthropic Claude',       'api.anthropic.com',                     'sk-ant-'),
    ('COMPANIES_HOUSE_API_KEY', 'Companies House API',    'api.company-information.service.gov.uk',''),
    ('DATABASE_URL',            'Supabase Postgres',      'pooler.supabase.co',                    ''),
    ('RESEND_API_KEY',          'Resend (transactional)', 'api.resend.com',                        're_'),
    ('SLACK_WEBHOOK_URL',       'Slack webhook',          'hooks.slack.com',                       ''),
]

# Keys whose absence blocks real pipeline runs. Surface these as 'missing'
# (red) instead of 'idle' (grey) so an operator can't ship without them.
# Google Places is no longer here — discovery now runs via the free
# scraper (scraper/google_places_free.py), no key required.
# Apollo also no longer here — replaced by CH officers + email pattern
# detection (pipeline/sources/decision_maker.py), free.
_REQUIRED_FOR_PIPELINE = {
    'ANTHROPIC_API_KEY',
    'COMPANIES_HOUSE_API_KEY',
}

# Worker env file location. Pipeline keys (Anthropic, Apollo, Google
# Places, Companies House) live here — not in the web process's env —
# because the worker is the one that actually calls those APIs. The
# settings page reads both sources so the operator sees the unified
# "is the pipeline ready?" answer rather than "what does the web
# process happen to see?".
_WORKER_ENV_PATH = '/etc/innovite/crm-worker.env'
_WORKER_ENV_TTL_S = 60.0
_worker_env_cache: tuple[float, dict[str, str]] = (0.0, {})
_worker_env_lock = _threading.Lock()


def _load_worker_env() -> dict[str, str]:
    """Parse /etc/innovite/crm-worker.env for the settings panel.

    Cached 60s — the file rarely changes and we don't want stat() per
    integration row per render. Silently returns {} if the file isn't
    readable; the web process runs as `deploy` and the file is
    600 deploy:deploy, so unreadable means something is wrong with
    deploy and the settings page should still render."""
    global _worker_env_cache
    now = _time.time()
    cached_at, cached = _worker_env_cache
    if now - cached_at < _WORKER_ENV_TTL_S:
        return cached
    with _worker_env_lock:
        cached_at, cached = _worker_env_cache
        if now - cached_at < _WORKER_ENV_TTL_S:
            return cached
        parsed: dict[str, str] = {}
        try:
            with open(_WORKER_ENV_PATH, 'r', encoding='utf-8') as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    k, _, v = line.partition('=')
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k:
                        parsed[k] = v
        except (FileNotFoundError, PermissionError):
            pass
        except Exception:
            pass  # never let this crash the settings page
        _worker_env_cache = (now, parsed)
        return parsed


def _resolve_env(name: str) -> str:
    """Resolve an env var by checking the web process's env first, then
    the worker env file. Empty string if neither has it."""
    return _os.environ.get(name) or _load_worker_env().get(name, '')


def _mask_key(env_name: str, prefix: str) -> tuple[str, bool]:
    """Return (display_value, is_real).

    When the env var is set (in either web or worker env), show
    `<prefix>•••••<last4>`. When missing, show a plain em-dash —
    never a plausible-looking fake value. (Anti-pattern #10 in
    CLAUDE.md: synthetic placeholder data must never present as real.)"""
    raw = _resolve_env(env_name)
    if raw:
        last4 = raw[-4:] if len(raw) >= 4 else raw
        if env_name == 'SLACK_WEBHOOK_URL':
            # Webhooks are URLs — show channel-like fragment, never the path token.
            tail = raw.rsplit('/', 1)[-1][-6:] or last4
            return f'…/{tail}', True
        if env_name == 'DATABASE_URL':
            # Show host only — never any creds.
            try:
                from urllib.parse import urlparse
                host = urlparse(raw).hostname or 'connected'
                return host, True
            except Exception:
                return 'connected', True
        return f'{prefix}•••••{last4}', True
    return '—', False


def integration_keys() -> list[dict]:
    """One row per integration, masked for display."""
    out = []
    for env_name, label, host, prefix in _INTEGRATION_KEYS:
        masked, is_real = _mask_key(env_name, prefix)
        if is_real:
            state, detail = 'healthy', 'Active'
        elif env_name in _REQUIRED_FOR_PIPELINE:
            state, detail = 'missing', 'Missing — pipeline will not run'
        else:
            state, detail = 'idle', 'Not configured'
        out.append({
            'env_name': env_name,
            'label':    label,
            'host':     host,
            'masked':   masked,
            'state':    state,
            'detail':   detail,
            'is_real':  is_real,
        })
    # Plausible doesn't use a key — surface as a separate row
    out.append({
        'env_name': '',
        'label':    'Plausible analytics',
        'host':     'plausible.io',
        'masked':   'innoviteai.com',
        'state':    'healthy',
        'detail':   'Receiving events',
        'is_real':  True,
    })
    return out


# ── Mailboxes (Step 7 backbone) ──────────────────────────────────────
# Mailboxes are first-class. Pool vs dedicated assignment is encoded
# by mailboxes.dedicated_client_id (NULL = pool). Daily cap is per
# mailbox; the send scheduler picks healthy mailboxes with capacity to
# hit each client's per-day target.

_HEALTH_NEEDS_ATTENTION = ('throttled', 'disconnected')


def mailboxes_all() -> list[dict]:
    """Every mailbox with domain + dedicated-client name joined for display."""
    return fetch_all("""
        select
          m.id,
          m.address,
          m.daily_cap,
          m.sent_today,
          m.warmup_provider,
          m.warmup_day,
          m.warmup_target,
          m.warmup_status,
          m.health_state,
          m.last_error,
          m.last_error_at,
          m.paused,
          sd.domain,
          sd.dns_verified,
          sd.spf_verified,
          sd.dkim_verified,
          sd.dmarc_verified,
          c.id   as dedicated_client_id,
          c.name as dedicated_client_name
        from crm.mailboxes m
        join crm.sending_domains sd on sd.id = m.sending_domain_id
        left join crm.clients c on c.id = m.dedicated_client_id
        order by sd.domain, m.address
    """)


def sending_domains_summary() -> list[dict]:
    """One row per domain with mailbox-state rollups for the page strip."""
    return fetch_all("""
        select
          sd.id,
          sd.domain,
          sd.dns_verified,
          sd.spf_verified,
          sd.dkim_verified,
          sd.dmarc_verified,
          count(m.id)                                                       as mailbox_count,
          count(*) filter (where m.health_state = 'healthy')                as healthy_count,
          count(*) filter (where m.health_state = 'warming')                as warming_count,
          count(*) filter (where m.health_state in ('throttled','disconnected')) as needs_attention_count,
          count(*) filter (where m.health_state = 'paused')                 as paused_count
        from crm.sending_domains sd
        left join crm.mailboxes m on m.sending_domain_id = sd.id
        group by sd.id, sd.domain, sd.dns_verified, sd.spf_verified,
                 sd.dkim_verified, sd.dmarc_verified
        order by sd.domain
    """)


def mailboxes_summary() -> dict:
    """Headline counts for the page sub-header and the Settings summary card."""
    row = fetch_one("""
        select
          count(*)                                                          as total,
          count(*) filter (where health_state = 'healthy')                  as healthy,
          count(*) filter (where health_state = 'warming')                  as warming,
          count(*) filter (where health_state in ('throttled','disconnected')) as needs_attention,
          count(*) filter (where health_state = 'paused')                   as paused,
          coalesce(sum(daily_cap) filter (where health_state in ('healthy','warming')), 0) as total_capacity,
          coalesce(sum(sent_today) filter (where health_state in ('healthy','warming')), 0) as total_sent_today
        from crm.mailboxes
    """) or {}
    return {
        'total':            int(row.get('total') or 0),
        'healthy':          int(row.get('healthy') or 0),
        'warming':          int(row.get('warming') or 0),
        'needs_attention':  int(row.get('needs_attention') or 0),
        'paused':           int(row.get('paused') or 0),
        'total_capacity':   int(row.get('total_capacity') or 0),
        'total_sent_today': int(row.get('total_sent_today') or 0),
    }


# ── Vidora Media ─────────────────────────────────────────────────────
# Lead writes for Media Mode — Google Places discovery + IG snapshot +
# Claude Vision content audit. Pipeline lives in crm/pipeline + crm/scraper
# (instagram_snapshot, instagram_link, vidora_audit). See migration
# 0034_vidora_bridge.sql for the schema additions (external_id,
# vidora_data jsonb, source enum).

_VIDORA_CLIENT_NAME = 'Vidora Media'


def vidora_client_id() -> int:
    """Resolve the Vidora Media client_id. Migration 0034 ensures the row
    exists; we still SELECT each call rather than caching so a manual rename
    in the dashboard doesn't strand the bridge."""
    row = fetch_one(
        "select id from crm.clients where name = %s",
        (_VIDORA_CLIENT_NAME,),
    )
    if not row:
        raise RuntimeError(
            f"Vidora client ('{_VIDORA_CLIENT_NAME}') not found — apply migration 0034."
        )
    return int(row['id'])


def _parse_posts_per_week(freq: str | None) -> float | None:
    """Vidora stores posting_frequency as a string ('3.5/week', 'daily',
    'rare'). Innovite's column is numeric. Best-effort coerce — anything
    we can't parse stays in vidora_data."""
    if not freq:
        return None
    s = str(freq).strip().lower()
    if s in ('daily', 'every day'):
        return 7.0
    if 'rare' in s or 'inactive' in s or 'dead' in s:
        return 0.0
    # Pull the first numeric token.
    import re
    m = re.search(r'(\d+\.?\d*)', s)
    return float(m.group(1)) if m else None


def _empty_to_none(v):
    """Vidora often emits '' for missing scalar fields. Postgres rejects
    empty strings for date/numeric columns, so coerce '' → None before
    insert. Pass-through for everything else."""
    if isinstance(v, str) and v.strip() == "":
        return None
    return v


def _to_int(v):
    """Coerce to int or None. Tolerates strings, floats, '' and None."""
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _to_float(v):
    """Coerce to float or None. Tolerates strings, '' and None."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def upsert_vidora_lead(payload: dict, pdf_path: str | None) -> int:
    """Insert or update a lead from a Vidora pipeline result.

    Idempotent on (source='vidora_instagram', external_id=<username>).
    Returns the Innovite leads.id.
    """
    client_id = vidora_client_id()
    username = (payload.get('username') or '').strip()
    if not username:
        raise ValueError("vidora lead missing 'username'")

    competitors = payload.get('competitors') or []
    def _comp(i: int, k: str):
        return competitors[i].get(k) if i < len(competitors) and isinstance(competitors[i], dict) else None

    row = {
        'client_id':                  client_id,
        'external_id':                username,
        'source':                     'vidora_instagram',
        'business_name':              payload.get('business_name') or username,
        'address':                    payload.get('maps_address'),
        'phone':                      payload.get('maps_phone'),
        'website':                    payload.get('maps_website'),
        'email':                      payload.get('email'),
        'google_rating':              _to_float(payload.get('maps_rating')),
        'google_review_count':        _to_int(payload.get('maps_review_count')),
        'google_maps_url':            payload.get('maps_url'),
        'instagram_handle':           username,
        'instagram_followers':        _to_int(payload.get('followers')),
        'instagram_engagement_rate':  _to_float(payload.get('engagement_rate')),
        'instagram_posts_per_week':   _parse_posts_per_week(payload.get('posting_frequency')),
        'instagram_avg_likes':        _to_int(payload.get('avg_likes')),
        'instagram_last_post_date':   _empty_to_none(payload.get('last_post_date')),
        'website_score':              _to_int((payload.get('website_analysis') or {}).get('score')),
        'grade':                      _empty_to_none(payload.get('lead_grade')),
        'overall_score':              _to_int(payload.get('overall_score')),
        'weakness_profile':           json.dumps(payload.get('top_weaknesses') or payload.get('weaknesses') or []),
        'competitor_1_name':          _comp(0, 'name'),
        'competitor_1_reviews':       _to_int(_comp(0, 'review_count') or _comp(0, 'reviews')),
        'competitor_1_score':         _to_int(_comp(0, 'score')),
        'competitor_2_name':          _comp(1, 'name'),
        'competitor_2_reviews':       _to_int(_comp(1, 'review_count') or _comp(1, 'reviews')),
        'competitor_2_score':         _to_int(_comp(1, 'score')),
        'competitor_3_name':          _comp(2, 'name'),
        'competitor_3_reviews':       _to_int(_comp(2, 'review_count') or _comp(2, 'reviews')),
        'competitor_3_score':         _to_int(_comp(2, 'score')),
        'email_subject':              payload.get('email_subject'),
        'email_body_day1':            payload.get('email_body'),
        'pdf_path':                   pdf_path,
        'vidora_data':                json.dumps({
            'scores':                       payload.get('scores'),
            'sales_notes':                  payload.get('sales_notes'),
            'personalised_pitch':           payload.get('personalised_pitch'),
            'business_intent_score':        payload.get('business_intent_score'),
            'business_type':                payload.get('business_type'),
            'location_match':               payload.get('location_match'),
            'location_signals':             payload.get('location_signals'),
            'selling_signals':              payload.get('selling_signals'),
            'priority_flag':                payload.get('priority_flag'),
            'upgrade_potential':            payload.get('upgrade_potential'),
            'estimated_audience_size':      payload.get('estimated_audience_size'),
            'competitor_avg_score':         payload.get('competitor_avg_score'),
            'competitor_benchmark':         payload.get('competitor_benchmark'),
            'has_link_in_bio':              payload.get('has_link_in_bio'),
            'bio_text':                     payload.get('bio_text'),
            'bio_website':                  payload.get('bio_website'),
            'avg_comments':                 payload.get('avg_comments'),
            'post_count':                   payload.get('post_count'),
            'story_highlight_categories':   payload.get('story_highlight_categories'),
            'trend':                        payload.get('trend'),
            'analysed_at':                  payload.get('analysed_at'),
        }),
    }

    cols = list(row.keys())
    placeholders = ','.join(['%s'] * len(cols))
    # Update everything except client_id (immutable) and external_id (the key).
    update_cols = [c for c in cols if c not in ('client_id', 'external_id', 'source')]
    update_clause = ','.join(f"{c} = excluded.{c}" for c in update_cols)
    sql = (
        f"insert into crm.leads ({','.join(cols)}) values ({placeholders}) "
        f"on conflict (source, external_id) where external_id is not null "
        f"do update set {update_clause} "
        f"returning id"
    )
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, [row[c] for c in cols])
            lead_id = cur.fetchone()[0]
        conn.commit()
    return int(lead_id)
