"""Innovite CRM — Flask web UI.

Single-password session auth via the APP_PASSWORD env var. Sessions are
signed with FLASK_SECRET_KEY, served Secure+HttpOnly+SameSite=Lax over
HTTPS (nginx terminates TLS; ProxyFix trusts X-Forwarded-Proto).
"""
import csv
import io
import json
import os
import time
from hmac import compare_digest

from flask import Flask, Response, abort, flash, jsonify, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, URLSafeSerializer
from psycopg import errors as psycopg_errors
from werkzeug.middleware.proxy_fix import ProxyFix

import db

UNDO_TTL_SECONDS = 60

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'dev-only-change-in-prod')

# Session cookie hardening. Safe behind nginx → 127.0.0.1:8080 with TLS at the edge.
app.config['SESSION_COOKIE_SECURE']   = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# nginx sends X-Forwarded-Proto=https; ProxyFix makes url_for / request.is_secure honour it.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


@app.template_filter('relative_time')
def _jinja_relative_time(dt):
    """`{{ ts|relative_time }}` → '4m ago' / '2h ago'. None/empty → '—'.
    Wraps db.relative_time so templates can render timestamps without
    the route having to pre-format each one."""
    if not dt:
        return '—'
    return db.relative_time(dt)

# Endpoint names (not URL paths) that don't require auth.
# unsubscribe is public because Gmail/Yahoo fire RFC 8058 one-click POSTs
# without any cookie context — they MUST work without a session.
_PUBLIC_ENDPOINTS = {'login', 'logout', 'healthz', 'static', 'unsubscribe', 'favicon'}


@app.before_request
def _require_login():
    # Skip if the endpoint is public (login page, healthz for monitoring, static).
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return None
    # Already authed — let the request through.
    if session.get('auth') is True:
        return None
    # API endpoints get a 401 (so fetch() callers see the failure cleanly);
    # everything else gets bounced to the login page with `next` set.
    if request.path.startswith('/api/'):
        return jsonify({'error': 'unauthorized'}), 401
    return redirect(url_for('login', next=request.full_path if request.query_string else request.path))


@app.route('/login', methods=['GET', 'POST'])
def login():
    expected = os.environ.get('APP_PASSWORD') or ''
    error = None
    if request.method == 'POST':
        attempted = (request.form.get('password') or '').encode('utf-8')
        if expected and compare_digest(attempted, expected.encode('utf-8')):
            session.clear()
            session['auth'] = True
            session.permanent = True
            # Honour the `next` param but only if it's a local path (open-redirect guard).
            nxt = request.args.get('next') or request.form.get('next') or url_for('inbox')
            if not nxt.startswith('/') or nxt.startswith('//'):
                nxt = url_for('inbox')
            return redirect(nxt)
        # Constant-ish delay to dampen brute-force feedback.
        time.sleep(0.3)
        error = 'Wrong password.'
    return render_template('login.html', error=error, next=request.args.get('next', ''))


@app.route('/logout', methods=['POST', 'GET'])
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
def home():
    # Inbox is the new daily entry. Overview lives on at /overview for the
    # deeper "state of every client" view (chart + activity feed).
    return redirect(url_for('inbox'))


@app.route('/overview')
def overview():
    db_error = None
    metrics = {
        'total_leads': 0, 'total_leads_delta': 0,
        'emails_week': 0, 'emails_week_delta': 0,
        'reply_rate': 0.0, 'reply_rate_delta': 0.0,
        'meetings_month': 0, 'meetings_month_delta': 0,
    }
    chart_labels: list[str] = []
    chart_values: list[int] = []
    activity: list[dict] = []
    finder_clients: list[dict] = []
    scraper_health: dict = {'total': 0}
    try:
        metrics = db.dashboard_metrics()
        chart_labels, chart_values = db.leads_per_day(7)
        activity = db.recent_activity(20)
        finder_clients = db.clients_for_finder()
        scraper_health = db.scraper_health_summary(200)
    except Exception as e:
        # Don't 500 the page on a DB blip — render the empty state with a banner.
        db_error = str(e).splitlines()[0][:240]
    return render_template(
        'overview.html',
        active='overview',
        metrics=metrics,
        chart_labels=chart_labels,
        chart_values=chart_values,
        activity=activity,
        finder_clients=finder_clients,
        scraper_health=scraper_health,
        db_error=db_error,
    )


@app.route('/clients')
def clients():
    db_error = None
    rows: list[dict] = []
    try:
        rows = db.list_clients()
    except Exception as e:
        db_error = str(e).splitlines()[0][:240]
    return render_template('clients.html', active='clients', clients=rows, db_error=db_error)


def _split_chips(raw: str) -> list[str]:
    """Chip inputs serialise as comma-joined strings. Split, strip, dedupe
    while preserving order. Used for target_industries, target_locations,
    and the exclusion_list on the new-client form."""
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for piece in raw.split(','):
        v = piece.strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out


@app.route('/clients/new', methods=['GET', 'POST'])
def client_new():
    """New-client form (US-017). GET renders empty; POST validates,
    inserts, and redirects to the new client's detail page on success.
    Validation errors re-render the form with the entered data preserved
    and inline messages on the affected fields."""
    if request.method == 'GET':
        return render_template(
            'client_new.html',
            active='clients',
            employee_bands=db.EMPLOYEE_BANDS,
            form={'active_filing_only': True},  # Default checkbox ON
            errors={},
        )

    form_in, errors, industries, locations, targeting_filters = _parse_client_form(request)
    if errors:
        return render_template(
            'client_new.html',
            active='clients',
            employee_bands=db.EMPLOYEE_BANDS,
            form=form_in,
            errors=errors,
        ), 400
    try:
        new_id = db.create_client(
            name=form_in['name'],
            industry=form_in['industry'],
            contact_name=form_in['contact_name'],
            contact_email=form_in['contact_email'],
            target_industries=industries,
            target_locations=locations,
            targeting_filters=targeting_filters,
        )
    except psycopg_errors.UniqueViolation:
        errors['name'] = 'A client with this name already exists.'
        return render_template(
            'client_new.html',
            active='clients',
            employee_bands=db.EMPLOYEE_BANDS,
            form=form_in,
            errors=errors,
        ), 400
    except Exception as e:
        flash(f'Could not create client: {e}', 'error')
        return render_template(
            'client_new.html',
            active='clients',
            employee_bands=db.EMPLOYEE_BANDS,
            form=form_in,
            errors=errors,
        ), 500

    # The wizard's step 3 has two submit buttons:
    #   'save_and_run' (primary, default) — queue the first pipeline run
    #   'save_only'                       — land on client detail, no run
    # Legacy callers that don't post a save_action default to save_and_run
    # so the existing redirect-to-approvals UX is preserved.
    save_action = (request.form.get('save_action') or 'save_and_run').strip()

    if save_action == 'save_only':
        flash(
            f"{form_in['name']} added. Click Find new leads when you're ready "
            f"to start the first pipeline run.",
            'success',
        )
        return redirect(url_for('client_detail', client_id=new_id))

    # Save & run: enqueue the first pipeline run immediately so the
    # operator lands on /approvals already populating instead of having
    # to click 'Find new leads' from client detail. The worker picks the
    # row up; the new-client redirect doesn't wait on it.
    try:
        db.fetch_one(
            """insert into crm.pipeline_runs (client_id, status, mode, triggered_by)
               values (%s, 'pending', 'onboarding', 'operator')
               returning id""",
            (new_id,),
        )
        flash(
            f"{form_in['name']} added. First pipeline run queued — "
            f"drafts will appear in Approvals as the run completes.",
            'success',
        )
        return redirect(url_for('campaigns', client=new_id, tab='pending'))
    except Exception as e:
        # Client was created; only the run-enqueue failed. Land on the
        # client page so the operator can retry from there.
        flash(
            f"{form_in['name']} added, but couldn't queue the first pipeline run: {e}. "
            f"Click Find new leads from the client page when ready.",
            'error',
        )
        return redirect(url_for('client_detail', client_id=new_id))


