# Backend plan — Innovite CRM

**Status:** Draft for review · 2026-05-02
**Scope:** Steps 7–10 backend (UI is shipped; this doc scopes the engine behind it).

## How to read this

The CRM at `innovite-crm.onrender.com` and the marketing site at `innoviteai.com` are both running. The marketing site has a working Vercel form handler (`innovite-website/api/submit.js`) and the CRM has a working Flask UI for every Step 7–10 surface. What is missing is the engine: a runner that finds leads, an engine that sends mail, a poller that catches replies, and a generator for reports. This document scopes that engine.

Where the brief in the user message conflicts with shipped code, the conflict is flagged in **Decisions needed** at the bottom rather than silently picking a side.

Read the data-sources brief (`crm/docs/data-sources.md`) and the user stories (`crm/docs/user-stories.md`) alongside this doc — both are referenced throughout.

---

## ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## BACKEND ARCHITECTURE
## ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Five services. They split on lifecycle, not on technology — Form Handler runs on Vercel because the form lives there; everything else runs on Render against the same Supabase Postgres the CRM uses. There is no microservices model; the four Render services share a database and talk through it where they can.

```
                       innoviteai.com (Vercel)
                              │
                              ▼
                  ┌─────────────────────┐
                  │  1. FORM HANDLER    │  serverless · already 80% built
                  └──────────┬──────────┘
                             │  inbound_leads + auto-reply
                             ▼
            ╔══════════════════════════════════════════╗
            ║  Supabase Postgres · crm schema          ║
            ║  + crm.inbound_leads / leads / emails    ║
            ║    replies / activity_log / mailboxes    ║
            ╚════╤═════════════════╤═════════╤═════════╝
                 │                 │         │
   ┌─────────────┴───┐  ┌──────────┴──┐  ┌───┴────────────┐
   │ 2. PIPELINE     │  │ 3. OUTREACH │  │ 4. REPLY       │
   │    RUNNER       │  │    ENGINE   │  │    ENGINE      │
   │ (cron + button) │  │ (cron 5m)   │  │ (cron 10m)     │
   └─────────┬───────┘  └─────────────┘  └────────────────┘
             │
             ▼
   ┌─────────────────┐
   │ innovite-scraper│  Tier-1 HTTP via TLS fingerprint, Redis queue
   │  (existing repo)│  worker pool, currently extracts <title> only —
   └─────────────────┘  needs per-source extractors built (Epic 5)

                        ┌──────────────────┐
                        │ 5. REPORTING     │  weekly cron
                        │    ENGINE        │
                        └──────────────────┘
```

**Single Render worker process.** Services 2/3/4/5 are not separate Render services. They are four cron jobs and a tiny HTTP server inside one `worker` process — a Python `apscheduler` or `rq` loop is enough at this scale. Reasons:

- One process means one DB pool, one log stream, one set of env vars.
- Render bills per service. Four workers = four bills for an agency with two clients.
- Tasks are short and IO-bound. They will not contend.
- The CRM's `app.py` stays a pure web service (per `crm/CLAUDE.md` — Render health check at `/healthz` must not touch DB; that contract stays).

A second process / second Render service is the right call **only** when a single task starts blocking the others, which is not at 2 clients and not at 10. Earn complexity.

---

## SERVICE 1 — FORM HANDLER

### What it does

Receives every submission from innoviteai.com — the qualifier modal and the 5-step application form — validates and scores it, persists it, fires an AI auto-response and a chat notification, and returns 200 fast enough that the page never feels stuck. The handler is the public face of the funnel: a slow or flaky one breaks the funnel.

### What triggers it

HTTP POST from the marketing site's form JS in `innovite-website/app.js`. The site is static; the form fetches `/api/<endpoint>` on the same Vercel domain.

### What it reads / writes

