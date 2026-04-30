"""QA harness for Innovite CRM. Run from repo root."""
from __future__ import annotations
import ast
import os
import re
import sys
import traceback
from pathlib import Path

CRM = Path(__file__).resolve().parent.parent
results: list[tuple[str, str, str]] = []   # (suite, test, status_or_msg)


def add(suite: str, test: str, msg: str) -> None:
    results.append((suite, test, msg))


# ── 1. Python syntax + AST parse ────────────────────────────────────
for f in ['app.py', 'db.py', 'models.py']:
    path = CRM / f
    try:
        ast.parse(path.read_text())
        add('python-syntax', f, '✅ parses')
    except SyntaxError as e:
        add('python-syntax', f, f'❌ {e.msg} at line {e.lineno}')


# ── 2. pyflakes — undefined names, unused imports ───────────────────
from pyflakes.api import checkPath
import io
import contextlib
for f in ['app.py', 'db.py', 'models.py']:
    path = CRM / f
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(buf):
        n = checkPath(str(path))
    out = buf.getvalue().strip()
    if n == 0 and not out:
        add('pyflakes', f, '✅ clean')
    else:
        for line in (out.splitlines() or [f'{n} issues']):
            add('pyflakes', f, f'⚠ {line}')


# ── 3. ruff — broader linting ───────────────────────────────────────
import subprocess
r = subprocess.run(['ruff', 'check', str(CRM), '--select', 'F,B', '--quiet'],
                   capture_output=True, text=True)
if r.returncode == 0 and not r.stdout.strip():
    add('ruff', 'all .py files', '✅ clean')
else:
    for line in (r.stdout.strip().splitlines() or [r.stderr.strip()]):
        if line:
            add('ruff', 'lint', f'⚠ {line}')


# ── 4. Jinja template compile (every .html) ─────────────────────────
from jinja2 import Environment, FileSystemLoader, TemplateSyntaxError
env = Environment(loader=FileSystemLoader(str(CRM / 'templates')))
for tpl in sorted((CRM / 'templates').glob('*.html')):
    try:
        env.get_template(tpl.name)
        add('jinja-syntax', tpl.name, '✅ compiles')
    except TemplateSyntaxError as e:
        add('jinja-syntax', tpl.name, f'❌ line {e.lineno}: {e.message}')
    except Exception as e:
        add('jinja-syntax', tpl.name, f'❌ {e}')


# ── 5. SQL syntax check on every literal in db.py ───────────────────
import sqlparse
db_src = (CRM / 'db.py').read_text()
sql_blocks = re.findall(r'(?s)"""\s*(select|insert|update|delete|create|alter)\b.*?"""',
                        db_src, re.IGNORECASE)
parsed = 0
for sql in sql_blocks:
    parsed_obj = sqlparse.parse(sql)
    if parsed_obj:
        parsed += 1
add('sql-parse', f'{parsed} statements', '✅ parses' if parsed == len(sql_blocks) else f'⚠ {parsed}/{len(sql_blocks)}')


# ── 6. Migration SQL files ──────────────────────────────────────────
for sf in sorted((CRM / 'migrations').glob('*.sql')):
    txt = sf.read_text()
    parsed_obj = sqlparse.parse(txt)
    add('sql-parse', sf.name, f'✅ {len(parsed_obj)} stmts parse' if parsed_obj else '❌ empty')


# ── 7. Flask app boot + route registration ──────────────────────────
sys.path.insert(0, str(CRM))
os.environ.setdefault('DATABASE_URL', 'postgresql://test:test@localhost:5432/test')
os.environ.setdefault('FLASK_SECRET_KEY', 'test-key')
try:
    import importlib
    import app as crm_app  # noqa
    importlib.reload(crm_app)
    add('flask-boot', 'import + create_app', '✅ ok')
    rules = [r for r in crm_app.app.url_map.iter_rules() if r.endpoint != 'static']
    add('flask-routes', 'count', f'{len(rules)} registered')
    expected = {
        'overview', 'clients', 'client_detail', 'leads', 'lead_detail',
        'outreach', 'inbox', 'reports', 'settings', 'healthz',
        'favicon', 'leads_csv', 'leads_bulk_status', 'leads_undo',
        'update_lead_status_route', 'update_lead_notes_route',
        'outreach_toggle_pause',
    }
    actual = {r.endpoint for r in rules}
    missing = expected - actual
    extra = actual - expected
    if not missing:
        add('flask-routes', 'expected endpoints', f'✅ all {len(expected)} present')
    else:
        add('flask-routes', 'missing', f'❌ {sorted(missing)}')
    if extra:
        add('flask-routes', 'extra', f'ℹ {sorted(extra)}')