def _parse_client_form(request):
    """Extract + validate the new/edit client form. Returns (form_in, errors,
    industries, locations, targeting_filters). Used by both client_new and
    client_edit so the validation rules can't drift between them."""
    form_in = {
        'name':               (request.form.get('name')          or '').strip(),
        'industry':           (request.form.get('industry')      or '').strip(),
        'contact_name':       (request.form.get('contact_name')  or '').strip(),
        'contact_email':      (request.form.get('contact_email') or '').strip(),
        'target_industries':  (request.form.get('target_industries') or '').strip(),
        'target_locations':   (request.form.get('target_locations')  or '').strip(),
        'min_company_age':    (request.form.get('min_company_age')   or '').strip(),
        'employee_bands':     request.form.getlist('employee_bands'),
        'active_filing_only': _truthy_form('active_filing_only'),
        'exclusion_list':     (request.form.get('exclusion_list') or '').strip(),
    }
    industries = _split_chips(form_in['target_industries'])
    locations  = _split_chips(form_in['target_locations'])
    exclusions = _split_chips(form_in['exclusion_list'])
    age_raw    = form_in['min_company_age']
    age        = int(age_raw) if age_raw.isdigit() else None
    bands      = [b for b in form_in['employee_bands'] if b in db.EMPLOYEE_BANDS]

    errors: dict[str, str] = {}
    if not form_in['name']:
        errors['name'] = 'Name is required.'
    if not industries:
        errors['target_industries'] = 'Add at least one target industry.'
    if not locations:
        errors['target_locations'] = 'Add at least one target location.'
    if form_in['contact_email'] and '@' not in form_in['contact_email']:
        errors['contact_email'] = "That doesn't look like an email address."
    if age is not None and (age < 0 or age > 200):
        errors['min_company_age'] = 'Pick a value between 0 and 200.'

    targeting_filters = {
        'min_company_age_years': age,
        'employee_bands':        bands,
        'active_filing_only':    form_in['active_filing_only'],
        'exclusion_list':        exclusions,
    }
    return form_in, errors, industries, locations, targeting_filters


@app.route('/clients/<int:client_id>/edit', methods=['GET', 'POST'])
def client_edit(client_id: int):
    """Edit a client's basics + targeting. Mirrors /clients/new but updates
    in place. Re-uses client_new.html via the is_edit flag."""
    existing = db.get_client(client_id)
    if existing is None:
        abort(404)

    if request.method == 'GET':
        # Pre-fill the form from the existing client's saved values.
        tf = existing.get('targeting_filters') or {}
        form = {
            'name':               existing.get('name') or '',
            'industry':           existing.get('industry') or '',
            'contact_name':       existing.get('contact_name') or '',
            'contact_email':      existing.get('contact_email') or '',
            'target_industries':  ','.join(existing.get('target_industries') or []),
            'target_locations':   ','.join(existing.get('target_locations') or []),
            'min_company_age':    str(tf.get('min_company_age_years')) if tf.get('min_company_age_years') is not None else '',
            'employee_bands':     tf.get('employee_bands') or [],
            'active_filing_only': bool(tf.get('active_filing_only')) if 'active_filing_only' in tf else True,
            'exclusion_list':     ','.join(tf.get('exclusion_list') or []),
        }
        return render_template(
            'client_new.html',
            active='clients',
            employee_bands=db.EMPLOYEE_BANDS,
            form=form,
            errors={},
            is_edit=True,
            client_id=client_id,
            client_name=existing['name'],
        )

    form_in, errors, industries, locations, targeting_filters = _parse_client_form(request)
    if errors:
        return render_template(
            'client_new.html',
            active='clients',
            employee_bands=db.EMPLOYEE_BANDS,
            form=form_in,
            errors=errors,
            is_edit=True,
            client_id=client_id,
            client_name=existing['name'],
        ), 400

    try:
        db.update_client(
            client_id,
            name=form_in['name'],
            industry=form_in['industry'],
            contact_name=form_in['contact_name'],
            contact_email=form_in['contact_email'],
            target_industries=industries,
            target_locations=locations,
            targeting_filters=targeting_filters,
        )
    except psycopg_errors.UniqueViolation:
        errors['name'] = 'Another client already has this name.'
        return render_template(
            'client_new.html',
            active='clients',
            employee_bands=db.EMPLOYEE_BANDS,
            form=form_in,
            errors=errors,
            is_edit=True,
            client_id=client_id,
            client_name=existing['name'],
        ), 400

    flash(f"{form_in['name']} updated.", 'success')
    return redirect(url_for('client_detail', client_id=client_id))


@app.route('/clients/<int:client_id>/schedule', methods=['POST'])
def client_schedule(client_id: int):
    """Set/clear the daily pipeline schedule for a client (US-002).

    Form fields:
      run_at        — 'HH:MM' or '' to disable
      paused        — checkbox truthy to pause without losing the time
    """
    existing = db.get_client_schedule(client_id)
    if existing is None:
        abort(404)

    raw = (request.form.get('run_at') or '').strip()
    paused = _truthy_form('paused')

    run_at: str | None
    if not raw:
        run_at = None
    else:
        # Accept HH:MM or HH:MM:SS; reject anything else cleanly.
        parts = raw.split(':')
        if len(parts) < 2 or not all(p.isdigit() for p in parts[:2]):
            flash('Schedule time must be HH:MM (24-hour).', 'error')
            return redirect(url_for('client_detail', client_id=client_id))
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            flash('Schedule time must be 00:00 – 23:59.', 'error')
            return redirect(url_for('client_detail', client_id=client_id))
        run_at = f'{h:02d}:{m:02d}'

    db.set_client_schedule(client_id, run_at, paused)
    if run_at and not paused:
        flash(f'Daily run scheduled at {run_at} Europe/London.', 'success')
    elif run_at and paused:
        flash(f'Schedule {run_at} saved but paused.', 'success')
    else:
        flash('Daily schedule cleared.', 'success')
    return redirect(url_for('client_detail', client_id=client_id))


