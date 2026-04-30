# User Stories — Innovite CRM

## How to read these

**Format:** As a [role], I want to [action], so that [outcome].

**Priority**
- **P0** — must have for launch (without it, the CRM cannot do its job)
- **P1** — needed within 30 days of launch (gap would block scaling, not the first run)
- **P2** — nice to have (improves operator quality of life, not on the critical path)

**Status:** Draft → Scoped → In Progress → Done. Everything below is **Draft** — review and edit before we cost or build.

**Roles**
- **Operator** — Sammy / the Innovite team running the CRM day-to-day
- **Client** — the agency's customer (e.g. Vidora Media, ROCA Accountants) whose pipeline is being run
- **Prospect** — a target business that gets contacted, or a visitor who submits the innoviteai.com form

**Source UI** — every story below points back to the page and element it was derived from, so we can sanity-check the spec against the rendered screen.

---

## Epic 1 — Find new leads

Source UI: the **Find new leads** button appears in three places, all currently disabled:
- `templates/overview.html` line 22 — global, opens the multi-client picker
- `templates/outreach.html` line 11 — global, same picker as Overview
- `templates/client_detail.html` line 34 — single-client, no picker (runs for the current client)

Internally the action runs the scraper / enrichment / scoring pipeline. The user-facing language is **Find new leads** because "pipeline" is internal jargon that doesn't tell an operator what the click does. Keep "pipeline" in code, schema, and `crm.activity_log` action names; never in UI copy.

### US-001 · Find new leads on demand for a single client

**As** an operator
**I want to** click **Find new leads** on a client's detail page and have the scraper find, enrich, and grade leads for that client's configured industries and locations
**So that** I can generate fresh leads for one client without running scripts manually or waiting for the next scheduled run

**Priority:** P0
**Status:** Draft
**Acceptance criteria**
- [ ] Pipeline runs only for the client selected (or the current client if triggered from the client detail page)
- [ ] Reads target industries and locations from `crm.clients.target_industries` / `target_locations`
- [ ] New rows appear in `crm.leads` within 5 minutes for a typical run (≤ 200 candidates)
- [ ] Each new lead has a grade A/B/C/D assigned
- [ ] An entry is written to `crm.activity_log` with action `pipeline_run` and the client id
- [ ] The button shows a loading state while the run is in progress and is disabled to prevent double-submits
- [ ] On failure, an error banner is shown with a short reason and the run is logged with status `failed`

**Notes** — Depends on `innovite-scraper` being reachable from the Render web service (or a separate worker). The button currently exists on Outreach but should also surface on the client detail page so an operator can run a single-client pipeline from where they already are.

---

### US-002 · Schedule daily pipeline runs per client

**As** an operator
**I want to** set a daily run time per client (e.g. 07:00 Europe/London)
**So that** new leads are discovered overnight and the queue is full before the sending window opens

**Priority:** P0
**Status:** Draft
**Acceptance criteria**
- [ ] Daily schedule is configurable per client in **Settings → Clients** (or on the client detail page)
- [ ] Schedule respects Europe/London timezone (matches existing **Sending hours** field in `templates/settings.html`)
- [ ] A run fires at the configured time even if the operator is not signed in
- [ ] Each run writes one `pipeline_run` row to `crm.activity_log`
- [ ] A run summary email is sent to the operator after each run (counts of new / skipped / errored leads)
- [ ] Failed runs raise an alert visible on the **System status** card on Settings
- [ ] An operator can pause or resume a client's schedule from the client detail page without losing the saved time

**Notes** — Naturally pairs with the **Pause all outreach** danger-zone toggle in `templates/settings.html` — pipeline schedules should keep running even when sends are paused, because we still want a queue ready for resume.

---

### US-003 · See history of pipeline runs

**As** an operator
**I want to** view a chronological list of recent pipeline runs (per client and across all clients) with counts and outcome
**So that** I can spot a silently failing schedule without waiting for the lead pipeline to dry up

**Priority:** P1
**Status:** Draft
**Acceptance criteria**
- [ ] Last 30 days of runs visible on the client detail page under a **Pipeline runs** section
- [ ] Each row shows: started at, duration, leads added, leads skipped (duplicates), errors, status (success / partial / failed)
- [ ] Failed runs link to the underlying error (log line or stack trace summary)
- [ ] Runs are paginated or capped at the most recent 100 per client

---

### US-018 · Find new leads for multiple clients via a picker

**As** an operator
**I want to** click **Find new leads** on Overview or Outreach and pick which active clients to run for in a single confirmation modal
**So that** my morning routine is one click and one tick, not opening each client detail page in turn

