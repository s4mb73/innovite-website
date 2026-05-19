# Innovite CRM

## What this is
Internal lead-management + outreach platform for the Innovite agency.
Lives inside the marketing-site repo at `/crm/`; deploys to a self-hosted VPS.

- Production: `https://app.innovite.io`
- Hosting: Hetzner ARM VPS at `46.225.208.85` (Ubuntu 24.04). nginx reverse-proxies to gunicorn (`crm-web.service`, systemd, bind `127.0.0.1:8080`, 2 workers). Working dir on the box: `/srv/innovite/innovite-website/crm/`. Env file: `/etc/innovite/crm-web.env` (perms 600, owner `deploy`).
- Branches: `claude/fix-text-consistency-ocvsN` (production) / `claude/code-feedback-Zua33` (mirror).
- Deploy: GitHub Actions workflow `.github/workflows/deploy-crm.yml` triggers on push to the production branch when `crm/**` changes — SSHes into the VPS with a restricted deploy key that forces `/srv/innovite/deploy.sh` (git fetch + reset, pip install, systemctl restart, `/healthz` check).
- Repo: `s4mb73/innovite-website`
- Database: Supabase Postgres, schema `crm`. Same project as the marketing site (`api/submit.js` writes inbound form leads to `public.leads`).

## Tech stack
- Python 3.11 + Flask 3 + gunicorn — single web service
- psycopg 3 with raw SQL — **no ORM**. Decision: SQLAlchemy adds tax we haven't earned. Reconsider only when the schema starts fighting the SQL.
- Jinja2 templates + small JS islands. Chart.js via CDN for the dashboard.
- Supabase pooler at port 6543 (transaction pooler) via `DATABASE_URL`
- Health check at `/healthz` (does not touch DB — important: changing this breaks the circuit-breaker recovery flow). Polled by deploy.sh after every restart, also by external uptime monitoring.
- Anthropic Claude API for AI features (drafting, sentiment) — env `ANTHROPIC_API_KEY`. May share the marketing site's key or use a separate one (decision pending).
- Email infrastructure (planned, Step 7+): Zoho SMTP + IMAP via env vars.

## File layout
```
crm/
  app.py                      Flask routes (16 endpoints)
  db.py                       psycopg connection + query functions
  models.py                   typed dataclasses (sparse — added per-step)
  templates/
    base.html                 220px sidebar + main, undo banner global
    overview.html             dashboard
    clients.html              client roster (rows, not cards)
    client_detail.html        single-client deep view
    leads.html                leads table with filters/search/sort/bulk
    lead_detail.html          single-lead full dossier
    outreach.html             placeholder (Step 7)
    inbound.html              placeholder (Step 8)
    reports.html              placeholder (Step 9)
    settings.html             placeholder (Step 10)
  static/style.css            all CSS, design tokens at top
  migrations/
    0001_init.sql             schema + 6 tables + RLS + seed clients
    0002_demo_seed.sql        idempotent demo data — ~40 leads, emails,
                              replies, activity_log entries
  tests/qa_smoke.py           static QA harness (30 checks, no DB needed)
  docs/
    data-sources.md           UK B2B enrichment provider research
    plan-2026-04-30.txt       day-1-of-Step-7 daily plan
  requirements.txt
  Procfile                    web: gunicorn app:app -b 0.0.0.0:$PORT --workers 2
  runtime.txt + .python-version  pin Python (legacy from Render-era; VPS uses system Python 3.12 via venv)
  .env.example
  .gitignore
  README.md
```

## Design system

The CRM is a **light theme** in the main content area with a **dark sidebar** —
two contexts in one app. Tokens cascade via CSS custom properties. The
sidebar re-pins the dark palette via a scoped block in
`static/style.css`. Read tokens from `:root` for main, from `.sidebar`
for sidebar. Do not invent new tokens.