@app.route('/clients/<int:client_id>')
def client_detail(client_id: int):
    db_error = None
    client = None
    stats = {
        'total_leads': 0, 'total_leads_delta': 0,
        'reply_rate': 0.0, 'reply_rate_delta': 0.0,
        'meetings_month': 0, 'meetings_month_delta': 0,
    }
    leads: list[dict] = []
    pipeline_runs: list[dict] = []
    try:
        client = db.get_client(client_id)
        if client is None:
            abort(404)
        stats = db.client_stats(client_id)
        leads = db.client_recent_leads(client_id, 10)
        pipeline_runs = db.client_pipeline_runs(client_id, days=30, limit=20)
    except Exception as e:
        # 404s should propagate; everything else degrades gracefully.
        if hasattr(e, 'code') and e.code == 404:
            raise
        db_error = str(e).splitlines()[0][:240]
    return render_template(
        'client_detail.html',
        active='clients',
        client=client,
        stats=stats,
        leads=leads,
        pipeline_runs=pipeline_runs,
        db_error=db_error,
    )


def _lead_filters_from_request():
    """Pull filter / search / sort / page from request.args. Centralised
    so /leads, /leads.csv, and the bulk-action POST all parse identically.
    """
    args = request.args
    status = args.get('status') or None
    if status not in db.LEAD_STATUSES and status != 'all':
        status = None
    if status == 'all':
        status = None
    client_raw = args.get('client')
    client_id = int(client_raw) if (client_raw or '').isdigit() else None
    search = (args.get('q') or '').strip() or None
    sort = args.get('sort') or 'triage'
    if sort not in db.SORT_SQL:
        sort = 'triage'
    page = int(args.get('page') or 1)
    return {'status': status, 'client_id': client_id, 'search': search, 'sort': sort, 'page': page}


@app.route('/leads')
def leads():
    db_error = None
    result = {'rows': [], 'total': 0, 'counts': {s: 0 for s in db.LEAD_STATUSES} | {'all': 0},
              'page': 1, 'pages': 1, 'page_size': 50, 'page_start': 0, 'page_end': 0}
    clients_min: list[dict]   = []
    client_tabs: list[dict]   = []
    active_client: dict | None = None
    scraper_offline = False
    f = _lead_filters_from_request()

    try:
        clients_min = db.all_clients_min()
        client_tabs = db.leads_count_per_client(status=f['status'], search=f['search'])
        scraper_offline = db.scraper_offline_now()

        # Default to first active client if none specified — the page is
        # always scoped to one client (no "all clients" view).
        if f['client_id'] is None and client_tabs:
            f['client_id'] = client_tabs[0]['id']

        if f['client_id'] is not None:
            active_client = next((c for c in client_tabs if c['id'] == f['client_id']), None)
            if active_client is None:
                # Filter named a client that doesn't exist — fall back to first.
                f['client_id'] = client_tabs[0]['id'] if client_tabs else None
                active_client = client_tabs[0] if client_tabs else None

        if f['client_id'] is not None:
            result = db.leads_search(**f, page_size=50)
    except Exception as e:
        db_error = str(e).splitlines()[0][:240]

    return render_template(
        'leads.html',
        active='leads',
        result=result,
        clients_min=clients_min,
        client_tabs=client_tabs,
        active_client=active_client,
        f=f,
        active_status=request.args.get('status', 'all'),
        scraper_offline=scraper_offline,
        db_error=db_error,
    )


@app.route('/leads.csv')
def leads_csv():
    f = _lead_filters_from_request()
    try:
        rows = db.leads_for_csv(status=f['status'], client_id=f['client_id'], search=f['search'])
    except Exception as e:
        return Response(f'Could not generate export: {e}', status=502, mimetype='text/plain')

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['business_name', 'client', 'grade', 'status', 'score',
                'reviews', 'decision_maker', 'email', 'phone', 'website', 'city', 'added'])
    for r in rows:
        w.writerow([
            r.get('business_name') or '',
            r.get('client_name') or '',
            r.get('grade') or '',
            r.get('status') or '',
            r.get('overall_score') if r.get('overall_score') is not None else '',
            r.get('google_review_count') if r.get('google_review_count') is not None else '',
            r.get('decision_maker_name') or '',
            r.get('email') or '',
            r.get('phone') or '',
            r.get('website') or '',
            r.get('city') or '',
            r['created_at'].isoformat() if r.get('created_at') else '',
        ])
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename="innovite-leads.csv"'},
    )


@app.route('/leads/bulk-status', methods=['POST'])
def leads_bulk_status():
    new_status = (request.form.get('status') or '').strip()
    ids_raw = request.form.getlist('ids')
    ids = [int(x) for x in ids_raw if x.isdigit()]
    if not ids or new_status not in db.LEAD_STATUSES:
        flash('Nothing to update — pick a status and select at least one lead.', 'error')
        return redirect(request.referrer or url_for('leads'))
    try:
        result = db.bulk_change_status(ids, new_status)
        n = result['updated']
        if n == 0:
            flash(f'No changes — selected leads were already “{new_status}”.', 'info')
        else:
            flash(f'Updated {n} lead{"s" if n != 1 else ""} to “{new_status}”.', 'success')
            # Stash undo payload — picked up by inject_undo() context processor.
            session['undo'] = {
                'ts':       int(time.time()),
                'previous': result['previous'],
                'desc':     f'Reverted {n} lead{"s" if n != 1 else ""} to previous status.',
            }
    except Exception as e:
        flash(f'Update failed: {e}', 'error')
    return redirect(request.referrer or url_for('leads'))


@app.route('/leads/undo', methods=['POST'])
def leads_undo():
    undo = session.pop('undo', None)
    if not undo:
        flash('Nothing to undo.', 'error')
        return redirect(request.referrer or url_for('leads'))
    if int(time.time()) - int(undo.get('ts', 0)) > UNDO_TTL_SECONDS:
        flash('Undo window has expired.', 'error')
        return redirect(request.referrer or url_for('leads'))
    try:
        previous = [(int(i), str(s)) for i, s in undo.get('previous', [])]
        n = db.bulk_revert_status(previous)
        flash(undo.get('desc') or f'Reverted {n} leads.', 'success')
    except Exception as e:
        flash(f'Undo failed: {e}', 'error')
    return redirect(request.referrer or url_for('leads'))


@app.context_processor
def inject_approvals_count():
    """Surface the pending-approvals count to the sidebar on every page.
    Cheap query (partial index on needs_approval=true); a single int."""
    try:
        return {'approvals_pending_count': db.approvals_total()}
    except Exception:
        # Never break the sidebar on a DB blip — just hide the badge.
        return {'approvals_pending_count': 0}


@app.context_processor
def inject_undo():
    """Surface the undo banner across every page until consumed or expired."""
    undo = session.get('undo')
    if not undo:
        return {}
    age = int(time.time()) - int(undo.get('ts', 0))
    if age > UNDO_TTL_SECONDS:
        session.pop('undo', None)
        return {}
    return {
        'undo_available':   True,
        'undo_description': undo.get('desc') or 'Undo last change',
        'undo_seconds_left': max(0, UNDO_TTL_SECONDS - age),
    }


