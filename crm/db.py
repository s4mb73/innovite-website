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
    """Scheduled sends for today (calendar day, Europe/London)."""
    extra = [
        "e.status = 'scheduled'",
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
    """Recently sent emails — last 30 days, capped at `limit` rows."""
    extra = [
        "e.status = 'sent'",
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
    return rows


def outreach_followups(client_id: int | None = None, search: str | None = None) -> list[dict]:
    """Day-3 / Day-7 emails scheduled within the next 7 days (excluding today)."""
    extra = [
        "e.status = 'scheduled'",
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