**Priority:** P0
**Status:** Draft
**Acceptance criteria**
- [ ] Buttons on `templates/overview.html:23` and `templates/outreach.html:12` open a modal listing all active clients
- [ ] Each row shows: client name, last find time (relative), last lead count, plus a hint if targeting is incomplete
- [ ] Clients with empty `target_industries` / `target_locations` render a disabled checkbox and a **Set targeting first →** link to the client detail page
- [ ] Modal estimates duration and external lookup count before the operator commits ("~3–5 minutes · ~140 Google Maps lookups")
- [ ] Confirm button reads **Run for N clients** and updates as the operator ticks / unticks
- [ ] Modal copy is explicit that finding leads ≠ sending emails — sending is gated separately by US-004
- [ ] On confirm, a progress card appears at the top of the page showing per-client state (queued / running / done / failed) with a single-click cancel
- [ ] Each per-client run writes start + finish rows to `crm.activity_log` with action `pipeline_run`
- [ ] Partial failure (one client errors, others succeed) is non-fatal — the failed client surfaces an error banner on its row in the progress card; the operator retries from the per-client detail page (US-001)
- [ ] When all runs complete, the progress card shows totals ("11 new at Vidora, 23 at ROCA"), Total Leads KPI updates, and the Recent Activity feed reflects new entries

**Notes** — Single-client triggering remains via US-001 from the client detail page; the picker is the multi-client convenience layer for the morning routine, not a replacement.

---

## Epic 2 — Outbound Email Engine

Source UI: `templates/outreach.html` — **Today / Sent / Follow-ups / Bounces** tabs (lines 93–110), **Send status per client** panel with Pause/Resume (lines 50–88), and `templates/settings.html` — **Sending email · Zoho** card (lines 62–110), **Sequence cadence** card (lines 113–159).

### US-004 · Send scheduled emails respecting cap and hours

**As** an operator
**I want to** have queued Day 1 emails sent automatically during the configured sending window, capped per client per day
**So that** outbound goes out without me clicking anything, and we never exceed the daily cap that protects sender reputation

**Priority:** P0
**Status:** Draft
**Acceptance criteria**
- [ ] Sender reads from the existing Zoho SMTP settings shown on Settings (host, port, from address)
- [ ] Sends only between the configured **Sending hours · Europe/London** start and end times
- [ ] Skips weekends when **Skip weekends** is checked
- [ ] Per-client daily send cap (`s.daily_send_cap`) is enforced; the (cap + 1)-th email rolls over to the next sending day
- [ ] System-wide pause (Settings → Danger zone → **Pause all sends**) halts every client immediately
- [ ] Each successful send updates `crm.emails.sent_at` and increments the **Sent · 7d** KPI on Outreach
- [ ] Sends respect each client's pause flag (see US-006) — a paused client's queued emails stay queued

**Notes** — The KPIs `pending_today`, `sent_7d`, `reply_rate`, `bounce_rate` on `templates/outreach.html` lines 24–47 all assume this engine is running. Reply rate depends on Epic 3.

---

### US-005 · Auto-schedule Day 3 and Day 7 follow-ups

**As** an operator
**I want to** have Day 3 and Day 7 follow-ups scheduled automatically after a Day 1 email is sent, using the cadence toggles in Settings
**So that** the canonical Innovite cadence (Day 1 → Day 3 → Day 7) runs without manual intervention

**Priority:** P0
**Status:** Draft
**Acceptance criteria**
- [ ] When a Day 1 email is sent, a Day 3 row is created in `crm.emails` with `scheduled_at = sent_at + 3 days` (skipping weekends if **cadence_skip_weekends** is on)
- [ ] When a Day 3 email is sent, a Day 7 row is similarly scheduled at +4 days from Day 3
- [ ] The cadence toggles on Settings (`day1_enabled`, `day3_enabled`, `day7_enabled`) gate scheduling — disabling Day 7 stops the auto-schedule for new sequences but does not delete existing rows
- [ ] Follow-ups appear in the **Follow-ups this week** tab on Outreach with the correct `scheduled_at` and step pill (`step-2`, `step-3`)
- [ ] If a lead replies before a follow-up sends, the queued follow-up is cancelled (see US-009)

---

### US-006 · Pause and resume outbound for a single client

**As** an operator
**I want to** pause a client's outbound queue from the **Send status per client** panel without affecting other clients
**So that** I can hold sends for one client (e.g. mid-onboarding, tech issue, holiday) without taking the whole agency offline