@app.route('/leads/<int:lead_id>')
def lead_detail(lead_id: int):
    db_error = None
    lead = None
    timeline: list[dict] = []
    activity: list[dict] = []
    try:
        lead = db.get_lead(lead_id)
        if lead is None:
            abort(404)
        timeline = db.lead_timeline(lead_id)
        activity = db.lead_activity(lead_id, 30)
    except Exception as e:
        if hasattr(e, 'code') and e.code == 404:
            raise
        db_error = str(e).splitlines()[0][:240]
    return render_template(
        'lead_detail.html',
        active='leads',
        lead=lead,
        timeline=timeline,
        activity=activity,
        statuses=db.LEAD_STATUSES,
        db_error=db_error,
    )


@app.post('/leads/<int:lead_id>/status')
def update_lead_status_route(lead_id: int):
    new_status = (request.form.get('status') or '').strip()
    if new_status not in db.LEAD_STATUSES:
        return {'ok': False, 'error': 'Invalid status'}, 400
    try:
        db.update_lead_status(lead_id, new_status)
    except LookupError:
        return {'ok': False, 'error': 'Lead not found'}, 404
    except Exception as e:
        return {'ok': False, 'error': str(e)}, 500
    return {'ok': True, 'saved_at': int(time.time())}


@app.post('/leads/<int:lead_id>/notes')
def update_lead_notes_route(lead_id: int):
    notes = request.form.get('notes', '')
    if len(notes) > 5000:
        return {'ok': False, 'error': 'Notes too long (max 5000 chars)'}, 400
    try:
        db.update_lead_notes(lead_id, notes)
    except Exception as e:
        return {'ok': False, 'error': str(e)}, 500
    return {'ok': True, 'saved_at': int(time.time())}


@app.route('/outreach')
def outreach():
    """Back-compat alias — Outreach folded into Campaigns. Map the old
    tab names to the new ones so any bookmarks keep working."""
    old_tab = (request.args.get('tab') or 'today').lower()
    new_tab = {
        'today':     'sending',
        'sent':      'sent',
        'followups': 'sending',
        'bounces':   'bounces',
    }.get(old_tab, 'pending')
    qs = request.args.to_dict()
    qs['tab'] = new_tab
    return redirect(url_for('campaigns', **qs), code=301)


@app.post('/outreach/clients/<int:client_id>/pause')
def outreach_toggle_pause(client_id: int):
    paused = (request.form.get('paused') or '').lower() == 'true'
    try:
        result = db.set_client_outreach_paused(client_id, paused)
        if not result['changed']:
            flash(f"{result['name']} was already {'paused' if paused else 'sending'}.", 'info')
        else:
            verb = 'paused' if paused else 'resumed'
            flash(f"{result['name']} outreach {verb}.", 'success')
    except LookupError:
        flash('Client not found.', 'error')
    except Exception as e:
        flash(f'Could not update pause state: {e}', 'error')
    return redirect(request.referrer or url_for('outreach'))


@app.route('/inbox')
def inbox():
    """Unified Inbox — replies + form submissions awaiting operator action.
    Renamed from /inbound. Two tabs (needs_you / done). Drafts tab
    deferred until AI auto-reply backend exists (US-023); ?tab=drafts
    falls back to needs_you via the INBOX_TABS membership check below.

    Also surfaces the dashboard 'Today' KPI strip — Overview's role as
    the daily entry has been folded in here so the operator gets context
    + the queue on one screen."""
    db_error = None
    tab = (request.args.get('tab') or 'needs_you').lower()
    if tab not in db.INBOX_TABS:
        tab = 'needs_you'

    counts = {k: 0 for k in db.INBOX_TABS}
    items: list[dict] = []
    metrics = {
        'total_leads': 0, 'emails_week': 0,
        'reply_rate': 0.0, 'meetings_month': 0,
    }
    try:
        counts  = db.inbox_tab_counts()
        items   = db.inbox_items(tab=tab)
        metrics = db.dashboard_metrics()
    except Exception as e:
        db_error = str(e).splitlines()[0][:240]

    return render_template(
        'inbox.html',
        active='inbox',
        tab=tab,
        counts=counts,
        items=items,
        metrics=metrics,
        db_error=db_error,
    )


# /inbound → /inbox 301 redirect for legacy bookmarks (Epic 9).
@app.route('/inbound')
def inbound_legacy():
    return redirect(url_for('inbox', **request.args.to_dict()), code=301)


# Per-form drawer endpoints below stay on /inbound/<id> — those are
# stable internal IDs referenced from the form drawer JS, not browseable
# URLs. Renaming them would just churn API surface for no gain.
@app.get('/inbound/<int:inbound_id>.json')
def inbound_detail_json(inbound_id: int):
    """JSON used by the slide-out drawer on the inbound page."""
    row = db.inbound_get(inbound_id)
    if not row:
        return {'error': 'not_found'}, 404
    # ISO-format datetimes for JSON
    out = {**row}
    for k in ('created_at', 'auto_response_sent_at'):
        v = out.get(k)
        if v is not None:
            out[k] = v.isoformat()
    return out


@app.post('/inbound/<int:inbound_id>/status')
def inbound_set_status(inbound_id: int):
    new_status = (request.form.get('status') or '').strip().lower()
    try:
        result = db.update_inbound_status(inbound_id, new_status)
    except ValueError:
        return {'ok': False, 'error': 'invalid_status'}, 400
    except LookupError:
        return {'ok': False, 'error': 'not_found'}, 404
    except Exception as e:
        return {'ok': False, 'error': str(e)[:200]}, 500
    return {'ok': True, **result}


# Reply-thread + reply-send endpoints (US-024). The drawer fetches the
# full thread by reply id, then POSTs to send. SMTP wiring is deferred
# to the email-engine backend; this endpoint records the manual reply,
# advances lead status if asked, and clears the row from Needs you.
@app.get('/inbox/reply/<int:reply_id>.json')
def inbox_reply_thread(reply_id: int):
    try:
        data = db.reply_thread(reply_id)
    except Exception as e:
        return {'error': str(e)[:200]}, 500
    if not data:
        return {'error': 'not_found'}, 404
    return data


@app.post('/inbox/reply/<int:reply_id>/send')
def inbox_reply_send(reply_id: int):
    body    = (request.form.get('body') or '').strip()
    subject = (request.form.get('subject') or '').strip()
    action  = (request.form.get('action') or '').strip().lower() or None
    if not body:
        return {'ok': False, 'error': 'empty_body'}, 400
    if not subject:
        return {'ok': False, 'error': 'empty_subject'}, 400
    if action not in (None, 'meeting', 'lost'):
        return {'ok': False, 'error': 'invalid_action'}, 400
    try:
        result = db.reply_send(
            reply_id, body=body, subject=subject, status_action=action,
        )
    except LookupError:
        return {'ok': False, 'error': 'not_found'}, 404
    except Exception as e:
        return {'ok': False, 'error': str(e)[:200]}, 500
    return result