(Pre-2026-05-19 the CRM was uniform dark — diverged from the marketing
site at https://innovite.io which is still dark. Accept the split.)

### Colours — main content (`:root`)
- `--bg: #ffffff`
- `--s1: #f7f7f8` / `--s2: #eef0f3` / `--s3: #e4e6eb`
- `--border: rgba(11,12,17,.08)` / `--b2: rgba(11,12,17,.14)`
- `--t1: #14151d` (near-black) / `--t2: #5a5d68` / `--t3: #9a9da8`
- `--accent: #3d7cf5` (saturated blue — primary on white)
- `--accent-h: #5a94ff`
- `--accent-bg: rgba(61,124,245,.08)` / `--accent-bg2: rgba(61,124,245,.04)`
- `--accent-deep: #2563eb` (darker still — focus rings, large headings)
- `--green: #1f9d62` / `--green-bg: rgba(31,157,98,.10)`
- `--amber: #b97a14` / `--amber-bg: rgba(185,122,20,.10)`
- `--red: #d33b3b`

### Colours — sidebar (`.sidebar` scope override)
- `--bg: #0b0c11`
- `--s1: #14151d` / `--s2: #1a1c25` / `--s3: #242732`
- `--border: rgba(255,255,255,.05)` / `--b2: rgba(255,255,255,.09)`
- `--t1: #ecedf3` / `--t2: #9a9da8` / `--t3: #5e6069`
- `--accent: #8FB7FF` (light periwinkle reads on dark)

### Type
- **Switzer** (variable axis, ITF / Fontshare) for UI — neo-grotesque, less ubiquitous than Outfit. CRM has diverged here from the marketing site (which still uses Outfit) — accept this until the marketing site is refreshed.
- **Newsreader** (Google Fonts, 400 / 500 + italic) for page titles + metric numbers ONLY. Do not spread the serif to card titles, badges, tab labels, or anything < 17px — it loses meaning when over-applied.
- **Body baseline**: Switzer 14px / weight 350 / line-height 1.6 / letter-spacing 0.005em. (Weight + tracking originally tuned for dark-mode; left as-is post-light-flip — text is now `--t1: #14151d` near-black on white, still reads clean. Revisit if it looks thin on light bg.)
- **Type scale (6 sizes only)**: `11.5 / 13 / 14 / 17 / 22 / 28`. Never invent intermediate sizes. The cluster between 10 and 14 (10 / 10.5 / 11 / 12 / 12.5 / 13.5) is the classic "AI-slop" fuzzy-hierarchy tell — pick 11.5 or 13.
- **Tabular figures** (`font-variant-numeric: tabular-nums`) on every metric, every table column.
- Min font size 11.5px. **Never** Inter, Arial, system-default. Never Outfit-look-alikes (DM Sans, Plus Jakarta, Manrope) — the whole point of Switzer is escaping that tier.

### Radii
- `--r-sm: 10px` (buttons, inputs, pills)
- `--r-md: 16px` (cards)
- `--r-lg: 24px` (CTA blocks, modals)

### Easing
- `--ease: cubic-bezier(.2,.6,.2,1)` — quiet, no overshoot

## Anti-patterns — never ship these

1. Purple gradients
2. Cards inside cards (surfaces are flat)
3. Bounce or elastic easing
4. Pure grey (everything tinted blue)
5. Inter / Arial / system fonts
6. Body text below 12px
7. Generic dingbats as icons (use inline SVG)
8. Hard-coded colour literals in component CSS — always reference tokens. The light/dark split relies on selectors composing against `var(--…)`, not on baked-in hex codes.
9. Light text on light backgrounds — primary button text must be `#ffffff` on the saturated `--accent`. In the sidebar (dark context) buttons use `var(--bg)` instead — that's the dark-context `#0b0c11`, NOT white.
10. Synthetic placeholder data presented as real — always label demo data (see migration 0002 header comment for the wipe pattern).
11. Editable enrichment fields via the UI — pipeline writes enrichment, humans don't manually fix from the UI. Status + notes are the only editable lead fields.
12. PostgREST keyword args after `**` expansion in Jinja — `url_for('x', **qs, page=N)` is valid Python but Jinja's parser rejects it. Use `qs.copy() + .update({'page': N})`.

## Schema (`crm` schema, see `migrations/0001_init.sql`)

```
crm.clients          (active client roster — Vidora Media, ROCA Accountants seeded)
   ↓ 1:many
crm.leads            (deeply enriched outbound targets, ~50 fields)
   ↓ 1:many
crm.emails           (Day 1 / 3 / 7 scheduled or sent)
   ↓ 1:many
crm.replies          (inbound responses, sentiment column)

crm.inbound_leads    (innovite.io form submissions — overlaps with public.leads,
                      reconciliation deferred — see migration note)
crm.activity_log     (audit trail; foreign keys nullable)
```

RLS enabled on every CRM table with **no policies** — anon/authenticated keys can't touch CRM data. Flask backend uses the service-role key, which bypasses RLS. The MCP read-only user has SELECT-only grants.

JSONB for `target_industries`, `target_locations`, `weakness_profile`. CHECK constraints (not enums) on every status / grade / sentiment column. Indexes on every FK + on `leads.status`, `leads.grade`, `leads.created_at`, plus partial indexes on `emails.scheduled_at where status='scheduled'` and `replies.processed where processed=false`.

## Working style — applies to every Claude session in this directory

- **Take positions; defend them.** Don't list options when the user asks for a recommendation.
- **Push back when wrong.** Cheaper than letting the user ship the wrong thing.
- **Shape brief → wireframe → 2–3 specific questions → wait → build.** Never code without confirmation on >1-line changes.
- **Match complexity to scale.** A 1-person CRM doesn't need microservices. Earn complexity.
- **Boring stack wins.** Postgres + raw SQL > ORM. Server-rendered Jinja > SPA. One process > microservices.
- **Empty / loading / error states are part of the deliverable** — every page has all three.
- **Idempotent migrations.** `if not exists`, `on conflict do nothing`. Numbered SQL files in `migrations/`.
- **Type hints on the data layer.** Every db function returns `dict` / `dataclass`.
- **Defensive defaults.** Bad input → graceful banner, not 500. The dashboard surviving a DB blip is worth 5 lines of try/except (we already have this pattern, see `app.py:overview`).
- **Smoke-test before claiming done.** Run `crm/tests/qa_smoke.py` after schema/route/template changes.
- **Commit messages explain *why*, not just what.**
- **Push to feature branches** (`claude/code-feedback-Zua33` is mine; `claude/fix-text-consistency-ocvsN` is production). Never to `main` (no `main` exists on origin).

## Routes registered (16)

| Method | URL | Purpose |
|---|---|---|
| GET | `/` | Overview dashboard |
| GET | `/clients` | Client roster |
| GET | `/clients/<id>` | Single client |
| GET | `/leads` | Leads table |
| GET | `/leads/<id>` | Single lead |
| GET | `/leads.csv` | CSV export with current filters |
| POST | `/leads/bulk-status` | Change status on N leads, audit + 60s undo |
| POST | `/leads/undo` | Revert most recent bulk change (TTL 60s, session) |
| POST | `/leads/<id>/status` | JSON: change one lead's status |
| POST | `/leads/<id>/notes` | JSON: save notes (debounced live save, 800ms) |
| GET | `/outreach` | placeholder (Step 7) |
| GET | `/inbound` | placeholder (Step 8) |
| GET | `/reports` | placeholder (Step 9) |
| GET | `/settings` | placeholder (Step 10) |
| GET | `/healthz` | health probe — must not touch DB (deploy.sh and uptime monitors poll this; if it touches DB, breaker re-trips) |
| GET | `/favicon.ico` | 204 — silences Chrome request that ignores the data-URI in `<link rel="icon">` |

## Open items

- **Auth**: POC has no auth. Single-password gate planned before public link-out.
- **`crm.inbound_leads` vs `public.leads`**: marketing form writes to `public.leads`; CRM has parallel `crm.inbound_leads`. Inbound page (Step 8) will read from `public.leads` until consolidated.
- **Background worker** for Step 7+ (pipeline, scheduler, IMAP poller) will be a second systemd unit (`crm-worker.service`) on the same VPS, sharing the venv. Env file already provisioned at `/etc/innovite/crm-worker.env` with `COMPANIES_HOUSE_API_KEY` and `SCRAPER_API_TOKEN`. Apscheduler-based, single process, all four engines (pipeline / outreach / reply / reporting) co-resident.
- **Pricing tier on `crm.clients`** is internal — never displayed on the marketing site (see root `CLAUDE.md`).

## Before completing any task

Ask:
1. Does it match the existing tokens (no new colours, fonts, radii)?
2. Have I introduced any of the 12 anti-patterns above?
3. Is the change idempotent on re-run / re-render?
4. Does it respect `prefers-reduced-motion`?
5. If migration: re-runs cleanly?
6. If route: passes the QA smoke harness (`crm/tests/qa_smoke.py`)?
7. If template: empty / loading / error states all rendered?
8. If schema: indexes on the columns we'll filter / sort by?
9. Have I committed with a message that explains *why*?

If any answer is wrong, fix it before reporting done.
