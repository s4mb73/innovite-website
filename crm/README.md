# Innovite CRM

Internal lead-management + outreach platform for the Innovite agency.
Lives inside the marketing-site repo for convenience; deploys separately to Railway.

## What's here

```
crm/
  app.py                 ← Flask app, one route per nav item
  db.py                  ← psycopg connection + helpers (fetch_all/one, execute)
  models.py              ← typed dataclasses (added per-step)
  templates/
    base.html            ← layout: 220px sidebar + main content area
    overview.html, clients.html, leads.html, lead_detail.html,
    outreach.html, inbound.html, reports.html, settings.html
    (plus client_detail.html)
  static/style.css       ← all CSS, dark theme, design tokens
  migrations/
    0001_init.sql        ← Supabase schema (Step 1)
  requirements.txt
  Procfile               ← web: gunicorn app:app
  runtime.txt            ← python-3.11.10
  .env.example
  .gitignore
```

## Stack

- **Database**: Supabase Postgres, schema `crm`. Same project as the marketing site.
- **Backend**: Python 3.11 + Flask + gunicorn. psycopg with raw SQL — no ORM.
- **Frontend**: server-rendered Jinja templates. Chart.js via CDN for the dashboard.
- **Hosting**: Railway (Linux container, auto-deploys from GitHub).
- **AI**: Claude API for reply sentiment + email drafting (added Step 7+).
- **Email**: Zoho SMTP (send) + IMAP (poll for replies) (added Step 7+).

## Deploying to Railway (one-time setup)

1. **Create a new Railway service** in your project → "Deploy from GitHub repo" → pick `s4mb73/innovite-website`.
2. **Service settings → Source → Root Directory** → set to `crm`. (Otherwise Railway tries to deploy the marketing site.)
3. **Service settings → Variables** → add:
   - `DATABASE_URL` — copy from Supabase → Project Settings → Database → Connection string → **Transaction pooler** (port 6543). Paste the whole `postgresql://...` URI.
   - `FLASK_SECRET_KEY` — any long random string. (Only used once auth lands.)
4. **Service settings → Networking → Generate Domain** to get a `*.up.railway.app` URL.
5. Push to the branch Railway watches → it builds and deploys automatically.

After that, every `git push` to that branch redeploys. Logs visible in Railway → Deployments → (latest) → View Logs.

## Routes (Step 2 — placeholders)

| URL                    | Page                | Real content arrives in |
|------------------------|---------------------|--------------------------|
| `/`                    | Overview dashboard  | Step 3                  |
| `/clients`             | Client roster       | Step 4                  |
| `/clients/<id>`        | Single client       | Step 4                  |
| `/leads`               | All leads, filtered | Step 5                  |
| `/leads/<id>`          | Single lead         | Step 6                  |
| `/outreach`            | Email queue + sends | Step 7                  |
| `/inbound`             | Form submissions    | Step 8                  |
| `/reports`             | Per-client reports  | Step 9                  |
| `/settings`            | Email/system config | Step 10                 |
| `/healthz`             | Health probe        | live now                |

## Open items

- **Auth**: POC has no auth. Railway URL is unguessable but the app must not be linked publicly until a single-password gate is added.
- **`crm.inbound_leads` vs `public.leads` overlap** — see migrations note.
- **Background worker** — the pipeline / scheduler / IMAP poller will live in a second Railway service later (Step 7+). Same repo, different start command.