@app.route('/reports')
def reports():
    db_error = None
    period = (request.args.get('period') or '30d').lower()
    if period not in {p[0] for p in db.REPORTS_PERIODS}:
        period = '30d'
    days = db.reports_period_days(period)

    clients_min: list[dict] = []
    client: dict | None     = None
    kpis = {'leads': 0, 'leads_delta': 0, 'sent': 0, 'sent_delta': 0,
            'reply_rate': 0.0, 'reply_rate_delta': 0.0,
            'meetings': 0, 'meetings_delta': 0,
            'won': 0, 'won_delta': 0,
            'pipeline_value': 0, 'pipeline_value_delta': 0,
            'avg_deal_value': db.REPORTS_DEFAULT_AVG_DEAL_VALUE}
    chart   = {'labels': [], 'sent': [], 'replies': []}
    funnel: list[dict]       = []
    sequence: list[dict]     = []
    wins: list[dict]         = []
    hook_cohorts: list[dict] = []
    targets: dict        = db.reports_targets(days)
    narrative: str       = ''

    rollup: dict = {'clients': 0, 'meetings': 0, 'won': 0, 'pipeline_value': 0, 'days': days}
    try:
        clients_min = db.reports_clients_min()
        rollup      = db.reports_rollup(days)
        client_raw = request.args.get('client')
        client_id = int(client_raw) if (client_raw or '').isdigit() else None
        if client_id is None and clients_min:
            client_id = clients_min[0]['id']
        if client_id is not None:
            client       = db.reports_client_summary(client_id)
            kpis         = db.reports_kpis(client_id, days)
            chart        = db.reports_chart_series(client_id, days)
            funnel       = db.reports_funnel(client_id, days)
            sequence     = db.reports_sequence(client_id, days)
            wins         = db.reports_wins(client_id, days)
            hook_cohorts = db.reports_hook_cohorts(client_id, days)
            narrative    = db.reports_narrative(client_id, days, kpis)
    except Exception as e:
        db_error = str(e).splitlines()[0][:240]

    # Email-to-client mailto — recap text only, no private CRM URL
    # (clients can't open it). A real shareable public report URL is
    # a separate user story; until then the mailto is honest.
    mailto_url = ''
    if client and client.get('contact_email'):
        period_label = next((lbl for slug, lbl, _ in db.REPORTS_PERIODS
                             if slug == period), 'period')
        subj = f"Innovite — {client['name']} — {period_label} performance report"
        body_lines = [
            f"Hi {(client.get('contact_name') or '').split(' ')[0] or 'there'},",
            '',
            f"Quick recap for {period_label.lower()}:",
            '',
            narrative or '(report has no activity to summarise yet)',
            '',
            f"• Meetings booked: {kpis['meetings']}",
            f"• Deals won: {kpis['won']}",
            f"• Pipeline value: £{kpis['pipeline_value']:,}",
            f"• Reply rate: {kpis['reply_rate']}%",
            '',
            "Happy to walk through any of it on a call.",
            '',
            "— Sammy",
        ]
        from urllib.parse import quote
        mailto_url = (
            f"mailto:{client['contact_email']}"
            f"?subject={quote(subj)}&body={quote(chr(10).join(body_lines))}"
        )

    return render_template(
        'reports.html',
        active='reports',
        period=period,
        days=days,
        periods=db.REPORTS_PERIODS,
        clients_min=clients_min,
        client=client,
        kpis=kpis,
        chart=chart,
        funnel=funnel,
        sequence=sequence,
        wins=wins,
        targets=targets,
        rollup=rollup,
        hook_cohorts=hook_cohorts,
        mailto_url=mailto_url,
        db_error=db_error,
    )


@app.route('/reports.csv')
def reports_csv():
    period = (request.args.get('period') or '30d').lower()
    if period not in {p[0] for p in db.REPORTS_PERIODS}:
        period = '30d'
    days = db.reports_period_days(period)
    client_raw = request.args.get('client')
    client_id  = int(client_raw) if (client_raw or '').isdigit() else None
    if client_id is None:
        clients_min = db.reports_clients_min()
        client_id = clients_min[0]['id'] if clients_min else None
    if client_id is None:
        return Response('', status=204)

    rows = db.reports_csv_rows(client_id, days)
    buf  = io.StringIO()
    w    = csv.writer(buf)
    for r in rows:
        w.writerow(r)

    client = db.reports_client_summary(client_id) or {'name': 'unknown'}
    safe_name = ''.join(ch if ch.isalnum() or ch in '-_' else '_'
                        for ch in client['name']).strip('_').lower()
    fname = f'innovite-report-{safe_name}-{period}.csv'
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{fname}"'},
    )


@app.route('/settings')
def settings():
    db_error = None
    settings_data = {k: v for k, v in db.SETTINGS_DEFAULTS.items()}
    integrations: list[dict] = []
    status: dict = {}
    mb_summary: dict = {'total': 0, 'healthy': 0, 'warming': 0, 'needs_attention': 0}
    try:
        settings_data = db.settings_all()
        integrations  = db.integration_keys()
        status        = db.system_status()
        mb_summary    = db.mailboxes_summary()
    except Exception as e:
        db_error = str(e).splitlines()[0][:240]
    return render_template(
        'settings.html',
        active='settings',
        s=settings_data,
        integrations=integrations,
        status=status,
        mb_summary=mb_summary,
        db_error=db_error,
    )


def _truthy_form(name: str) -> bool:
    """Checkbox values arrive as 'on' or absent; coerce."""
    v = (request.form.get(name) or '').strip().lower()
    return v in ('on', '1', 'true', 'yes')


@app.post('/settings/profile')
def settings_save_profile():
    try:
        db.settings_set('profile', {
            'name':        (request.form.get('name')        or '').strip(),
            'email':       (request.form.get('email')       or '').strip(),
            'booking_url': (request.form.get('booking_url') or '').strip(),
            'signature':   (request.form.get('signature')   or '').strip(),
        })
        flash('Profile saved.', 'success')
    except Exception as e:
        flash(f'Could not save profile: {e}', 'error')
    return redirect(url_for('settings') + '#profile')


@app.post('/settings/email')
def settings_save_email():
    # Cap is per-mailbox now (see crm.mailboxes.daily_cap), so this form
    # only persists the global business-hours window.
    try:
        db.settings_set('sending_hours', {
            'start':         (request.form.get('hours_start') or '09:00').strip(),
            'end':           (request.form.get('hours_end')   or '17:00').strip(),
            'tz':            'Europe/London',
            'skip_weekends': _truthy_form('skip_weekends'),
        })
        flash('Sending hours saved.', 'success')
    except Exception as e:
        flash(f'Could not save sending hours: {e}', 'error')
    return redirect(url_for('settings') + '#hours')


