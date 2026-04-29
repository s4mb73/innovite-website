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
    """All clients with embedded 7-day metrics + change vs prior 7d."""
    sql = """
        select
          c.id, c.name, c.industry, c.contact_name, c.contact_email,
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
                                      and e.replied_at <  now() - interval '7 days')         as replied_7d_prev
        from crm.clients c
        order by
          case c.status when 'active' then 0 when 'paused' then 1 else 2 end,
          c.created_at desc
    """
    rows = fetch_all(sql)
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
    return rows


# ── Client detail (Step 4 part 2) ────────────────────────────────────
def get_client(client_id: int) -> dict | None:
    sql = """
        select id, name, industry, contact_name, contact_email,
               monthly_fee, pricing_tier, status,
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
    'closed':    'grey',
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
LEAD_STATUSES = ['new', 'contacted', 'replied', 'meeting', 'won', 'lost', 'closed']

SORT_SQL = {
    'recent': 'l.created_at desc',
    'grade':  "case l.grade when 'A' then 1 when 'B' then 2 when 'C' then 3 when 'D' then 4 when 'F' then 5 else 6 end, l.created_at desc",
    'score':  'coalesce(l.overall_score, 0) desc, l.created_at desc',
}


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
    order_sql = SORT_SQL.get(sort, SORT_SQL['recent'])
    page = max(1, page)
    offset = (page - 1) * page_size
    params['limit'] = page_size
    params['offset'] = offset

    rows = fetch_all(f"""
        select l.id, l.business_name, l.grade, l.status, l.overall_score,
               l.decision_maker_name, l.google_review_count, l.created_at,
               c.id as client_id, c.name as client_name
          from crm.leads l
          left join crm.clients c on c.id = l.client_id
         where {where_sql}
         order by {order_sql}
         limit %(limit)s offset %(offset)s
    """, params)

    for r in rows:
        r['grade_colour']  = GRADE_COLOUR.get(r.get('grade') or '', 'grey')
        r['status_colour'] = STATUS_COLOUR.get(r.get('status') or '', 'grey')
        r['relative']      = relative_time(r['created_at'])

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
    return fetch_all("select id, name from crm.clients order by name")


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