**Priority:** P0
**Status:** Draft
**Acceptance criteria**
- [ ] Existing Pause / Resume button (`outreach_toggle_pause`) writes `crm.clients.outreach_paused`
- [ ] Paused clients show the **Paused** state pill and `is-paused` row state already styled in `style.css`
- [ ] No new sends fire for a paused client; queued rows remain in `crm.emails` with `status='scheduled'`
- [ ] Pipeline runs and reporting are unaffected (matches the panel hint copy on `outreach.html` line 53)
- [ ] An entry is written to `crm.activity_log` for each pause / resume with the operator and client id
- [ ] Resuming releases queued sends to the next valid sending window — it does not blast everything immediately

---

### US-007 · Suppress bounced addresses

**As** an operator
**I want to** click **Suppress** on a bounced row in the Bounces tab and have that address blocked from future sends across all clients
**So that** repeat hard bounces stop hurting our domain reputation, and soft bounces get a configurable retry policy

**Priority:** P1
**Status:** Draft
**Acceptance criteria**
- [ ] **Suppress** button on `outreach.html` (currently disabled, line 211) is enabled and writes to a `crm.suppressed_addresses` table
- [ ] Hard bounces (severity = `hard`) auto-suppress without manual click
- [ ] Soft bounces retry up to 2 times then auto-suppress
- [ ] Suppressed addresses are excluded from any future scheduling, even on a different client
- [ ] An audit row is written to `crm.activity_log` with the address and reason

---

## Epic 3 — Reply Detection

Source UI: `templates/outreach.html` — the **Reply rate · 7d** KPI (line 38) and the **Replied** dot in the Sent tab (line 192). `templates/inbound.html` — the **Needs your response** panel (lines 51–92) and the AI auto-reply section in the drawer (lines 226–232). Reply detection straddles both pages: an IMAP poller flags inbound responses, which feeds reply-rate on Outreach and the response queue on Inbound.

### US-008 · Detect inbound replies via IMAP

**As** an operator
**I want to** have replies to outbound emails detected automatically via IMAP polling on the Zoho mailbox
**So that** the **Replied** dot, the reply-rate KPI, and the inbound response queue all reflect reality without manual reconciliation

**Priority:** P0
**Status:** Draft
**Acceptance criteria**
- [ ] IMAP connection uses the host / port already shown on Settings (`s.sending_email.imap_host` / `imap_port`)
- [ ] Poller runs at least every 5 minutes
- [ ] A message is matched to its outbound email via Message-ID / In-Reply-To headers
- [ ] On match, a row is inserted into `crm.replies` with `received_at`, raw body, and a foreign key to the originating `crm.emails.id`
- [ ] `crm.emails.replied_at` is set on the originating row, which lights the green **Replied** dot in the Sent tab
- [ ] Reply rate on Outreach (`replies / sent`) updates within one poll cycle of the new reply landing
- [ ] Out-of-office / auto-responder replies are flagged and excluded from the reply rate calculation

---

### US-009 · Stop the sequence when a lead replies

**As** an operator
**I want to** automatically cancel any queued Day 3 / Day 7 follow-ups for a lead the moment they reply
**So that** we never send "just bumping this" after someone has already responded — that's the single fastest way to lose a deal

**Priority:** P0
**Status:** Draft
**Acceptance criteria**
- [ ] When a reply is recorded against a lead, all `crm.emails` rows for that lead with `status='scheduled'` move to `status='cancelled'` with reason `reply_received`
- [ ] The cancelled rows disappear from the **Follow-ups this week** tab on Outreach
- [ ] An entry is written to `crm.activity_log` with action `sequence_stopped` and the trigger (`reply_received`)
- [ ] Cancellation is idempotent — a second reply on the same lead does not error or duplicate the audit row

---

### US-010 · Analyse reply sentiment and surface hot replies

**As** an operator
**I want to** have each detected reply tagged positive / neutral / negative / out-of-office, with the positive ones surfaced in the **Needs your response** panel on Inbound
**So that** I can spend my morning on the replies that are warm, not on every "unsubscribe" or auto-responder

**Priority:** P1
**Status:** Draft
**Acceptance criteria**
- [ ] `crm.replies.sentiment` is populated by a Claude Haiku call on the reply body
- [ ] Positive replies appear in the **Needs your response** panel within 10 minutes of detection
- [ ] Negative replies suppress the lead and write `unsubscribed` or `negative_reply` to `crm.activity_log`
- [ ] Out-of-office replies do not change lead status and do not stop the sequence (the queued Day 3 / 7 still send)
- [ ] Each tagged reply records the model id, prompt version, and confidence so we can audit drift

