"""Innovite CRM — Flask web UI.

POC stage: no auth (Railway URL stays unguessable).
TODO: add single-password session gate before this is publicly linked.
"""
import os
from flask import Flask, render_template

import db

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'dev-only-change-in-prod')


@app.route('/')
def overview():
    metrics = db.dashboard_metrics()
    chart_labels, chart_values = db.leads_per_day(7)
    activity = db.recent_activity(20)
    return render_template(
        'overview.html',
        active='overview',
        metrics=metrics,
        chart_labels=chart_labels,
        chart_values=chart_values,
        activity=activity,
    )


@app.route('/clients')
def clients():
    return render_template('clients.html', active='clients')


@app.route('/clients/<int:client_id>')
def client_detail(client_id: int):
    return render_template('client_detail.html', active='clients', client_id=client_id)


@app.route('/leads')
def leads():
    return render_template('leads.html', active='leads')


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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '8080')), debug=True)
