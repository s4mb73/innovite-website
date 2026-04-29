"""Innovite CRM — Flask web UI.

POC stage: no auth (Railway URL stays unguessable).
TODO: add single-password session gate before this is publicly linked.
"""
import csv
import io
import os
from flask import Flask, Response, abort, flash, redirect, render_template, request, url_for

import db

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
        n = db.bulk_change_status(ids, new_status)
        flash(f'Updated {n} lead{"s" if n != 1 else ""} to “{new_status}”.', 'success')
    except Exception as e:
        flash(f'Update failed: {e}', 'error')
    return redirect(request.referrer or url_for('leads'))


@app.route('/leads/<int:lead_id>')
def lead_detail(lead_id: int):
    return render_template('lead_detail.html', active='leads', lead_id=lead_id)


@app.route('/outreach')
def outreach():
    return render_template('outreach.html', active='outreach')


@app.route('/inbound')
def inbound():
    return render_template('inbound.html', active='inbound')


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
