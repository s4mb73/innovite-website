"""Innovite CRM — Flask web UI.

POC stage: no auth (Railway URL stays unguessable).
TODO: add single-password session gate before this is publicly linked.
"""
import csv
import io
import os
import time
from flask import Flask, Response, abort, flash, redirect, render_template, request, session, url_for

import db

UNDO_TTL_SECONDS = 60

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'dev-only-change-in-prod')


@app.route('/')
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
    try:
        metrics = db.dashboard_metrics()
        chart_labels, chart_values = db.leads_per_day(7)
        activity = db.recent_activity(20)
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
    try:
        client = db.get_client(client_id)
        if client is None:
            abort(404)
        stats = db.client_stats(client_id)
        leads = db.client_recent_leads(client_id, 10)
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
    sort = args.get('sort') or 'recent'
    if sort not in db.SORT_SQL:
        sort = 'recent'
    page = int(args.get('page') or 1)
    return {'status': status, 'client_id': client_id, 'search': search, 'sort': sort, 'page': page}


@app.route('/leads')
def leads():
    db_error = None
    result = {'rows': [], 'total': 0, 'counts': {s: 0 for s in db.LEAD_STATUSES} | {'all': 0},
              'page': 1, 'pages': 1, 'page_size': 50, 'page_start': 0, 'page_end': 0}
    clients_min: list[dict] = []
    f = _lead_filters_from_request()
    try:
        result = db.leads_search(**f, page_size=50)
        clients_min = db.all_clients_min()
    except Exception as e:
        db_error = str(e).splitlines()[0][:240]
    return render_template(
        'leads.html',
        active='leads',
        result=result,
        clients_min=clients_min,
        f=f,
        active_status=request.args.get('status', 'all'),
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
    db_error = None
    tab = (request.args.get('tab') or 'today').lower()
    if tab not in db.OUTREACH_TABS:
        tab = 'today'

    client_raw = request.args.get('client')
    client_id  = int(client_raw) if (client_raw or '').isdigit() else None
    search     = (request.args.get('q') or '').strip() or None

    kpis = {'pending_today': 0, 'sent_7d': 0, 'reply_rate': 0.0, 'bounce_rate': 0.0}
    counts = {k: 0 for k in db.OUTREACH_TABS}
    clients_panel: list[dict] = []
    clients_min: list[dict] = []
    rows: list[dict] = []

    try:
        kpis          = db.outreach_kpis()
        counts        = db.outreach_tab_counts(client_id=client_id)
        clients_panel = db.outreach_clients_panel()
        clients_min   = db.all_clients_min()
        if   tab == 'today':     rows = db.outreach_today(client_id, search)
        elif tab == 'sent':      rows = db.outreach_sent(client_id, search)
        elif tab == 'followups': rows = db.outreach_followups(client_id, search)
        else:                    rows = db.outreach_bounces(client_id, search)
    except Exception as e:
        db_error = str(e).splitlines()[0][:240]

    return render_template(
        'outreach.html',
        active='outreach',
        tab=tab,
        kpis=kpis,
        counts=counts,
        clients_panel=clients_panel,
        clients_min=clients_min,
        rows=rows,
        f={'client_id': client_id, 'search': search},
        db_error=db_error,
    )


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


@app.route('/inbound')
def inbound():
    db_error = None
    tab    = (request.args.get('tab')   or 'all').lower()
    if tab not in db.INBOUND_TABS:
        tab = 'all'
    score  = (request.args.get('score') or '').lower() or None
    if score not in db.INBOUND_SCORES:
        score = None
    search = (request.args.get('q') or '').strip() or None

    kpis = {'new_7d': 0, 'new_7d_delta': 0, 'hot_count': 0, 'hot_pct': 0,
            'avg_response': '—', 'avg_response_s': 0, 'book_rate': 0.0}
    counts = {k: 0 for k in db.INBOUND_TABS}
    needs_response: list[dict] = []
    rows: list[dict] = []

    try:
        kpis           = db.inbound_kpis()
        counts         = db.inbound_tab_counts()
        needs_response = db.inbound_needs_response()
        rows           = db.inbound_list(tab=tab, score=score, search=search)
    except Exception as e:
        db_error = str(e).splitlines()[0][:240]

    return render_template(
        'inbound.html',
        active='inbound',
        tab=tab,
        kpis=kpis,
        counts=counts,
        needs_response=needs_response,
        rows=rows,
        f={'score': score, 'search': search},
        db_error=db_error,
    )


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


@app.route('/reports')
def reports():
    return render_template('reports.html', active='reports')


@app.route('/settings')
def settings():
    return render_template('settings.html', active='settings')


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