@app.post('/settings/cadence')
def settings_save_cadence():
    try:
        db.settings_set('cadence', {
            'day1_enabled':  _truthy_form('day1_enabled'),
            'day3_enabled':  _truthy_form('day3_enabled'),
            'day7_enabled':  _truthy_form('day7_enabled'),
            'skip_weekends': _truthy_form('cadence_skip_weekends'),
        })
        flash('Cadence saved.', 'success')
    except Exception as e:
        flash(f'Could not save cadence: {e}', 'error')
    return redirect(url_for('settings') + '#cadence')


@app.post('/settings/pause-all')
def settings_pause_all():
    paused = (request.form.get('paused') or '').lower() == 'true'
    try:
        db.settings_set('system_outreach_paused', paused)
        verb = 'paused globally' if paused else 'resumed'
        flash(f'Outreach {verb}.', 'success')
    except Exception as e:
        flash(f'Could not update pause: {e}', 'error')
    return redirect(url_for('settings') + '#danger')


@app.route('/mailboxes')
def mailboxes():
    db_error = None
    rows: list[dict] = []
    domains: list[dict] = []
    summary: dict = {'total': 0, 'healthy': 0, 'warming': 0,
                     'needs_attention': 0, 'paused': 0,
                     'total_capacity': 0, 'total_sent_today': 0}
    try:
        rows    = db.mailboxes_all()
        domains = db.sending_domains_summary()
        summary = db.mailboxes_summary()
    except Exception as e:
        db_error = str(e).splitlines()[0][:240]
    return render_template(
        'mailboxes.html',
        active='mailboxes',
        mailboxes=rows,
        domains=domains,
        summary=summary,
        db_error=db_error,
    )


@app.route('/outreach/suppress', methods=['POST'])
def outreach_suppress():
    """Suppress an email address across all clients (US-007).

    Form fields:
      address  — email address to suppress (required)
      reason   — 'manual' / 'hard_bounce' / 'soft_bounce_x2' / 'unsubscribe' / 'complaint'
    """
    address = (request.form.get('address') or '').strip()
    reason = (request.form.get('reason') or 'manual').strip()
    if reason not in ('manual', 'hard_bounce', 'soft_bounce_x2', 'unsubscribe', 'complaint'):
        reason = 'manual'
    if not address or '@' not in address:
        flash('Need a valid address.', 'error')
        return redirect(url_for('outreach'))
    created = db.suppress_address(address, reason=reason, added_by='operator')
    flash(f'Suppressed {address}.' if created else f'{address} was already suppressed.',
          'success')
    return redirect(request.referrer or url_for('outreach', tab='bounces'))


# ── Campaigns (Batch A — Approvals + Outreach merged) ───────────────
# /campaigns is the unified outbound surface. Four tabs:
#   - pending  → Day-1 drafts awaiting approval (was /approvals)
#   - sending  → emails scheduled to ship today (was /outreach?tab=today)
#   - sent     → recently sent emails (was /outreach?tab=sent)
#   - bounces  → bounce log (was /outreach?tab=bounces)
# /approvals and /outreach 301 → /campaigns with the right tab.

_CAMPAIGN_TABS = ('pending', 'sending', 'sent', 'bounces')


@app.route('/campaigns')
def campaigns():
    db_error = None
    tab = (request.args.get('tab') or 'pending').lower()
    if tab not in _CAMPAIGN_TABS:
        tab = 'pending'

    client_raw = request.args.get('client')
    client_id  = int(client_raw) if (client_raw or '').isdigit() else None
    search     = (request.args.get('q') or '').strip() or None

    rows: list[dict] = []
    client_pills: list[dict] = []
    tab_counts: dict = {k: 0 for k in _CAMPAIGN_TABS}

    try:
        client_pills = db.approvals_count_per_client()
        tab_counts = {
            'pending':  db.approvals_total(),
            'sending':  len(db.outreach_today(client_id=None, search=None)),
            'sent':     0,  # filled below if tab=sent (heavy query)
            'bounces':  0,
        }
        if tab == 'pending':
            rows = db.approvals_pending(client_id=client_id, search=search)
        elif tab == 'sending':
            rows = db.outreach_today(client_id=client_id, search=search)
        elif tab == 'sent':
            rows = db.outreach_sent(client_id=client_id, search=search)
            tab_counts['sent'] = len(rows)
        else:  # bounces
            rows = db.outreach_bounces(client_id=client_id, search=search)
            tab_counts['bounces'] = len(rows)
    except Exception as e:
        db_error = str(e).splitlines()[0][:240]

    active_client_name = None
    if client_id:
        for p in client_pills:
            if p['id'] == client_id:
                active_client_name = p['name']
                break

    return render_template(
        'campaigns.html',
        active='campaigns',
        tab=tab,
        rows=rows,
        client_pills=client_pills,
        tab_counts=tab_counts,
        active_client_id=client_id,
        active_client_name=active_client_name,
        search=search or '',
        db_error=db_error,
    )


# /approvals stays as a back-compat alias. 301 so search engines /
# bookmarks update. Preserves the ?client= filter.
@app.route('/approvals')
def approvals():
    qs = request.args.to_dict()
    qs.setdefault('tab', 'pending')
    return redirect(url_for('campaigns', **qs), code=301)


@app.post('/approvals/approve')
def approvals_approve():
    """Bulk approve N drafts. Form field `ids` is a list of email ids."""
    ids_raw = request.form.getlist('ids')
    ids = [int(x) for x in ids_raw if x.isdigit()]
    if not ids:
        flash('Pick at least one draft to approve.', 'error')
        return redirect(request.referrer or url_for('approvals'))
    try:
        result = db.approvals_bulk_approve(ids, operator='operator')
        n = result['approved']
        if n == 0:
            flash('No drafts approved — they may have been handled already.', 'info')
        else:
            flash(
                f'Approved {n} draft{"s" if n != 1 else ""}. '
                f'The outreach engine will pick {"them" if n != 1 else "it"} up on the next tick.',
                'success',
            )
    except Exception as e:
        flash(f'Approve failed: {e}', 'error')
    return redirect(request.referrer or url_for('approvals'))


@app.post('/approvals/<int:email_id>/skip')
def approvals_skip(email_id: int):
    """Cancel a single queued draft without sending."""
    try:
        db.approvals_skip(email_id, operator='operator')
        return {'ok': True}
    except LookupError:
        return {'ok': False, 'error': 'not_found'}, 404
    except Exception as e:
        return {'ok': False, 'error': str(e)[:200]}, 500


@app.post('/approvals/<int:email_id>/save-template')
def approvals_save_template(email_id: int):
    """Promote an approved draft to a saved campaign template. The next
    Day-N draft for the same campaign+step will seed Claude with this
    template's structure."""
    set_default = (request.form.get('default') or 'true').lower() in ('true', '1', 'yes')
    try:
        result = db.save_email_as_template(
            email_id, set_as_default=set_default, operator='operator',
        )
        return {'ok': True, **result}
    except LookupError:
        return {'ok': False, 'error': 'not_found'}, 404
    except ValueError as e:
        return {'ok': False, 'error': str(e)}, 400
    except Exception as e:
        return {'ok': False, 'error': str(e)[:200]}, 500