---

## Epic 4 — Inbound Lead Scoring

Source UI: `templates/inbound.html` — score pills `score-hot` / `score-warm` / `score-cold` (lines 70–73, 156–160), **Avg time-to-response** KPI (line 41), and the drawer's **Why this score** + **AI auto-reply** sections (lines 221–232). Today the form on innoviteai.com writes to `public.leads` (per `crm/CLAUDE.md`); the CRM reads the same rows but has no scorer or auto-reply.

### US-011 · Score new form submissions hot / warm / cold

**As** an operator
**I want to** have every new innoviteai.com form submission scored hot / warm / cold within 60 seconds of arrival
**So that** the Inbound page's **Needs your response** panel and KPIs are populated in real time, not retrospectively

**Priority:** P0
**Status:** Draft
**Acceptance criteria**
- [ ] A new submission in `public.leads` is scored within 60 seconds (target p95)
- [ ] Score is one of `hot`, `warm`, `cold` and stored on the inbound row
- [ ] Scoring uses a documented rubric (deal range, target volume, current method, industry, complete vs sparse fields)
- [ ] **Hot · 7d** count and the **Hot %** sub-label on the Inbound KPI block reflect the new score
- [ ] Hot scores appear in the **Needs your response** panel
- [ ] An operator can override the score from the drawer; an override is logged in `crm.activity_log`

**Notes** — `crm.inbound_leads` vs `public.leads` reconciliation is still open (see `crm/CLAUDE.md` open items). Scope this story against whichever table is canonical at build time.

---

### US-012 · Send AI auto-reply within 5 minutes

**As** a prospect who submits the contact form
**I want to** receive a relevant, personal-feeling reply within minutes
**So that** I trust the agency is responsive and book a call before my attention drifts to the next tab

**Priority:** P0
**Status:** Draft
**Acceptance criteria**
- [ ] An auto-reply is sent within 5 minutes of submission (matches the **Avg time-to-response** KPI label)
- [ ] Auto-reply copy is generated by Claude using the prospect's submitted fields (name, company, current method, deal range)
- [ ] Hot scores get a booking-link CTA; cold scores get a softer "we'll be in touch" reply (no booking link)
- [ ] The reply respects the marketing-site copy rules: UK English, no banned words, no emoji, no exclamation marks
- [ ] The sent reply text and timestamp are persisted on the inbound row and shown in the drawer's AI auto-reply section
- [ ] The drawer pill reads `sent <relative time>` when triggered, `not triggered` when suppressed
- [ ] Auto-reply is suppressed for known disposable / role-based addresses (info@, admin@, etc.)

---

### US-013 · Show "why this score" reasoning in the drawer

**As** an operator
**I want to** see a short bullet list explaining why a submission was scored hot / warm / cold (the `drawerWhyGrade` list in `inbound.html`)
**So that** I can trust the score, override it confidently when wrong, and refine the rubric over time

**Priority:** P1
**Status:** Draft
**Acceptance criteria**
- [ ] Each scored row has 3–5 short reasons attached (e.g. "Deal range £5k–£15k", "Currently doing outbound manually", "Industry matches target list")
- [ ] Reasons render in `#drawerWhyGrade` on drawer open
- [ ] Reasons are persisted (not regenerated on each open) so they remain stable for audit
- [ ] When an operator overrides the score, the reasons stay attached to the original score for the audit trail

---

## Epic 5 — Scraper Integration

Source UI: `templates/settings.html` — the **Integrations & API keys** panel (lines 162–191), which already lists masked keys and a `state` for each. Today the panel is read-only and the **Rotate** button is disabled. Stories below assume the underlying integrations actually flow lead data into the pipeline.

### US-014 · Discover candidate businesses from Google Maps

**As** an operator
**I want to** have the pipeline pull candidate businesses for each client's target industries and locations from Google Maps
**So that** the lead pool reflects real local businesses (with addresses, phone numbers, websites) rather than a stale CSV

**Priority:** P0
**Status:** Draft
**Acceptance criteria**
- [ ] For a given client, the scraper queries Google Maps for each (industry × location) pair from `crm.clients.target_industries` / `target_locations`
- [ ] Each unique business returned is upserted into `crm.leads` keyed on (name + postcode) — no duplicates across runs
- [ ] Captured fields include: business name, address, postcode, phone, website, rating, review count
- [ ] The Google Maps integration shows `state='healthy'` on the Settings integrations panel when keys are valid and the last call succeeded
- [ ] When the API quota is exhausted, the integration shows `state='warn'` and the pipeline degrades gracefully (existing leads are kept, run is logged as `partial`)