- **Reads:** none from a database. Pulls config from environment variables only.
- **Writes:** one row to `crm.inbound_leads` per submission (this doc proposes consolidating onto the CRM schema and retiring `public.leads` — see decision #1).
- **Side effects:** one transactional email to the lead, one chat notification, one optional founder-notification email.

### External services

- **Supabase Postgres** — direct REST insert via `SUPABASE_SERVICE_ROLE_KEY`.
- **Resend** — both the auto-response email and the founder notification.
- **Anthropic Claude Haiku** — generates the personalised auto-reply copy. Falls back to a templated email if the call times out (8s) or the key is missing.
- **Slack OR Discord webhook** — the brief says Discord; the shipped code uses Slack. Decision #2.

### Effort estimate

**6–10 hours** to converge the shipped Vercel handler with the brief in the user message:

| Sub-task | Hours |
|---|---|
| Decide single endpoint vs split (`/api/apply` + `/api/qualify`) | 0 (decision only) |
| Switch chat notification from Slack → Discord (or keep Slack — decision #2) | 1 |
| Point insert at `crm.inbound_leads` instead of `public.leads`, drop the parallel table | 2 |
| Confirm `from sammy@innoviteai.com` matches the configured `FROM_EMAIL` | 0.5 |
| Pin scoring rubric to deal-value + decision-maker + timeline (current rubric uses q1/q2/q3/q4 — same set, different framing) | 1 |
| Add a 5-min SLA monitor (log a warning if Resend dispatch lags > 60s) | 1 |
| Validation hardening for the new fields (5-step form payload — see decision #3) | 1.5 |
| QA across Hot / Warm / Cold / no-fit on real form | 1 |

### Dependencies

- **Decision #1** (which inbound table is canonical).
- **Decision #2** (Slack vs Discord).
- **Decision #3** (one endpoint vs two; the brief says two — they share 95% of the code).
- The Resend domain (`innoviteai.com`) and the Anthropic key are already provisioned (per `innovite-website/CLAUDE.md`).
- Nothing else. This service is independently shippable on day 1 and does not block any other service.

### Notes on what's already there

`innovite-website/api/submit.js` already does ~80% of this. It validates name + email, scores Hot/Warm/Cold/no-fit, inserts into `public.leads`, fires Slack, sends a founder notification, and generates an Anthropic-personalised auto-reply with a templated fallback. The deltas to the brief are surgical: where it writes, what chat platform it uses, and whether the scoring rubric column names match the brief.

---

## SERVICE 2 — PIPELINE RUNNER

### What it does

Turns "find new leads for ROCA" into rows in `crm.leads`. For each (industry × location) the client has configured, it discovers candidate businesses, enriches them across the 9 data sources, scores them, queues Day 1 emails, and writes a PDF intelligence report per lead. Pipeline runs are the single most expensive thing the agency does per client — most of the £/client/month budget gets spent here.

### What triggers it

Three triggers, in priority order:

1. **Operator click** — `POST /api/pipeline/run` with `{client_id, mode}` from the **Find new leads** modal (`crm/templates/_find_leads_modal.html`). Synchronous-ish: writes a `pipeline_runs` row, returns the run id, the modal polls.
2. **Daily cron** — per-client schedule, default 07:00 Europe/London. Reads `crm.client_pipeline_schedules` (new table — decision #5).
3. **New-client onboarding hook** — when `crm.clients.status` transitions to `onboarding → active`, fire a single bootstrap run (this is debated — US-017 says "no automatic pipeline run on save"; the operator triggers explicitly).

### What it reads / writes

**Reads from `crm`:**
- `clients.target_industries` / `target_locations` / `targeting_filters`
- `clients.exclusion_list` (auto-applied on top: client's own domain + agency-wide DNC + cross-client dedup)
- existing `leads` (to dedupe by `(business_name, postcode)`)

**Writes to `crm`:**
- `leads` — upserts new candidates, never duplicates an existing one for the same client.
- `pipeline_runs` (new table — decision #6) — one row per run with started/finished, counts, errors.
- `activity_log` — `pipeline_run` action, with detail JSON containing the run summary.
- `emails` — Day 1 drafts queued to `scheduled` status, ready for the Outreach Engine.

### External services

The 9 data sources from the brief, mapped against the existing data-sources doc:

| # | Source | Provider (recommended) | Auth | Multilogin? |
|---|---|---|---|---|
| 1 | Google Maps | Google Places API (Place Details + Nearby Search) | API key | No |
| 2 | Website analysis | PageSpeed Insights API + custom Python (SSL/CTA/load) | Free / API key | No |
| 3 | Instagram | Apify perfectscrape actor | Apify token | No |
| 4 | Companies House | Companies House Public API | Free API key | No |
| 5 | LinkedIn | Apollo.io (cached LinkedIn URLs — DO NOT scrape) | API key | No |
| 6 | Google Reviews | Outscraper (full review bodies) | API key | No |
| 7 | Facebook | Apify Facebook Pages actor | Apify token | No |
| 8 | Meta Ad Library | Meta Ad Library API (free, public) + scraper fallback | None / scraper | Maybe — see source 8 below |
| 9 | Job boards | Indeed RSS + LinkedIn Jobs via Apify | Mixed | No |

The scraper repo's TLS-client-api gives Chrome-fingerprinted requests through rotating sticky proxies — which covers most "scrape a public page" jobs (Meta Ad Library, Facebook Pages, job boards) without Multilogin. **Multilogin is only needed if we end up logged-in scraping Instagram or LinkedIn**, which the data-sources doc explicitly recommends against (legal + ToS exposure). Recommendation: **skip Multilogin**; if a future source forces it, revisit. See decision #8.

### Effort estimate

This is the biggest service. Effort by phase:

| Phase | Hours |
|---|---|
| `POST /api/pipeline/run` endpoint on CRM, reads client config, writes a `pipeline_runs` row, returns run id | 4 |
| Polling endpoint (`GET /api/pipeline/run/<id>`) for the modal progress card | 2 |
| Worker side: pick up runs from DB, dispatch source jobs to scraper API | 6 |
| Source 1 (Google Places — discovery + reviews structured fields) | 6 |
| Source 4 (Companies House — match by name + postcode, derive `revenue_band` from filings) | 8 |
| Source 5 (Apollo — decision-maker name/title/email/LinkedIn URL) | 4 |
| Source 6 (Outscraper review bodies for sentiment) | 4 |
| Source 2 (PageSpeed + SSL + CTA detection) | 4 |
| Source 3 (Apify Instagram, compute ER in Python) | 4 |
| Source 7 (Facebook Pages — followers, last post date) | 3 |
| Source 8 (Meta Ad Library — competitor ad tracking) | 6 |
| Source 9 (job board hiring signals) | 6 |
| Scoring (A/B/C/D/F + `overall_score`, weakness profile, hook type) | 6 |
| PDF intelligence report generator (per lead, per client template) | 12 |
| Day-1 email draft generator (Anthropic, per-lead personalisation) | 6 |
| Dedup + exclusion-list logic (3 layers per US-017 notes) | 4 |
| Rate-limit + spend-cap enforcement (£ ceiling per run, kill-switch) | 4 |
| Observability — per-source latency, hit rate, cost/run logging | 3 |
| End-to-end run for one real client + tuning | 8 |

**Total: ~90 hours** for all 9 sources. **MVP slice: ~32 hours** (sources 1, 4, 5, plus scoring and the scaffold) — enough to demo a credible run and produce graded leads with decision-maker contacts. Sources 2, 3, 6 add another ~16 hours. Sources 7, 8, 9 are P1 — defer until a client asks.

### Dependencies

- Service 1 doesn't need to be done; this service does not depend on inbound forms.
- Scraper integration option (decision #4) — A/B/C from the brief.
- API keys provisioned for Google, Apollo, Apify, Outscraper, Companies House, Bouncer (decision #9).
- `crm.pipeline_runs` table (decision #6).
- Per-client schedule storage (decision #5).
- A real client willing to accept the first imperfect run as the testbed (Vidora is the natural pick — already seeded, in-house relationship).

---

## SERVICE 3 — OUTREACH ENGINE

### What it does

Sends queued emails out of `crm.emails` via Zoho SMTP, rotating across the healthy mailbox pool, respecting per-mailbox daily caps, business hours, system pause, and per-client pause. After each send it schedules the next step (Day 3, then Day 7) on the cadence. Skips leads that have replied. Handles bounces. This service is what differentiates "we have a CRM" from "we have a working agency" — it's the daily heartbeat.

### What triggers it

Cron tick every 5 minutes between configured business hours (default 09:00–17:00 Europe/London). The first thing the loop does is read `crm.settings.system_outreach_paused` — if true, it returns immediately. Cheap, frequent, idempotent.

### What it reads / writes

**Reads:**
- `emails` where `status='scheduled' and scheduled_at <= now()` — the work queue.
- `mailboxes` filtered to `health_state in ('healthy','warming') and paused=false and sent_today < daily_cap`.
- `clients.outreach_paused` — per-client gate (US-006).
- `settings.sending_hours`, `settings.cadence`, `settings.system_outreach_paused`.
- `replies` to detect "lead has replied, skip queued sends" (the cancellation also happens on the Reply Engine side, but the sender double-checks).
- `suppressed_addresses` (new table, decision #7).

**Writes:**
- `emails.sent_at`, `emails.status='sent'`.
- `mailboxes.sent_today` (increment).
- `emails` — new rows for Day 3 and Day 7 follow-ups, scheduled at `sent_at + 3d` / `sent_at + 7d` (skipping weekends if configured).
- On bounce: `emails.status='bounced'` + `bounces` table (new).
- `activity_log` — `email_sent`, `email_bounced`, `cadence_scheduled`, `cadence_cancelled`.

### External services

- **Zoho SMTP** (`smtp.zoho.eu:587`) — credentials per mailbox, password via env var (the env-var name is stored on the row, the secret stays in env config — already specced in `0009_mailboxes.sql`).
- **Bouncer** (or Anymailfinder) — pre-send email verification, optional. Cheaper per send than recovering from a hard bounce.

### Mailbox rotation rules

Each tick picks one email to send, and one mailbox to send it from:

1. Filter mailboxes to `healthy/warming + has capacity + paused=false`.
2. If the lead's client has a dedicated mailbox (`mailboxes.dedicated_client_id = client_id`), prefer that one if it has capacity. Otherwise fall back to pool.
3. Within the pool, weight by remaining capacity (`daily_cap - sent_today`). Avoids hammering one healthy mailbox while a fresh one sits idle.
4. Enforce **2-minute jitter between sends from the same mailbox** (the brief's "rate limiting"). Handled in Redis with a `last_send:{mailbox_id}` timestamp.
5. If no mailbox has capacity, the email rolls to next tick — that's correct behaviour, not an error.

### Bounce detection

Two paths:

- **Synchronous** — SMTP returns 5xx → mark `bounced` immediately, hard-suppress the address.
- **Asynchronous** — bounce arrives later as a DSN to the sending mailbox. Caught by the Reply Engine on the next IMAP poll, which writes the bounce back to the corresponding `emails` row. (Yes — the Reply Engine handles bounces because that's where IMAP already lives. Don't run two IMAP poller loops.)

### Effort estimate

| Sub-task | Hours |
|---|---|
| Cron loop scaffold + system pause check | 2 |
| Mailbox-pick logic with capacity-weighted rotation | 4 |
| SMTP send via `smtplib.SMTP_SSL` per-mailbox creds | 4 |
| Per-mailbox 2-min jitter via Redis | 2 |
| Threading headers (`Message-ID`, `In-Reply-To`, `References`) for follow-ups | 2 |
| Cadence auto-scheduling (Day 1 → Day 3 → Day 7, weekend skip) | 3 |
| Reply-cancellation guard (skip if `replies` exists for the lead) | 1 |
| Synchronous bounce handling + suppressed_addresses table | 3 |
| `sent_today` increment with daily reset (midnight Europe/London cron) | 2 |
| Per-mailbox health_state transitions (auto-throttle on >2% bounce rate) | 3 |
| Activity-log writes for every send/bounce/cancel | 1 |
| QA — send to a controlled test inbox, verify threading, verify cap enforcement | 4 |

**Total: ~31 hours.**

### Dependencies

- Pipeline Runner has produced at least one `crm.emails` row to send.
- Zoho mailbox passwords loaded into env (already specced — env-var name is on the row, secret separately in Render env).
- Suppressed-addresses table (decision #7).

---

## SERVICE 4 — REPLY ENGINE

### What it does

Watches every active mailbox's IMAP inbox, matches inbound messages to outbound emails via standard mail headers, classifies sentiment with Claude, cancels the cadence on positive/neutral replies, and notifies the operator. Without this service, reply rate is fiction and the cadence is irresponsible (we'd keep sending follow-ups after someone has replied).

### What triggers it

Cron tick every 10 minutes. Connects in parallel to all `mailboxes.health_state in ('healthy','warming')`, fetches messages newer than the last seen UID per mailbox, processes each.

### What it reads / writes

**Reads:**
- `mailboxes` for IMAP credentials (host/port/user, password from env).
- `emails` to match `Message-ID` ↔ `In-Reply-To` / `References`.
- `mailbox_state` (new tiny table, decision #11) — last seen UID per mailbox.

**Writes:**
- `replies` — every matched inbound message, with raw body, headers, sentiment.
- `emails.replied_at` on the originating row.
- `emails.status='cancelled'` for any other `scheduled` rows for the same lead.
- `leads.status='replied'`.
- `bounces` / `emails.status='bounced'` when the inbound is a DSN (handled here, not in Outreach Engine — see Service 3 note).
- `activity_log` — `reply_received`, `sequence_stopped`, `bounce_detected`, `auto_responder_skipped`.

### External services

- **Zoho IMAP** (`imap.zoho.eu:993`) — same credentials as SMTP per mailbox.
- **Anthropic Claude Haiku** — sentiment classification on the reply body. Returns `positive | neutral | negative | ooo` per US-010.
- **Discord webhook** (or Slack — decision #2) — push on positive replies and bounces.

### Reply matching rules

Standard mail-thread matching, in order:

1. Inbound `In-Reply-To` matches our `emails.message_id` → confident match.
2. Inbound `References` chain contains one of our `message_id` → match to that lead.
3. Inbound `from_address` matches a `leads.decision_maker_email` for which we have a recent `emails.sent_at` → soft match (logged as `reply_match=heuristic`).
4. No match → write to a `replies` row with `lead_id=null`, surface in Inbox > Needs you for manual triage.

DSN messages (RFC 3464) are routed to the bounce handler instead of the reply handler. The classifier runs `headers["content-type"] starts_with multipart/report` first.

### Effort estimate

| Sub-task | Hours |
|---|---|
| IMAP poller skeleton, per-mailbox parallel connect | 4 |
| Last-UID state, exactly-once message handling | 3 |
| Header-based reply matching | 4 |
| DSN parsing for bounces, route to bounce handler | 3 |
| Sentiment classification via Claude | 2 |
| OOO / auto-responder detection (subject regex + return-path heuristics) | 2 |
| Cadence cancellation on positive/neutral reply | 2 |
| Discord/Slack push on positive reply | 1 |
| Lead status auto-advance to `replied` | 1 |
| Activity-log writes | 1 |
| QA — replay 20 real Zoho replies (good, OOO, bounce, unsubscribe) | 5 |

**Total: ~28 hours.**

### Dependencies

- Outreach Engine has sent at least one email so there is something to match against.
- `emails.message_id` column needs to be added if it doesn't exist on the schema (decision #11). Threading without it is fragile.
- IMAP credentials per mailbox in env.

---

## SERVICE 5 — REPORTING ENGINE

### What it does

Generates a weekly performance report per active client and emails it. Mostly a recap — what happened in the last 7 days — plus the funnel and the wins. The CRM `Reports` page already renders this data live (`crm/templates/reports.html` is fully wired); this service produces a PDF + email of the same content for clients who don't have a CRM login.

### What triggers it

Cron tick weekly, default Monday 08:00 Europe/London (configurable per client).
Manual trigger via a kebab on the Reports page (`Email this report` already exists as a `mailto:`; the new endpoint replaces it with a real send).

### What it reads / writes

**Reads:**
- All the same SQL the Reports page already uses (`db.reports_kpis`, `reports_chart_series`, `reports_funnel`, `reports_sequence`, `reports_wins`, `reports_narrative`).
- `clients.contact_email` for the recipient.
- `clients.goals` (US-034 — to render target lines).

**Writes:**
- A row to `client_reports` (new table, decision #12) — period, generated_at, pdf_path, sent_at, recipient.
- `activity_log` — `client_report_sent`.
- File: PDF to Supabase Storage or Render persistent disk (decision #13).

### External services

- **WeasyPrint** or **Playwright** for HTML → PDF. WeasyPrint is lighter (no Chromium), so default to it; Playwright only if the print stylesheet needs JS rendering for charts. Chart.js renders client-side, so the PDF either needs Playwright headless OR pre-render the chart server-side as SVG. Recommendation: **server-side SVG via matplotlib** — already used by other ops tooling, no Chromium dependency. Decision #14.
- **Resend** — same provider as the inbound auto-reply, sends from `sammy@innoviteai.com`.

### Effort estimate

| Sub-task | Hours |
|---|---|
| Weekly cron loop, picks active clients | 1 |
| Server-side chart rendering (matplotlib SVG) | 4 |
| HTML report template (reuse existing `reports.html`, strip operator-only chrome) | 3 |
| WeasyPrint integration + branded PDF wrapper | 4 |
| Resend send with PDF attachment + plain-text body | 3 |
| Founder-summary email — short text-only digest to `sammy@innoviteai.com` aggregating all client deltas | 2 |
| `client_reports` audit table | 1 |
| QA — render for both seeded clients, sanity-check on print | 3 |

**Total: ~21 hours.**

### Dependencies

- Outreach Engine + Reply Engine running long enough to produce real numbers (a one-week soak before sending the first real client report — otherwise the report says "1 email sent, 0 replies").
- Resend domain authentication (already in place for inbound auto-reply).
- Decision on PDF generator (#14) and storage (#13).

---

## ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## SCRAPER INTEGRATION
## ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

### Recommendation: **Option B — scraper exposes an API, CRM calls it**

The scraper repo already does exactly this. `innovite-scraper/api/src/index.ts` runs an Express server with `POST /scrape`, `GET /scrape/:id`, `GET /scrape` — the contract is built. Adding a few more endpoints (`/scrape/google-places`, `/scrape/companies-house-search`, etc., or a single `/jobs/<source>` with a discriminator) is a small lift on top of the existing BullMQ-Redis-Postgres scaffold. The CRM's pipeline runner becomes a thin orchestrator that POSTs jobs and polls for results.

### Why not Option A (scraper writes directly to CRM database)

Tempting because it removes a hop. Three reasons not to:

1. **Two writers to one schema is a recipe for races and broken constraints.** The CRM's Flask app and the scraper's Node worker would both need to know the full `crm.leads` schema, including the CHECK constraints, the trigger, and the partial indexes. Drift becomes inevitable.
2. **The scraper already has its own Postgres** (`scrape_results`) on a separate volume, separate auth, separate connection pool. Pointing it at Supabase means new env vars, new RLS reasoning, new latency profile.
3. **Coupling pins us to a single CRM.** If we ever spin up a sister project, or run the scraper for a non-Innovite use, Option A means each consumer carries scraper code. Option B means each consumer makes HTTP calls.

### Why not Option C (Redis/RabbitMQ between them)

The scraper already has Redis internally for BullMQ. Putting *another* queue between CRM and scraper API is two queues for two services. It's the right shape at 10+ clients with multiple concurrent runs and partial-result streaming. It is overkill at 2 clients with daily batch runs that complete in minutes. Bring it back when the API call timing becomes the bottleneck, which it won't for the first ~12 months. (The data-sources doc estimates ~140 Maps lookups + ~40 enrichments per client per day = under 200 jobs, well inside what `POST /jobs` + poll handles.)

### What to build for Option B

- **On the scraper repo:** add per-source extractors as new modules under `worker/src/scrapers/`, mirroring the existing `title.ts`. Branch in the worker on a `job_type` field. Already documented in the README.
- **On the CRM:** a small `scraper_client.py` with `submit_jobs(jobs: list[dict]) -> list[job_id]` and `wait_for_results(job_ids, timeout=300) -> list[result]`. Polling, not webhooks — the scraper API returns results synchronously after the job finishes, and the CRM is happy to wait inside its run worker.
- **Auth:** shared bearer token in env (`SCRAPER_API_TOKEN`). The scraper API is on a private Render network or behind a Render IP allowlist; the bearer is defence in depth.

### Cost of Option B vs A

Latency: one extra HTTP hop per source per lead. At 200 leads × 5 sources × 100ms = 100s extra per run. Acceptable — pipeline runs already take 3-5 minutes (per US-018 modal copy).

Engineering: ~6 hours of API surface work on the scraper side, ~4 hours on the CRM client. ~2 hours saved on schema-coupling debugging that A would have inflicted on us inside a month.

---

## ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## DATA SOURCES FOR SCRAPER
## ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This section maps each of the 9 sources to a concrete provider, drawing on the research already in `crm/docs/data-sources.md`. **None of the recommended providers require Multilogin** — we win that bet by sticking to public APIs and headless-but-not-logged-in scraping for Meta. Multilogin enters the conversation only if we ever need to log in to Instagram or LinkedIn, and the data-sources doc explicitly recommends against that path.

### Source 1 — Google Maps (business discovery)

- **Data:** name, address, phone, website, lat/lng, category, Google rating, review count, Maps URL, opening hours.
- **Access:** Google Places API — Nearby Search (discovery), Place Details (per-business enrichment). Official REST.
- **Multilogin:** No.
- **Rate limits:** 100 QPS shared across the project; $200/mo free credit ≈ 6k Place Details calls.
- **Risks:** None — it's the official API. Cost scales with call volume; budget ~£40–80/mo per client at full coverage.
- **Value to report:** the spine. Every other source pivots off the (business, address) pair Google Places returns.
- **Effort:** ~6 hours (auth, paginator, rate-limit handling, write to `crm.leads` columns already specced).

### Source 2 — Website analysis (HTTP scoring)

- **Data:** SSL flag, CTA detection, response time, Core Web Vitals, performance score, tech-stack hints (optional).
- **Access:** PageSpeed Insights API (free, 25k queries/day) for performance + CWV. Custom Python via `requests` for SSL + CTA + load-time. Both free.
- **Multilogin:** No.
- **Rate limits:** PSI 240/min — generous.
- **Risks:** PSI can return errors when the target site blocks the lighthouse runner; fall back to local Python checks.
- **Value:** the "weakness profile" is mostly built from this — slow site, no CTA, no SSL all become email hooks.
- **Effort:** ~4 hours.

### Source 3 — Instagram (engagement data)

- **Data:** followers, posts, last-post date, avg likes, computed engagement rate, bio, link.
- **Access:** Apify perfectscrape actor — pre-built, ~£0.0003/profile.
- **Multilogin:** No (Apify runs the scraping in their cloud; we just call their API).
- **Rate limits:** Actor-dependent. At a few hundred profiles per run, no realistic ceiling.
- **Risks:** Instagram changes its DOM regularly; actor maintenance is on Apify, but expect occasional stale runs. Have a 24h cache.
- **Value:** moderate — useful for content/agency clients (Vidora) and outbound hooks ("posting 3x/wk, no website CTA"). Less useful for clients targeting professional services.
- **Effort:** ~4 hours.

### Source 4 — Companies House (financial signals)

- **Data:** company number, SIC code, incorporation date, status, officers, recent filings, accounts metadata.
- **Access:** Companies House Public API. Free.
- **Multilogin:** No.
- **Rate limits:** 600 requests / 5 minutes — comfortable.
- **Risks:** Most UK SMBs file abridged accounts; turnover is rarely directly available. Derive a `revenue_band` from latest filing type ("micro-entity" → <£632k, "abridged" → £632k–£10.2m, "full" → £10.2m+). Document the heuristic.
- **Value:** very high. Filters out dormant / pre-revenue / shell companies that look real on Google but waste a cadence slot.
- **Effort:** ~8 hours (matching by name + postcode is the fiddly bit; below-threshold matches need the `flagged for manual review` path per US-015).

### Source 5 — LinkedIn (decision maker names)

- **Data:** decision-maker name, title, LinkedIn URL, work email (~65–70% hit rate).
- **Access:** Apollo.io API. **Use Apollo's cached LinkedIn data — do not scrape LinkedIn directly.** Apollo carries the legal exposure; we don't.
- **Multilogin:** No (and explicitly do not scrape with PhantomBuster — UK GDPR + LinkedIn ToS combination is a bad bet for the controller; covered in `data-sources.md`).
- **Rate limits:** 600 req/min on Apollo Basic.
- **Risks:** Apollo's UK micro-SMB coverage is partial. Plan a fallback (Anymailfinder) for misses.
- **Value:** highest single signal. A named decision-maker swings reply rate 3-5x vs `info@` cold outreach. Without this, the cadence is essentially throwaway.
- **Effort:** ~4 hours for Apollo + 2 hours for Anymailfinder fallback = ~6 hours.

### Source 6 — Google Reviews (sentiment analysis)

- **Data:** full review bodies (Place Details only returns 5; need Outscraper for the rest), historical trend, recent negative review count.
- **Access:** Outscraper Reviews API, ~$2/1k reviews.
- **Multilogin:** No.
- **Rate limits:** Light.
- **Risks:** ToS-grey (Outscraper sits in the same legal posture as most Maps scrapers — they take the heat). Fine for v1; revisit if/when the legal posture changes or volume rises.
- **Value:** gives the sentiment hook for the email ("noticed your last 3 reviews mentioned slow response — we help service businesses fix that"). Strong differentiator.
- **Effort:** ~4 hours including the Claude sentiment pass over the bodies.

### Source 7 — Facebook (page data)

- **Data:** Facebook page existence, page likes, last post date, posting frequency.
- **Access:** Apify Facebook Pages actor, or direct scrape via the existing tls-client-api.
- **Multilogin:** No (public pages are fetchable without login).
- **Rate limits:** Apify actor's; light at our volume.
- **Risks:** Many UK service businesses have abandoned Facebook pages. Useful as a "still active on social" signal, not as a primary source.
- **Value:** low to moderate. Skip unless a specific client targets local service businesses heavily.
- **Effort:** ~3 hours.

### Source 8 — Meta Ad Library (competitor ad tracking)

- **Data:** active ads run by competitor pages, ad creative samples, run dates.
- **Access:** Meta Ad Library API (free, public, no auth) for political + issue ads. **For commercial ads** the API access is more restricted; the Ad Library website is public but usage requires scraping (the API only returns commercial ads filtered by EU geo).
- **Multilogin:** No for the API. **Yes potentially** if commercial scraping ever needs to log in to dodge interstitials — a real concern for the .com Ad Library on heavy use. Build it without first; if blocked, then evaluate Multilogin or Bright Data datasets.
- **Rate limits:** API-side ~200 req/hr; scrape side rate-limited per the tls-client-api defaults.
- **Risks:** Meta tightens commercial ad library access regularly. Plan for scraper rot.
- **Value:** moderate — a "your competitors X and Y are running ads, you aren't" line is a strong Day 1 hook. P1, not P0.
- **Effort:** ~6 hours (API path) or ~10 hours if scraping is needed.

### Source 9 — Job boards (hiring signals)

- **Data:** open roles posted in the last N days, role titles, sometimes salary bands.
- **Access:** Indeed RSS feed (free, public, per-employer); LinkedIn Jobs via Apify; Glassdoor scrape as fallback.
- **Multilogin:** No.
- **Rate limits:** Indeed RSS unrestricted; Apify per actor.
- **Risks:** Hiring signals decay fast; cache for 7 days max.
- **Value:** strong buying signal — "you're hiring 3 sales reps; we help service firms scale outbound without scaling headcount" is one of the better Day 1 angles. P1.
- **Effort:** ~6 hours.

### Build effort summary

| Source | P0/P1 | Hours |
|---|---|---|
| 1. Google Maps | P0 | 6 |
| 4. Companies House | P0 | 8 |
| 5. LinkedIn (via Apollo) | P0 | 6 |
| 2. Website analysis | P0 | 4 |
| 6. Google Reviews | P0 | 4 |
| 3. Instagram | P1 | 4 |
| 7. Facebook | P1 | 3 |
| 8. Meta Ad Library | P1 | 6 |
| 9. Job boards | P1 | 6 |
| **Total** | | **47** |

P0 only: 28 hours. The rest is incremental and gated on whether a real client's positioning needs them.

---

## ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## BUILD ORDER
## ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Optimise for: **(1) demoable to a prospect by end of week 2**, **(2) revenue-generating by end of week 3**, **(3) backend complete by end of week 4**.

The principle: build the front-of-funnel first (Form Handler converges with brief), then the engine that turns leads into emails (Pipeline Runner MVP + Outreach Engine), then the loop that closes the loop (Reply Engine), then the polish that keeps clients happy (Reporting Engine). Each week ends in a state that could be demoed.

### Week 1 — Inbound funnel + scraper API surface

**Goal:** every form submission flows correctly and the scraper repo is ready to receive enrichment jobs from the CRM.

| Day | Deliverable |
|---|---|
| Mon | Decisions sign-off on #1–#4 (canonical inbound table, Slack vs Discord, endpoint shape, integration option) |
| Mon–Tue | Service 1 convergence — point at `crm.inbound_leads`, switch to Discord (or keep Slack), split endpoints if desired, deploy. ~6 hours. |
| Wed–Thu | Scraper repo: extend the Express API with per-source job submission. Implement Google Places + Companies House extractors in `worker/src/scrapers/`. ~16 hours. |
| Fri | `scraper_client.py` on the CRM side. End-to-end: CRM submits 5 test businesses, scraper enriches, results returned. ~6 hours. |

**End of week 1:** a real form submission lands cleanly in the CRM and triggers the auto-reply. The scraper can take a list of UK businesses and return Companies House + Google Places enrichment.

### Week 2 — Pipeline Runner MVP + scoring + Apollo

**Goal:** the operator can click **Find new leads** and 30 minutes later see graded leads in the CRM with named decision-makers. This is the demo to the prospect.

| Day | Deliverable |
|---|---|
| Mon–Tue | `POST /api/pipeline/run` endpoint, `pipeline_runs` table, modal progress wiring. ~6 hours. |
| Tue–Wed | Apollo integration on the scraper side. Apply scoring rubric in CRM after enrichment. ~10 hours. |
| Wed | Dedup + exclusion-list logic. ~4 hours. |
| Thu | Day-1 email draft generator (Anthropic, queues to `crm.emails` as `scheduled`). ~6 hours. |
| Fri | First end-to-end run for Vidora Media. Tune rubric weights against the real output. ~6 hours. |

**End of week 2:** Sammy can demo a live "Find new leads for ROCA" run to a prospect. Leads have grades, decision-maker names, Day-1 drafts ready to send. **No emails go out yet** — sending is week 3.

### Week 3 — Outreach Engine + Reply Engine

**Goal:** real outbound starts. Replies come back. The cadence runs.

| Day | Deliverable |
|---|---|
| Mon–Tue | Outreach Engine — cron loop, mailbox rotation, SMTP send, cap enforcement, per-mailbox jitter. ~12 hours. |
| Wed | Cadence auto-scheduling (Day 3, Day 7) + reply-cancellation guard + suppressed-addresses table. ~6 hours. |
| Wed | First real send — single mailbox, single client (Vidora), 5 test sends. Verify threading. ~3 hours. |
| Thu | Reply Engine — IMAP poller, header matching, sentiment classification, lead-status transitions, Discord/Slack notifications. ~12 hours. |
| Fri | Bounce handling end-to-end (DSN parse → suppress → activity log). ~6 hours. |

**End of week 3:** the agency is generating revenue-eligible behaviour — emails go out on cadence, replies are caught, the CRM reflects reality. **Single client only**, single sending domain, single mailbox to start. Soak before scaling.

### Week 4 — Reporting Engine + scaling + P1 sources

**Goal:** the loop is closed. A client gets a weekly PDF. Sources 6, 2, 3 land for richer hooks. Scale to the full mailbox pool.

| Day | Deliverable |
|---|---|
| Mon | Server-side chart rendering (matplotlib SVG). PDF template + WeasyPrint integration. ~8 hours. |
| Tue | Weekly cron, per-client send via Resend, founder-summary digest. ~6 hours. |
| Wed | Sources 6 (Google Reviews bodies + sentiment) and 2 (PageSpeed + SSL/CTA) land — strengthens Day-1 hooks. ~8 hours. |
| Thu | Source 3 (Instagram via Apify). ~4 hours. |
| Thu–Fri | Scale: enable the full pool of healthy mailboxes, onboard ROCA Accountants alongside Vidora, run the second client through end-to-end. ~10 hours. |

**End of week 4:** the backend is feature-complete to the brief. P1 sources (Facebook, Meta Ads, Jobs) remain for when a specific client positioning needs them.

### What is *not* in this 4-week plan and is fine to defer

- AI auto-reply drafts on inbound replies (US-023 — biggest leverage feature once the volume justifies, ~30 real replies needed).
- Public sharable report URL (US-035 — single-password gate first, US-035 second).
- Lemwarm/Instantly API integration (US-040 — manual entry is fine until 20+ mailboxes).
- Auto-transition `contacted → lost` after 14 days (US-020 — needs the Outreach Engine to have been running 21 days first).
- `crm.lead_status_history` table (US-030 notes — `updated_at` is right ~95% of the time).

---

## ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## DECISIONS NEEDED FROM ME
## ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

These need to be settled before any code starts. None take more than a sentence to answer; some block multiple services.

### Inbound funnel

**1. Canonical inbound table — `public.leads` or `crm.inbound_leads`?**
Today the Vercel handler writes to `public.leads`; the CRM has a parallel `crm.inbound_leads` table that is empty. Pick one. (Recommendation: **B — `crm.inbound_leads`**, retire `public.leads`. Keeps everything CRM-shaped in one schema, lets RLS work.)
- A) Keep `public.leads` as canonical, point CRM Inbox to read from it.
- B) Switch Vercel handler to write `crm.inbound_leads`, drop `public.leads`.

**2. Chat notification platform — Slack or Discord?**
Brief says Discord. Shipped code uses Slack (env var `SLACK_WEBHOOK_URL`).
- A) Slack (no work — it already runs).
- B) Discord (1 hour to swap webhook + payload shape).

**3. Form endpoint shape — single `/api/submit` or split `/api/apply` + `/api/qualify`?**
Brief says split. Shipped code is one endpoint that handles both shapes (qualifier is optional fields on the same payload).
- A) Keep single endpoint (faster, less surface area).
- B) Split (matches brief, doubles the URL surface; ~2 hours).

### Pipeline + scraper

**4. Scraper integration option — A, B, or C?**
Recommendation: **B**. Confirm or override.

**5. Per-client schedule storage — column on `crm.clients` or new `crm.client_pipeline_schedules` table?**
- A) `clients.daily_pipeline_run_at TIME` column (simple, one cron time per client).
- B) New table with `(client_id, cron_expr, paused, last_run_at)` (extensible — multiple schedules per client, arbitrary cron).
- Recommendation: **A** until a client actually wants two daily runs; promote to B when needed.

**6. `crm.pipeline_runs` table — yes or no?**
Recommendation: **yes** (US-003 explicitly asks for run history; the modal needs it for progress polling). Confirm.

**7. `crm.suppressed_addresses` table — yes, or just a flag on `inbound_leads`?**
- A) New table (clean, audit-friendly, supports "added by", reason, expires_at).
- B) `is_suppressed` boolean on each lead row (simpler, but no audit).
- Recommendation: **A** — required by US-007 and the bounce flow.

### Operations

**8. Multilogin — adopt now or stay on tls-client-api?**
Recommendation: **stay on tls-client-api**. None of the 9 sources require logged-in access under the recommended providers. Confirm or override.

**9. API keys to provision in week 1**
Confirm Sammy will set up accounts for: Google Cloud (Places + PSI), Apollo.io Basic, Apify, Outscraper, Companies House, Bouncer (P1: ZeroBounce as backup, Anymailfinder for Apollo misses). Total run-rate ~£90–140/mo per active client (per `crm/docs/data-sources.md` Stack Recommendation).

**10. Single Render worker process or separate services per engine?**
Recommendation: **single worker process** running all four cron jobs (cheaper, simpler, sufficient at this scale). Confirm.

### Schema additions

**11. Schema deltas for the engine**
Confirm we can add (each is `add column if not exists` / `create table if not exists`):
- `crm.emails.message_id text` — for IMAP threading.
- `crm.emails.kind text default 'cadence'` — values `cadence | manual_reply | autoreply` (already implied by US-024).
- `crm.emails.cancel_reason text` — for `reply_received | manual_reply_sent | suppressed`.
- `crm.mailbox_state` (table) — `mailbox_id, last_uid bigint, last_polled_at`.
- `crm.bounces` (table) — full DSN copy + classification.

**12. `crm.client_reports` audit table**
For weekly report sends. Confirm.

**13. PDF storage location**
Render persistent disk, Supabase Storage, or just regenerate-on-demand and never store?
- Recommendation: **Supabase Storage**, public-read with token URL. Stays alongside the rest of the data, no extra service.

**14. PDF generator — WeasyPrint or Playwright?**
Recommendation: **WeasyPrint** + matplotlib SVG for charts (no Chromium dependency; saves ~400MB on the worker image). Confirm or override.

### Sequencing

**15. First real client — Vidora or ROCA?**
Recommendation: **Vidora** for the first end-to-end soak (in-house relationship, more forgiving of imperfect first-run output). Onboard ROCA in week 4. Confirm.

**16. Email volume cap during week-3 soak**
Recommendation: **30 sends/day total across all mailboxes** until reply rate and bounce rate land in expected range (≥ 3% reply, < 2% bounce on a healthy domain). Then ramp. Confirm.

---

## Notes on what's missing from this plan

- No security/auth scope for the CRM itself (the public read-only report URL needs the single-password gate first; that's a separate ~3-hour story per US-035).
- No load testing — at 2 clients, hitting any limit is months away.
- No observability/alerting beyond per-job logs and Discord/Slack pings on positive replies + bounces. A Sentry or BetterStack hookup is a 1-hour add when needed.
- No GDPR/PECR compliance audit — the agency-wide do-not-contact list (per US-017 notes) is the operational lever; documenting the lawful-interest assessment for cold UK B2B outreach is a Sammy-side legal task, not engineering.

This is enough to ship the whole agency-grade backend in 4 weeks with one engineer. The risk is not capability — it's discipline against the 47-source temptation. P0 first; everything else when a real client asks.