@app.post('/approvals/<int:email_id>/edit-approve')
def approvals_edit_approve(email_id: int):
    """Update subject + body, approve in one transaction."""
    subject = (request.form.get('subject') or '').strip()
    body    = (request.form.get('body') or '').strip()
    try:
        db.approvals_edit_and_approve(email_id, subject, body, operator='operator')
        return {'ok': True}
    except ValueError as e:
        return {'ok': False, 'error': str(e)}, 400
    except LookupError:
        return {'ok': False, 'error': 'not_found'}, 404
    except Exception as e:
        return {'ok': False, 'error': str(e)[:200]}, 500


# ── Pipeline runner (US-001 — Find new leads) ────────────────────────
# Two endpoints:
#   POST /api/pipeline/run         — enqueue a run for one or more clients
#   GET  /api/pipeline/run/<id>    — poll for status / progress
# The actual work happens in crm-worker.service (pipeline.runner.run).
# This route is intentionally thin: write a row, return the id, get out.


@app.route('/api/pipeline/run', methods=['POST'])
def pipeline_run_create():
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get('client_ids') or ([payload['client_id']] if payload.get('client_id') else [])
    try:
        client_ids = [int(x) for x in raw_ids]
    except (TypeError, ValueError):
        return jsonify({'error': 'client_ids must be integers'}), 400
    if not client_ids:
        return jsonify({'error': 'client_ids required'}), 400

    mode = payload.get('mode') or 'manual'
    if mode not in ('manual', 'scheduled', 'onboarding'):
        return jsonify({'error': 'invalid mode'}), 400

    runs = []
    for cid in client_ids:
        row = db.fetch_one(
            """insert into crm.pipeline_runs (client_id, status, mode, triggered_by)
               values (%s, 'pending', %s, %s)
               returning id, client_id, status""",
            (cid, mode, 'operator'),
        )
        if row:
            runs.append(row)
    return jsonify({'runs': runs}), 202


@app.route('/api/pipeline/run/<int:run_id>')
def pipeline_run_status(run_id: int):
    row = db.fetch_one(
        """select id, client_id, status, mode, leads_added, leads_skipped,
                  leads_errored, progress, error_msg, started_at, finished_at, created_at
           from crm.pipeline_runs where id = %s""",
        (run_id,),
    )
    if not row:
        return jsonify({'error': 'not found'}), 404
    # Datetimes → ISO so the JS poll doesn't need a date parser.
    for k in ('started_at', 'finished_at', 'created_at'):
        if row.get(k) is not None:
            row[k] = row[k].isoformat()
    return jsonify(row)


@app.get('/api/search')
def api_search():
    """JSON-only endpoint that powers the ⌘K command palette.

    Returns {'clients': [...], 'leads': [...]} — capped at 8 each so
    the palette stays scannable. Empty query → empty result set
    (the palette's empty state advertises shortcuts in that case)."""
    q = (request.args.get('q') or '').strip()
    try:
        return jsonify(db.quick_search(q))
    except Exception as e:
        return jsonify({'error': str(e)[:200]}), 500


# ── Search-first lead discovery (US — agent-driven brief) ────────────
# A single page where the operator types industry + location, gets
# Google Maps results in seconds, then ticks the ones they want and
# assigns them to a real client. No wizard, no pre-configured client
# profile. Full enrichment runs in the background after assignment so
# the search UI feels instant.

@app.route('/search')
def search_page():
    """The Search → Results → Assign page."""
    try:
        clients = db.list_clients()
    except Exception:
        clients = []
    return render_template('search.html', active='search', clients=clients)


@app.post('/api/search/discover')
def api_search_discover():
    """Run a free Google Places discovery against the given (industries,
    locations) combo. Returns up to ~60 results across pairs. Synchronous
    — typically <5s for 20 results since we hit the free Maps endpoint
    directly with no per-call sleep.

    Body:
      {industries: ["accountants", ...], locations: ["Cambridge", ...],
       limit_per_pair: 20}
    Returns:
      {results: [{business_name, address, city, postcode, website,
                   google_rating, google_place_id, google_maps_url}],
       count, errors}
    """
    from pipeline.sources import google_places

    payload = request.get_json(silent=True) or {}
    industries = [s.strip() for s in (payload.get('industries') or []) if s and s.strip()]
    locations  = [s.strip() for s in (payload.get('locations')  or []) if s and s.strip()]
    try:
        limit_per_pair = int(payload.get('limit_per_pair') or 20)
    except (TypeError, ValueError):
        limit_per_pair = 20
    limit_per_pair = max(1, min(limit_per_pair, 40))

    if not industries or not locations:
        return jsonify({'error': 'industries and locations required'}), 400

    out: list[dict] = []
    errors: list[str] = []
    seen_place_ids: set[str] = set()
    for ind in industries:
        for loc in locations:
            try:
                rows = google_places.discover(ind, loc, limit=limit_per_pair)
            except Exception as e:
                errors.append(f'{ind}/{loc}: {str(e)[:120]}')
                continue
            for r in rows:
                if r.get('source_errors', {}).get('google_places'):
                    errors.append(f"{ind}/{loc}: {r['source_errors']['google_places']}")
                    continue
                pid = r.get('google_place_id')
                if pid and pid in seen_place_ids:
                    continue
                if pid:
                    seen_place_ids.add(pid)
                # Annotate which (industry, location) pair the result came from
                # so the UI can render a tag.
                r['matched_industry'] = ind
                r['matched_location'] = loc
                out.append(r)

    # Cross-client already-assigned lookup. Single query joins all leads
    # across all clients so the UI can flag rows the operator has
    # already pitched (under any client). Name-based match is good
    # enough for the UI hint; the assign endpoint dedupes more strictly
    # per-client.
    if out:
        names_lower = list({(r['business_name'] or '').strip().lower()
                            for r in out if r.get('business_name')})
        if names_lower:
            assigned_rows = db.fetch_all(
                """select lower(l.business_name) as name_lower, c.name as client_name
                   from crm.leads l
                   join crm.clients c on c.id = l.client_id
                   where lower(l.business_name) = any(%s)""",
                (names_lower,),
            )
            assigned_map = {r['name_lower']: r['client_name'] for r in assigned_rows}
            for r in out:
                name_lower = (r.get('business_name') or '').strip().lower()
                if name_lower in assigned_map:
                    r['already_assigned'] = True
                    r['assigned_to_client'] = assigned_map[name_lower]

    return jsonify({'count': len(out), 'results': out, 'errors': errors})