except Exception as e:
    add('flask-boot', 'failure', f'❌ {type(e).__name__}: {e}')
    traceback.print_exc()


# ── 8. Render templates with mock data (catches missing variables) ──
import datetime as dt
def now(): return dt.datetime(2026, 4, 29, 12, 0, tzinfo=dt.timezone.utc)
mock_metrics = {'total_leads': 10, 'total_leads_delta': 1, 'emails_week': 5,
                'emails_week_delta': 0, 'reply_rate': 12.4, 'reply_rate_delta': 0.5,
                'meetings_month': 3, 'meetings_month_delta': -1}
mock_client = {'id': 1, 'name': 'ROCA', 'industry': 'Pro services',
               'contact_name': 'Aidan', 'contact_email': 'a@b.com',
               'monthly_fee': 2500, 'pricing_tier': 'tier 3',
               'status': 'active', 'onboarded_at': now(), 'created_at': now(),
               'since': 'Jan 2026',
               # ↓ fields list_clients() decorates each row with
               'leads_7d': 5, 'leads_7d_delta': 2,
               'reply_rate': 12.4, 'reply_rate_delta': 1.8,
               # ↓ operational state added in US-019
               'target_industries': ['Construction', 'Property dev'],
               'target_locations':  ['Manchester', 'Leeds'],
               'outreach_paused': False,
               'pending_today': 3,
               'last_lead_added': now(),
               'targeting_empty': False,
               'locations_preview': ['Manchester', 'Leeds'],
               'locations_extra': 0,
               'last_find_relative': '14h ago',
               'last_find_days': 0,
               'op_state': 'healthy'}
mock_lead = {**{c: None for c in [
    'id','business_name','address','city','phone','email','website',
    'google_rating','google_review_count','google_maps_url',
    'instagram_handle','instagram_followers','instagram_engagement_rate',
    'instagram_posts_per_week','instagram_avg_likes','instagram_last_post_date',
    'website_score','website_ssl','website_cta','website_load_time_ms',
    'companies_house_number','companies_house_revenue_band',
    'companies_house_sic_code','companies_house_incorporated',
    'linkedin_url','decision_maker_name','decision_maker_title',
    'competitor_1_name','competitor_1_reviews','competitor_1_score',
    'competitor_2_name','competitor_2_reviews','competitor_2_score',
    'competitor_3_name','competitor_3_reviews','competitor_3_score',
    'competitor_rank','grade','overall_score','hook_type',
    'weakness_profile','revenue_gap_estimate',
    'email_subject','email_body_day1','email_body_day3','email_body_day7',
    'pdf_path','source','notes','client_name'
]}, 'id': 1, 'business_name': 'Apex Financial', 'grade_colour':'green',
   'status_colour':'green', 'status':'replied', 'created_at': now(), 'updated_at': now(),
   'client_id': 1, 'client_name':'ROCA',
   # ↓ triage fields added in US-021
   'stage_time_label': '6h', 'op_state': 'replied', 'relative': '6h ago'}

# A second mock with a different op_state so the rendered table exercises
# at least two of the lead-op-* class branches.
mock_lead_meeting = {**mock_lead,
                     'id': 2, 'business_name': 'Greenfield Property',
                     'status': 'meeting', 'op_state': 'meeting',
                     'stage_time_label': '2d', 'grade': 'A',
                     'grade_colour': 'green', 'status_colour': 'green',
                     'city': 'Leeds', 'decision_maker_name': 'Sarah Cole',
                     'decision_maker_title': 'MD'}

mock_result = {'rows': [mock_lead, mock_lead_meeting], 'total': 2,
               'counts': {'all':2,'new':0,'contacted':0,'replied':1,
                          'meeting':1,'won':0,'lost':0},
               'page':1,'pages':1,'page_size':50,'page_start':1,'page_end':2}

mock_finder_client = {
    'id': 1, 'name': 'ROCA Accountants',
    'targeting_empty': False,
    'last_find_relative': '14h ago',
    'last_lead_count_label': '12 found · 7d',
}