---

### US-015 · Enrich UK leads with Companies House data

**As** an operator
**I want to** enrich every UK lead with Companies House signals (company number, incorporation date, accounts filed, SIC code, officer count)
**So that** we can score viable, established businesses higher than dormant or pre-revenue companies and avoid wasting cadences on shells

**Priority:** P1
**Status:** Draft
**Acceptance criteria**
- [ ] Each lead with a UK postcode is matched to a Companies House record (best match by name + postcode + locality)
- [ ] Match confidence is stored alongside the result; below-threshold matches are flagged for manual review, not auto-attached
- [ ] Enrichment fields populate the existing `crm.leads` columns (company number, incorporation date, SIC code, officer count) — UI already renders these on the lead detail page
- [ ] The Companies House integration on Settings reflects health (rate-limit usage, last run)
- [ ] Re-running enrichment on a lead is idempotent — no duplicate rows, only newer values overwrite

---

### US-016 · Extract decision-maker contact from website and LinkedIn

**As** an operator
**I want to** have a likely decision-maker (name, role, email, LinkedIn URL) attached to each lead before the Day 1 email is drafted
**So that** the cadence sends to a person, not a generic info@ inbox, which is the single biggest driver of reply rate

**Priority:** P1
**Status:** Draft
**Acceptance criteria**
- [ ] For each lead, the scraper visits the website and attempts to extract: founder / MD / owner name, role, email, LinkedIn URL
- [ ] Where the website is sparse, falls back to a LinkedIn search for the company name + decision-maker role
- [ ] Extracted contacts populate the lead's decision-maker fields, which already render on the lead detail page
- [ ] If no decision-maker can be found with reasonable confidence, the lead is graded D (or flagged) so the Day 1 send doesn't fire to a generic mailbox
- [ ] All contact extraction is logged with source URL and timestamp for auditability

---

## Epic 6 — Client Onboarding

Source UI: `templates/clients.html` — the disabled **Add client** button (line 11) with tooltip "Onboarding flow ships next". Without a way to add clients, none of Epics 1–5 can do anything for a new customer. Logically prerequisite to everything above.

### US-017 · Add a new client via a dedicated form

**As** an operator
**I want to** click **Add client** on the Clients page and fill a single form to set up a new client and their targeting profile
**So that** I can onboard a client in under 90 seconds and have the pipeline ready to run for them

**Priority:** P0
**Status:** Draft
**Acceptance criteria**
- [ ] **Add client** button on `templates/clients.html:11` is enabled and navigates to `/clients/new`
- [ ] The page lives at `/clients/new` (dedicated page, not modal — chip inputs need the room) and uses the existing `settings-card` + `field-row` pattern from `templates/settings.html`
- [ ] Form is grouped into two cards: **Basics** and **Targeting**
- [ ] **Basics fields**
  - Name (required, free text)
  - Industry (free text — canonical list deferred to v2)
  - Contact name (optional)
  - Contact email (optional, validated as email if present)