@app.post('/api/search/discover-media')
def api_search_discover_media():
    """Enqueue a Media-mode (Vidora) search.

    Synchronously writes a crm.searches row and returns its id. The
    worker picks it up via _claim_next_search, dispatches to
    MediaMode.run, which then handles Google Places discovery +
    instagram_link + instagram_snapshot + vidora_audit + upsert per
    candidate. Async because each candidate can take 30-60s.

    Body:
      {industry: "aesthetic clinics", location: "Manchester", limit: 20}
    Returns:
      {search_id: int, status: "pending"}
    """
    payload = request.get_json(silent=True) or {}
    industry = (payload.get('industry') or '').strip()
    location = (payload.get('location') or '').strip()
    try:
        limit = int(payload.get('limit') or 20)
    except (TypeError, ValueError):
        limit = 20
    # Media discovery is async and per-candidate cost is real (one IG
    # snapshot + one Sonnet vision audit per survivor). Cap at 500 to
    # match the UI slider's ceiling. Beyond that, split into multiple
    # searches so the operator can pause/resume between batches.
    limit = max(1, min(limit, 500))

    if not industry or not location:
        return jsonify({'error': 'industry and location required'}), 400

    # Vidora Media is the canonical media-mode client (seeded by 0034).
    # Resolve by client_type so a rename in the UI doesn't strand the
    # endpoint.
    client = db.fetch_one(
        "select id from crm.clients where client_type = 'media' "
        "order by id limit 1"
    )
    if not client:
        return jsonify({'error': "no media-mode client configured "
                                 "(check crm.clients.client_type='media')"}), 400

    row = db.fetch_one(
        """insert into crm.searches (client_id, mode, params, status)
           values (%s, 'media', %s, 'pending')
           returning id""",
        (client['id'], json.dumps({
            'industry': industry,
            'location': location,
            'limit':    limit,
        })),
    )
    return jsonify({'search_id': row['id'], 'status': 'pending'}), 201


@app.post('/api/search/enrich-assign')
def api_search_enrich_assign():
    """Move selected search results into crm.leads under a chosen
    client, then enqueue a pipeline_run with mode='enrich_assigned'
    so the worker fills in the full enrichment (CH + DNS + Apollo +
    LinkedIn + website + jobs + Gazette + scoring + drafted Day-1
    email) in the background.

    Body:
      {client_id: int, results: [{google_place_id, business_name, ...}]}
    Returns:
      {inserted: N, skipped_dupes: N, lead_ids: [...], run_id: int|null}

    run_id is null only when no leads were actually inserted (everything
    was a dupe). UI polls /api/pipeline/run/<run_id> for progress.
    """
    payload = request.get_json(silent=True) or {}
    try:
        client_id = int(payload.get('client_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'client_id (int) required'}), 400
    results = payload.get('results') or []
    if not isinstance(results, list) or not results:
        return jsonify({'error': 'results array required'}), 400

    # Existing-lead dedupe by business_name (lower) per client.
    existing = {
        (r.get('business_name') or '').strip().lower()
        for r in db.fetch_all(
            'select business_name from crm.leads where client_id = %s',
            (client_id,),
        )
    }

    lead_ids: list[int] = []
    skipped = 0
    for r in results:
        name = (r.get('business_name') or '').strip()
        if not name:
            skipped += 1
            continue
        if name.lower() in existing:
            skipped += 1
            continue
        # source='outbound' is enforced by a CHECK constraint that only
        # accepts ('outbound','inbound','referral'). Search-discovered
        # leads are outbound (we found them and chose to contact them).
        row = db.fetch_one(
            """insert into crm.leads
                 (client_id, business_name, address, city, website,
                  google_rating, google_maps_url,
                  status, source)
               values (%s, %s, %s, %s, %s, %s, %s, 'new', 'outbound')
               returning id""",
            (
                client_id,
                name[:512],
                (r.get('address') or '')[:1024] or None,
                (r.get('city') or '') or None,
                (r.get('website') or '') or None,
                float(r['google_rating']) if r.get('google_rating') is not None else None,
                (r.get('google_maps_url') or '') or None,
            ),
        )
        if row:
            lead_ids.append(row['id'])
            existing.add(name.lower())

    run_id = None
    if lead_ids:
        try:
            row = db.fetch_one(
                """insert into crm.pipeline_runs
                     (client_id, status, mode, triggered_by, progress)
                   values (%s, 'pending', 'enrich_assigned', 'operator', %s)
                   returning id""",
                (client_id, json.dumps({'phase': 'queued', 'lead_ids': lead_ids})),
            )
            if row:
                run_id = row['id']
        except Exception as e:
            # Lead rows already inserted — surface the enqueue failure
            # but don't roll back the inserts; the operator can manually
            # trigger enrichment from the lead list if needed.
            app.logger.exception('enqueue enrich_assigned run failed')
            return jsonify({
                'inserted': len(lead_ids),
                'skipped_dupes': skipped,
                'lead_ids': lead_ids,
                'run_id': None,
                'enqueue_error': str(e)[:200],
            }), 201

    return jsonify({
        'inserted': len(lead_ids),
        'skipped_dupes': skipped,
        'lead_ids': lead_ids,
        'run_id': run_id,
    }), 201


# ── List-Unsubscribe (RFC 8058 one-click) ───────────────────────────
# Public endpoint — bound from the List-Unsubscribe + List-Unsubscribe-Post
# headers the outreach engine injects on every send. Gmail/Yahoo fire a
# POST with body 'List-Unsubscribe=One-Click' when the user clicks the
# native Unsubscribe button; we silently 204 back and add the address
# to the suppression list. GET shows a human confirmation page (for the
# rare client that falls back to a link-click flow, or if the recipient
# pastes the URL into a browser themselves).
#
# Token is an itsdangerous-signed payload of {e: email_id, a: address}
# so we don't need a per-email unsubscribe-token table. Cost: if
# FLASK_SECRET_KEY rotates, outstanding links go invalid — fine,
# operators can suppress manually if anyone complains.


@app.route('/unsubscribe', methods=['GET', 'POST'])
def unsubscribe():
    token = request.args.get('t') or request.form.get('t')
    if not token:
        return Response('Missing token.', status=400, mimetype='text/plain')

    ser = URLSafeSerializer(app.config['SECRET_KEY'], salt='unsubscribe-v1')
    try:
        data = ser.loads(token)
    except BadSignature:
        return Response('Invalid or expired token.', status=403, mimetype='text/plain')

    address = (data.get('a') or '').strip()
    email_id = data.get('e')
    if not address or '@' not in address:
        return Response('Invalid token payload.', status=403, mimetype='text/plain')

    try:
        db.suppress_address(
            address,
            reason='unsubscribe',
            added_by='one_click_header',
            detail=f'list-unsubscribe one-click (email {email_id})',
        )
    except Exception:
        # Never surface failure to the recipient — one-click MUST appear
        # to succeed. The operator can audit the suppression log.
        pass

    if request.method == 'POST':
        # RFC 8058: respond 200/204 with no body required.
        return Response('', status=204)
    return render_template('unsubscribe.html', address=address)


@app.route('/healthz')
def healthz():
    return {'ok': True}


@app.route('/favicon.ico')
def favicon():
    # Browsers request /favicon.ico regardless of <link rel="icon">; return
    # 204 to silence the noise (the real favicon is set via the data-URI
    # link in base.html).
    return '', 204


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '8080')), debug=True)