renders = [
    ('overview.html',     {'active':'overview','metrics':mock_metrics,
                            'chart_labels':[],'chart_values':[],'activity':[],
                            'finder_clients':[mock_finder_client],
                            'db_error':None}),
    ('clients.html',      {'active':'clients','clients':[mock_client],
                            'db_error':None}),
    ('client_detail.html',{'active':'clients','client':mock_client,
                            'stats':{'total_leads':10,'total_leads_delta':1,
                                     'reply_rate':12.4,'reply_rate_delta':0.5,
                                     'meetings_month':3,'meetings_month_delta':-1},
                            'leads':[],'db_error':None}),
    ('client_new.html',   {'active':'clients',
                            'employee_bands':('1-10','11-50','51-200','200+'),
                            'form':{'active_filing_only': True},
                            'errors':{}}),
    ('leads.html',        {'active':'leads','result':mock_result,
                            'clients_min':[{'id':1,'name':'ROCA'}],
                            'client_tabs':[{'id':1,'name':'ROCA','count':2}],
                            'active_client':{'id':1,'name':'ROCA'},
                            'f':{'status':None,'client_id':1,'search':None,
                                 'sort':'triage','page':1},
                            'active_status':'all','db_error':None}),
    ('lead_detail.html',  {'active':'leads','lead':mock_lead,'timeline':[],
                            'activity':[],'statuses':['new','contacted','replied',
                                'meeting','won','lost'],'db_error':None}),
    ('outreach.html',     {'active':'outreach','tab':'today',
                            'kpis':{'pending_today':0,'sent_7d':0,
                                    'reply_rate':0.0,'bounce_rate':0.0},
                            'counts':{'today':0,'sent':0,'followups':0,'bounces':0},
                            'clients_panel':[],'clients_min':[{'id':1,'name':'ROCA'}],
                            'finder_clients':[mock_finder_client],
                            'rows':[],'f':{'client_id':None,'search':None},
                            'db_error':None}),
    ('inbox.html',        {'active':'inbox','tab':'needs_you',
                            'counts':{'needs_you':2,'drafts':0,'done':5},
                            'items':[
                                {'kind':'reply','id':'r:1','href':'/leads/1',
                                 'display_name':'James Whitfield','company':'Pinnacle Construction',
                                 'subject':'Re: A quick thought on Pinnacle Construction',
                                 'snippet':'Could you send a bit more info on what you had in mind?',
                                 'received_at': now(),'relative':'4h ago',
                                 'signal_label':'Neutral','signal_class':'neutral',
                                 'client_name':'ROCA Accountants','lead_status':'replied'},
                                {'kind':'form','id':'f:1','href':'/inbox#form-1',
                                 'display_name':'Hannah Patel','company':'Patel Property',
                                 'subject':'Submitted via innoviteai.com',
                                 'snippet':'Currently doing outbound manually — looking for a system.',
                                 'received_at': now(),'relative':'2h ago',
                                 'signal_label':'Hot','signal_class':'hot',
                                 'client_name':'','inbound_id':1},
                            ],
                            'db_error':None}),
]

if 'crm_app' in dir():
    with crm_app.app.test_request_context():
        for name, ctx in renders:
            try:
                tpl = env.get_template(name)
                # Prevent the template from breaking on get_flashed_messages
                tpl.globals['get_flashed_messages'] = lambda **k: []
                tpl.globals['url_for'] = lambda ep, **kw: f'/{ep}'
                class _Args(dict):
                    def to_dict(self): return dict(self)
                tpl.globals['request'] = type('R', (), {'args': _Args()})()
                output = tpl.render(**ctx)
                add('template-render', name, f'✅ {len(output):,} chars')
            except Exception as e:
                add('template-render', name, f'❌ {type(e).__name__}: {e}')


# ── 9. Test client GET against placeholder pages (no DB needed) ─────
if 'crm_app' in dir():
    client = crm_app.app.test_client()
    for ep, path in [('healthz', '/healthz'), ('favicon', '/favicon.ico')]:
        try:
            r = client.get(path)
            ok = 200 <= r.status_code < 400
            add('test-client', f'GET {path}', f'{"✅" if ok else "❌"} {r.status_code}')
        except Exception as e:
            add('test-client', f'GET {path}', f'❌ {e}')


# ── 10. Print report ───────────────────────────────────────────────
print()
print(f'{"SUITE":<22} {"TEST":<40} STATUS')
print('-' * 100)
fails = 0
warns = 0
for suite, test, msg in results:
    print(f'{suite:<22} {test[:39]:<40} {msg}')
    if '❌' in msg: fails += 1
    elif '⚠' in msg: warns += 1
print()
print(f'TOTAL: {len(results)} checks · {fails} fail · {warns} warn · {len(results)-fails-warns} pass')
sys.exit(1 if fails else 0)