- [ ] **Targeting fields** (the universal filter set)
  - Target industries — chip input, at least 1 required
  - Target locations — chip input, at least 1 required (UK city / region names for v1)
  - Minimum company age in years — number input, optional
  - Employee size bands — multi-select chips: 1–10 / 11–50 / 51–200 / 200+
  - Active filing only — checkbox, defaults to ON
  - Exclusion list — chip input for domains and company names this client should never contact (their existing customers, their competitors, anyone they've already pitched). Each entry can be a domain (`acme.co.uk`) or a company name (`Acme Ltd`); matcher tries both. Optional.
- [ ] Save POSTs to `/clients/new`, inserts into `crm.clients` with `status='onboarding'`, then redirects to `/clients/<id>` with a flash banner "Client created. Run a pipeline when you're ready."
- [ ] Cancel button returns to `/clients` without writing
- [ ] Validation: cannot save without name + ≥1 target industry + ≥1 target location; inline errors styled like the existing form patterns (no full-page reload required to show them)
- [ ] No automatic pipeline run on save — operator triggers explicitly via US-001
- [ ] An entry is written to `crm.activity_log` with action `client_created` and the operator id

**Notes — schema impact**
- `crm.clients` already has `target_industries` and `target_locations` JSONB columns. The four extra filters (min company age, employee bands, active filing only, exclusion list) need either (a) a new `targeting_filters` JSONB column, or (b) discrete columns. Lean: single JSONB blob since the shape is likely to evolve, with a documented schema in `crm/docs/data-sources.md`.

**Notes — exclusion list scope**
- The new-client form collects only the **client-specific** exclusion list (this client's customers, competitors, partners, prior pitches).
- Auto-applied on top of the client list, never asked on the form:
  - The client's own domain (derived from contact email)
  - Agency-wide do-not-contact list (lives in Settings — covers GDPR / PECR removals, complaints; applies to every client)
  - Cross-client dedup — anything any *other* Innovite client is already cold-emailing, to prevent two clients hitting the same business through the same agency

**Explicitly cut from v1** — kept here so we don't lose the conversation:
- Monthly fee + pricing tier (commercial fields — editable later from the client detail page)
- Google rating + review count floor (only useful for review-heavy target industries; add as optional later)
- Revenue band (data only exists for ~30% of UK private companies; add with caveat label later)
- Advanced industry-specific signals — tech stack, e-commerce signals, hiring signals, certifications, trade-specific keywords (deferred to a v2 **Advanced targeting** card on the client detail page)
- Negative SIC codes / postcode exclusions (defer until an operator actually asks)
- Canonical typeahead lists for industries / locations (free text for v1, tighten when we see what gets typed)

---

## Epic 7 — Clients roster visuals

Source UI: `templates/clients.html` — the row layout currently shows leads · 7d, reply rate · 7d, and a retainer block (monthly_fee + pricing_tier). With those two commercial fields cut from US-017, every client added through the new-client form will render the retainer block blank, while seeded clients (Vidora, ROCA) render it populated. The fix is to repurpose that third column for an operational signal that's relevant for every client regardless of how they were created.

### US-019 · Clients page row shows operational health, not commercial info

**As** an operator
**I want to** see at a glance which clients are healthy, which are stale, and which need attention — without scrolling into the detail page
**So that** the Clients page is the right "morning triage" view for a 1-person agency, not just a roster

**Priority:** P1
**Status:** Draft
**Acceptance criteria**
- [ ] Each row keeps the existing two stat blocks: leads · 7d and reply rate · 7d (with deltas vs prev 7d)
- [ ] Third stat block replaces retainer / pricing tier — content is contextual:
  - Healthy and recent → "N pending today" + "last find Xh ago"
  - Just added (`created_at` < 24h, `leads_7d` == 0) → "Just added · Find new leads →" link
  - Targeting incomplete → "Set targeting →" link
  - Outreach paused → "Sending paused"
  - Stale (last lead added > 7d ago) → "Stale · Nd · Find new leads →" link
- [ ] Each row gets a 3px left-border state colour: green (healthy), amber (stale), red (paused), grey (just-added or targeting incomplete)
- [ ] Meta line under the client name swaps "contact name" for the first 2–3 target locations (a quick "yes targeting is right" sanity check). Contact name moves to the client detail page.
- [ ] `monthly_fee` and `pricing_tier` are removed from the row entirely; both already surface on the client detail page in the existing **Retainer** metric block, so no new UI is required to preserve the info
- [ ] No schema change — `db.list_clients()` adds two subqueries (last lead added, pending today) and exposes `outreach_paused` + a `targeting_empty` flag derived from `target_industries` / `target_locations` being null or empty
- [ ] Status pill (Active / Paused / Churned) stays — that's lifecycle state, distinct from operational state
- [ ] **Sort order surfaces problems first**. Primary: lifecycle status (active → paused → churned). Within active: op_state priority — `needs_targeting` → `paused` → `stale` → `just_added` → `healthy`. Within `stale`: most-stale first. Within other states: newest first. Reasoning: a wall of green rows above the one amber row defeats the purpose of the colour-coding — the eye has to scan past everything healthy before reaching what needs attention.
- [ ] Defer filter chips ("Needs attention (3) · Healthy (12)") until clients > 8 — for a 2-client agency the sort alone is enough.

**Notes** — This is the first place we lean into "the row tells you what to do next" instead of "the row dumps every field we know". Reuse the same idiom on Reports / Inbound rosters when those rows get redesigned.

---

## Epic 8 — Lead lifecycle

Source UI: `templates/leads.html` — the row layout, status tabs (line 56–77), and bulk-action dropdown (line 102–112). The schema CHECK constraint on `crm.leads.status` allows seven values today (`new`, `contacted`, `replied`, `meeting`, `won`, `lost`, `closed`), but only six have defined meanings — `closed` exists in the bulk dropdown without a tab and without documented purpose. Until the lifecycle is locked down, the Leads page can't be a triage view because the ranking of "what needs me" isn't defined.

### US-020 · Define the canonical lead lifecycle

**As** an operator
**I want to** know exactly what each lead status means, who sets it, and what comes next
**So that** I trust the status I'm looking at, automation moves leads when it should, and reporting reflects reality

**Priority:** P0
**Status:** Draft
**Acceptance criteria — six canonical statuses**

| Status | Meaning | Set by | Exits to |
|---|---|---|---|
| `new` | Pipeline found and graded the business. No outbound has been sent. | Auto (Find new leads / US-001) | `contacted`, `lost` |
| `contacted` | At least one outbound email has been sent. Lead is in or post cadence, awaiting reply. | Auto (US-004 send) | `replied`, `lost`, `meeting` (rare direct) |
| `replied` | Reply detected via IMAP (US-008). Sequence auto-stopped (US-009). Awaiting operator triage. | Auto | `meeting`, `won`, `lost` |
| `meeting` | Discovery / sales call booked. Calendar event exists. | Operator | `won`, `lost` |
| `won` | Became a paying customer. Deal closed. | Operator | (terminal) |
| `lost` | Declined, ghosted post-cadence, or operator disqualified. | Operator or auto (sequence + 14d cooling, see below) | (terminal) |

- [ ] `closed` is dropped from operator-facing UI (status tabs, bulk-action dropdown, `LEAD_STATUSES` constant). Schema CHECK constraint left as-is to avoid a destructive migration; a follow-up cleanup migration can remove it once we confirm zero rows use it.
- [ ] **Auto-transition `contacted` → `lost`** fires when a lead has been in `contacted` past Day 7 + 14 days with no reply detected. Operator can revert. Without this, `contacted` grows unbounded and the page becomes useless.
- [ ] **Auto-transitions write to `crm.activity_log`** with the trigger reason (e.g. `auto_lost_no_reply`).
- [ ] Operators can move any non-terminal lead to any other status manually (jump from `new` straight to `meeting` is allowed — sometimes inbound prospects skip outbound entirely).
- [ ] Terminal statuses (`won`, `lost`) can still be reverted by the operator if mis-clicked, but no automation moves them.

**Notes — `meeting` granularity** — kept as one state for v1. If reporting needs show-rate, add a `meeting_held_at` timestamp later rather than splitting into `meeting-booked` / `meeting-held` / `meeting-no-show`.

---

### US-021 · Leads page as morning triage view

**As** an operator
**I want to** open the Leads page and immediately see what needs my action — replies waiting, meetings booked — without scanning every row
**So that** the page is the right "morning triage" view for the lead funnel, not just a paginated dump

**Priority:** P0
**Status:** Draft
**Acceptance criteria**
- [ ] **Drop the Client column** — page is always scoped to one client, so showing the same name on every row is noise
- [ ] **Drop the Decision-maker column** — frequently empty, not actionable at the list level. Move under the business name as a subline (`Manchester · Sarah Cole`)
- [ ] **Add a Stage time column** — how long the lead has been in its current status (e.g. `Replied · 6h`, `Contacted · 14d`, `New · 3h`). Tells you whether something is hot or going cold without clicking in.
- [ ] **3px coloured left border per row** — driven by lifecycle status (US-020), not grade (since scoring is deferred):
  - **Amber** — `replied` (needs response from operator)
  - **Green** — `meeting` (booked, in motion)
  - **Grey** — `new` (just discovered, no action yet) or `won`/`lost` (terminal, dimmed)
  - **No border** — `contacted` (in active cadence, normal flow)
- [ ] **Default sort: needs-attention first** — replied → contacted (newest in cadence) → new (newest first) → meeting → won → lost. The existing "Sort · Recent / Grade / Score" dropdown stays as a manual override.
- [ ] **Needs-attention banner** above the table when relevant: e.g. `3 replies waiting · 2 meetings booked` with click-through to filtered views. Renders only when there's at least one item.
- [ ] **Grade pill stays** — renders when `lead.grade` is set, `—` when null. The page does not depend on grade for any op-state logic, so it works whether or not scoring is in.
- [ ] No new schema columns. `stage_time_label` and `op_state` are computed in `db.leads_search()` from existing fields (`status`, `updated_at`, `created_at`).

**Notes — scoring is deliberately deferred** — see "Out of scope for this draft" below. The page is built to absorb a `grade` value when scoring lands later, with no UI changes required.

---

## Epic 9 — Inbox

Source UI: `templates/inbox.html` — replaces the old Inbound page. The morning question "did anything come back?" was previously split between the Inbound page (form submissions) and the reply-rate KPI on Outreach. One operator, one queue.

The Outreach page keeps its identity as the **sends-side** dashboard (Today / Sent / Follow-ups / Bounces). The Inbox is the **responses-side** dashboard. That's the cut.

### US-022 · Unified Inbox replaces Inbound

**As** an operator
**I want to** open Inbox in the morning and see every reply + every form submission still waiting on me, in one queue
**So that** I never wonder "did I check Inbound? did I check Outreach?" again — there's one place

**Priority:** P0
**Status:** Draft
**Acceptance criteria**
- [ ] `/inbound` route renamed to `/inbox`. Sidebar nav reads "Inbox". Old `/inbound` URL 301-redirects to `/inbox` so any bookmarks survive.
- [ ] Three tabs:
  - **Needs you** — `crm.replies` joined to `crm.leads` where lead status = `replied` (not yet triaged) + `crm.inbound_leads` where status = `new`
  - **Drafts** — empty state for now (AI auto-reply generation is US-023)
  - **Done** — reply parent-leads moved past `replied` (meeting / won / lost) + inbound forms past `new`
- [ ] Reply rows link to the lead detail page (operator changes lead status there). Form rows open the existing slide-out drawer (no JSON contract change — drawer hits `/inbound/<id>.json` and `/inbound/<id>/status` per stable internal IDs).
- [ ] Each row shows: signal pill (sentiment for replies, score for forms), name + company, subject, body snippet, kind tag (Reply / Form), and relative time.
- [ ] Sort: most recent first across both kinds, sorted by `received_at`.
- [ ] No new schema. Reuses `crm.replies`, `crm.leads`, `crm.clients`, `crm.inbound_leads`. The `crm.inbound_leads` vs `public.leads` reconciliation stays deferred — Inbox queries whichever the inbound page currently uses.

**Notes** — Old `templates/inbound.html` deleted in this build. The supporting `inbound_kpis()` / `inbound_tab_counts()` / `inbound_needs_response()` / `inbound_list()` functions in `db.py` are dead code and should be removed in a follow-up cleanup; left in place this build to keep the diff focused on the page rename + new query layer.

---

### US-023 · AI auto-reply drafts (deferred — UI stub only this build)

**As** an operator
**I want to** have AI draft a personalised reply for every inbound reply within ~60s of detection, queued in the Drafts tab for one-click approve / edit
**So that** I can handle 50 replies/day in five minutes instead of an hour, without sending generic-feeling responses

**Priority:** P0 (deferred — biggest leverage feature once the backend lands)
**Status:** Draft
**Acceptance criteria** — placeholder, to be filled when scoping
- [ ] A reply detected via IMAP triggers an Anthropic call that drafts a reply tailored to: the reply text + sentiment + intent, the original outbound, the lead's enrichment profile, and the client's voice
- [ ] Draft appears in Inbox > Drafts tab within 60s of detection
- [ ] Operator can: **Approve & send** (one click), **Edit & send** (modal), or **Discard** (mark reply as needing manual handling)
- [ ] Approve writes to `crm.emails`, sets the lead's status to `meeting` or keeps `replied` based on intent, and writes a `draft_approved` row to `crm.activity_log`
- [ ] Discard rate per draft is tracked per client so we can spot when the AI is consistently off-tone for a particular voice
- [ ] Drafts older than 24h are auto-archived (operator missed them — not a blocker, the lead detail page is still the fallback)

**Notes** — Single biggest leverage feature for the agency model. Defer until: (a) IMAP reply detection (US-008) is wired, (b) we have ~30 real replies to use as training data for the prompt's tone-matching examples.

---

## Out of scope for this draft

These belong in later epics or separate docs — recording here so we don't lose them:

- **Reports (Step 9)** — per-client performance dashboard, exportable. Has its own template stub at `templates/reports.html` but no UI surface yet.
- **Auth** — POC has no auth (per `crm/CLAUDE.md` open items); single-password gate planned before public link-out.
- **`crm.inbound_leads` vs `public.leads` reconciliation** — affects Epic 4 acceptance; needs a schema decision before US-011 is scoped.
- **Background worker service on Render** — Epics 1, 2, 3 all assume a worker process. The decision on one-vs-two services should land before any of these are built.
- **Lead scoring rubric** — the schema already has `grade` (A/B/C/D/F) and `overall_score` columns, but the rubric for assigning them isn't specced yet. Three pillars sketched (Viability, Reachability, Buying signal) but the per-client weights need real conversion data to tune. Revisit after ~30 contacted leads have real reply data (smallest sample to start tuning weights). Until then the Leads page (US-021) treats grade as decoration — present if set, dash if null, never load-bearing for the op-state logic.
