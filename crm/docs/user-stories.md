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

### US-024 · Reply to a lead from Inbox > Needs you without leaving the page

**As** an operator
**I want to** open a reply in **Needs you**, see the full thread, type a response, and send it from inside the CRM
**So that** I can clear the morning queue without bouncing between Zoho and the CRM, and the conversation stays attached to the lead

**Priority:** P0
**Status:** Draft

**Acceptance criteria**
- [ ] Clicking a reply row in **Needs you** opens a slide-out drawer over the Inbox (same drawer pattern as the existing inbound drawer in `templates/inbox.html`) — does not navigate away
- [ ] Drawer header: lead name, company, sentiment pill, status pill, link to `/leads/<id>` for the full dossier
- [ ] Drawer body renders the full thread chronologically — every `crm.emails` row sent to this lead and every `crm.replies` row received, oldest first, scrolled to most recent on open
- [ ] Each thread message shows: direction (You / Lead), relative time (absolute on hover), subject (collapsed if same as parent), and rendered body with a "view raw" toggle
- [ ] Composer at the bottom of the drawer:
  - From — pre-filled from `s.sending_email.from_address`, read-only
  - To — pre-filled from the lead's reply-from address, read-only
  - Subject — pre-filled `Re: <original subject>`, editable
  - Body — empty, plain-text, with the previous message appended as `> ` quoted lines below the cursor
- [ ] Send action POSTs to `/inbox/<reply_id>/send` and:
  - Sends via Zoho SMTP using existing `s.sending_email` host / port / from
  - Sets `In-Reply-To` and `References` from the original reply's `Message-ID` so threading is preserved in Zoho's web UI and on the recipient's side
  - Inserts a row into `crm.emails` with `kind='manual_reply'`, `sent_at=now()`, body, subject, lead_id, client_id
  - Marks the original `crm.replies` row as `processed=true` with `processed_at=now()`
  - Writes `manual_reply_sent` to `crm.activity_log`
  - Row leaves **Needs you**, parent lead surfaces in **Done**
- [ ] Send button states: idle ("Send reply"), sending (spinner, disabled), error (inline banner with retry, typed text preserved), success (drawer auto-closes after ~800ms with a toast)
- [ ] **Quick action group next to Send** — three buttons sharing one row:
  - **Send & mark as meeting booked** — also moves lead `replied` → `meeting`
  - **Send & mark as lost** — also moves lead → `lost`
  - **Send only** — default, status stays `replied`
- [ ] Send-only does not auto-advance status; intent inference is deferred to US-023 (AI drafts)
- [ ] Discard button clears the composer after a confirm modal; does not delete the original reply
- [ ] Keyboard focus lands in the body field on drawer open
- [ ] Loading state — skeleton rows while `/inbox/<reply_id>.json` resolves
- [ ] Error state — thread fails to load → "Couldn't load this conversation" + retry button; send fails → inline banner under the composer with a short SMTP error, body preserved
- [ ] Unsaved-text guard — closing the drawer or navigating with text in the composer prompts "Discard your reply?"
- [ ] No new schema. `crm.emails.kind` already accepts free strings (migration 0001); add `'manual_reply'` to the documented list. `crm.replies.processed` already exists.

**Notes — Zoho specifics**
- Uses Zoho `.eu` endpoints (UK region), already configurable on Settings. No region detection — operator sets it once.
- Threading via `In-Reply-To` / `References` is honoured by Zoho for inbox grouping and Sent-folder placement, so the reply appears in the operator's Zoho Sent folder like a normal reply.

**Notes — voice**
- No copy linting on manual replies. Marketing-site copy rules apply to system-generated text (US-023 AI drafts), not to operator typing.

**Notes — explicitly out of scope**
- AI-drafted reply suggestions — US-023, deferred until volume justifies
- Forwarding to a colleague — single-operator agency, no recipient list yet
- Attachments — deferred until first asked for
- Multiple sending inboxes — cold-send warming infra is separate from the CRM reply path

---

### US-025 · Render the full conversation thread for a lead

**As** an operator
**I want to** see every outbound email and every received reply for a lead in a single chronological list
**So that** I can read the full back-and-forth in context without piecing the thread together from the lead detail page

**Priority:** P0
**Status:** Draft

**Acceptance criteria**
- [ ] `db.reply_thread(reply_id)` returns lead + client metadata plus a chronological message list mixing sent emails (`crm.emails` with `sent_at is not null` for that lead) with received replies (`crm.replies` for that lead)
- [ ] Each message carries: direction (`out` / `in`), subject, body, from / to address, ISO timestamp, relative time, sentiment (for `in` messages), and the cadence step pill (Day 1 / Day 3 / Day 7 — derived from `email_number`) for `out` messages
- [ ] Messages render oldest-first; the drawer scrolls to the most recent on open so the operator lands on the reply they just clicked
- [ ] Out and in messages are visually distinct — out uses the periwinkle tint, in uses surface-2
- [ ] Empty thread (no prior messages on file) renders a quiet "No prior messages on file" line, not an error
- [ ] Loading state — skeleton bubbles while the thread JSON resolves
- [ ] Error state — "Couldn't load this conversation" + Retry button; retry hits the same endpoint without re-opening the drawer
- [ ] Same component is reusable on `templates/lead_detail.html` so the lead page renders the thread without re-implementing the data layer

**Notes** — The drawer's thread (US-024) and any future "Conversation" panel on the lead detail page should pull from the same data layer function. Do not fork the query.

---

### US-026 · Pre-fill the reply composer with sender, recipient, subject, and quoted body

**As** an operator
**I want to** open the reply drawer and find From, To, Subject, and quoted body already populated from the conversation in front of me
**So that** I am typing the reply, not assembling it — the only thing that should need my attention is what to actually say

**Priority:** P0
**Status:** Draft

**Acceptance criteria**
- [ ] **From** — read-only, populated from `s.sending_email.from_address` (set on Settings → Sending email card)
- [ ] **To** — read-only, populated from the original reply's `from_address`, falling back to `lead.decision_maker_email` if the reply did not carry one
- [ ] **Subject** — editable, pre-filled with the original subject prefixed with `Re: ` if it does not already start with one (case-insensitive)
- [ ] **Body** — empty for typing at the top, with the lead's reply quoted as `> ` lines below the cursor
- [ ] Keyboard focus lands in the body field on drawer open, with the cursor at position 0,0 (above the quoted block)
- [ ] All four fields update if the operator opens a different reply without closing the drawer in between
- [ ] If `s.sending_email.from_address` is empty, the From field shows a `Set sending email →` link to Settings instead of an address; the send buttons are disabled with a tooltip "Sending email not configured"

**Notes** — The "from address not set" disabled state is the only place this story diverges from the happy path. We are not validating recipient addresses on the client — Zoho will bounce a malformed one and the bounce flow (US-007) handles it.

---

### US-027 · Send the manual reply via Zoho SMTP with thread-preserving headers

**As** an operator
**I want to** click Send and have the reply actually go out through Zoho, threaded into the same conversation on the recipient's side and visible in my Zoho Sent folder
**So that** the email lands like a normal reply — not as a new thread or a rogue message — and my Zoho web UI stays in sync with what I sent from the CRM

**Priority:** P0
**Status:** Draft

**Acceptance criteria**
- [ ] Send POSTs to `/inbox/reply/<reply_id>/send` with subject + body + optional `action`
- [ ] Server-side, the email goes out via SMTP using `s.sending_email.smtp_host` / `smtp_port` / authenticated as `from_address` (Zoho `.eu` endpoints by default for UK)
- [ ] Outgoing message sets `In-Reply-To: <original-message-id>` and `References` (chained from any prior `References` header in the thread, falling back to just the original Message-ID) so Zoho threads the reply on both inbox and Sent-folder views
- [ ] Outgoing message also sets a fresh `Message-ID` (UUID-based, sender's domain) so future replies thread back cleanly
- [ ] On success: a row is inserted into `crm.emails` with `kind='manual_reply'`, `status='sent'`, `sent_at=now()`, body, subject, from, to, lead_id, client_id; the originating `crm.replies.processed` is set true; an `crm.activity_log` row records `manual_reply_sent`
- [ ] On SMTP failure: typed body + subject are preserved client-side; an inline banner under the composer shows a short error ("Couldn't reach smtp.zoho.eu — check Settings") with a Retry button; nothing is written to `crm.emails`
- [ ] Send timeouts cap at 15s; longer than that, the banner says "Send timed out — your text is safe; try again"
- [ ] Sends are not rate-capped against the daily send cap (`s.daily_send_cap`) — a manual reply is a human-in-the-loop conversation, not bulk outbound, so US-004's cap does not apply

**Notes — Zoho specifics**
- UK account region so SMTP host defaults to `smtp.zoho.eu` port 587 (STARTTLS). The Settings page already exposes these — no detection logic needed.
- App-specific password lives in env (`ZOHO_SMTP_PASSWORD`), not in the DB. The Settings UI shows a "Configured / Not configured" pill — the password itself is never read or echoed back.

**Notes — explicitly out of scope**
- Switching to Zoho Mail API instead of SMTP — deferred. SMTP + headers covers ~95% of the threading behaviour; the API gives marginal benefits at higher OAuth setup cost. Revisit if read-after-send sync ever matters.

---

### US-028 · Quick-action send buttons advance lead status atomically

**As** an operator
**I want to** click "Send & mark as meeting booked" or "Send & mark as lost" and have the reply send and the lead status advance in one action
**So that** the morning queue clears at the speed of one click per lead, instead of three (send → open lead → change status)

**Priority:** P0
**Status:** Draft

**Acceptance criteria**
- [ ] Three buttons share one row at the bottom of the composer:
  - **Send reply** — primary, default action; lead stays at `replied`
  - **Send & mark as meeting booked** — secondary; advances `replied` → `meeting`
  - **Send & mark as lost** — secondary; advances `replied` → `lost`
- [ ] The `action` field (`meeting` / `lost` / unset) is sent on the same POST as body + subject — single round-trip, not send-then-status
- [ ] Lead status advance is atomic with the send: either both succeed and the row leaves Needs you, or neither happens and the typed text is preserved
- [ ] An `activity_log` row records the chosen action in its detail JSON (`status_change: 'meeting' | 'lost' | null`) so the audit trail shows whether the operator used a quick-action or default Send
- [ ] If the operator picks `meeting` or `lost`, the toast reads "Reply sent · marked meeting booked" / "Reply sent · marked lost" instead of plain "Reply sent"
- [ ] Picking `meeting` when the lead is already at `meeting` (rare — operator clicked from Done tab) is a no-op on status; the email still sends; no error
- [ ] No "Send & mark as won" — `won` requires a deal-close confirmation that does not belong in a reply composer; operator goes via the lead detail page (US-020 lifecycle)

**Notes** — Status auto-advance from a manual reply is a different signal than from an auto-detected reply (US-009 stops the cadence; it does not infer intent). Here the operator is the intent classifier — they read the reply, judged it, and clicked the right button. AI intent inference is deferred to US-023.

---

### US-029 · Manual reply cancels queued follow-ups for the lead

**As** an operator
**I want to** have any queued Day 3 / Day 7 follow-ups for this lead cancelled the moment my manual reply sends
**So that** the cadence engine does not fire a "just bumping this" follow-up after the lead and I are already mid-conversation

**Priority:** P0
**Status:** Draft

**Acceptance criteria**
- [ ] When `/inbox/reply/<id>/send` succeeds, all `crm.emails` rows for the same lead with `status='scheduled'` move to `status='cancelled'` with reason `manual_reply_sent`
- [ ] The cancelled rows disappear from the **Follow-ups this week** tab on Outreach
- [ ] An `activity_log` row records `sequence_stopped` with trigger `manual_reply_sent` (same shape as US-009's `reply_received` trigger, different reason)
- [ ] Cancellation is idempotent — re-sending a manual reply on the same lead does not error, even though there are no scheduled rows left to cancel
- [ ] A subsequent automated reply detection (US-008) on the same lead also does not double-cancel — US-009's existing trigger sees `status='cancelled'` and skips

**Notes** — Same end-state as US-009 (sequence stops when a reply is recorded), different trigger (manual send vs IMAP detection). Both write to the same `cancelled` status with a reason field that distinguishes them, so reporting can answer "how often does the operator stop a sequence vs auto-detection?"

---

## Epic 10 — Reports

Source UI: `templates/reports.html` — per-client performance page at `/reports`. Demoable today via a fixture (`db._reports_use_fixture()` returns True when `DATABASE_URL` is unset) — every section needs to read real data before this surface can be shared with a paying client. The fixture decisions also surface the schema gaps that block the production version: where per-lead `deal_value` lives and where per-client goals live.

The "What we did this period" narrative section was removed from the page on 2026-05-01 — clients reading their own report don't need a paragraph that paraphrases the numbers next to it. The `reports_narrative()` function still composes a recap paragraph that the **Email to client** mailto body uses; on the page itself, the KPI tiles + Wins card + chart + funnel + sequence already speak for themselves.

Stories below cover the gaps between the shipped UI and a real, sharable, single-client report.

### US-030 · Wins this period reads from `crm.leads`, not the demo fixture

**As** an operator
**I want to** open the Reports page for a real client and see the actual leads who reached `meeting` or `won` status in the period, not a hard-coded list
**So that** the report reflects work the agency actually did this month — the named outcomes are the part the client cares about most

**Priority:** P0
**Status:** Mostly shipped — query, fixture path, and 6-row cap landed. Two acceptance criteria still open: deal_value column (covered by US-031) and the conditional "demo data" tag. Production query was 500'ing on a psycopg `interval $1` binding bug until 2026-05-01 (commit 085b62f) — the SQL change in that fix is what made the production read path actually run, so re-verify in prod before closing.

**Acceptance criteria**
- [x] `db.reports_wins(client_id, days)` selects from `crm.leads` where `client_id = %(cid)s`, `status in ('meeting','won')`, and `updated_at >= now() - make_interval(days => %(d)s)` (note: `interval %(d)s` syntax does not bind in psycopg — Postgres only accepts a literal there; `make_interval(days => ...)` is the working pattern, see commit 085b62f)
- [x] Returns `decision_maker_name` as `contact`, `business_name` as `business`, the lead's status as `outcome`, and days since `updated_at` formatted by the existing `_format_days_ago()` helper as `when`
- [ ] `deal_value` is sourced from a real per-lead column where present, falling back to `null` on the row (display dash) — never the avg estimate (US-031 covers the column)
- [x] Capped at 6 most-recent rows so the card stays scannable
- [ ] "+ N more" hint appears below the 6 rows if the period contains more (links to a filtered Leads page view)
- [x] Empty state ("No wins yet in this window — sequence is still warming up") replaces the card silently when the query returns 0 rows
- [x] Fixture path is preserved for localhost demo: `_reports_use_fixture()` continues to return the existing fixture when `DATABASE_URL` is unset, so prospect-demo screenshots stay stable
- [ ] Card title's "demo data" tag only renders when `_reports_use_fixture()` is True; production renders the title alone (currently hardcoded in template — see `templates/reports.html` line 154)

**Notes** — Status-change date uses `crm.leads.updated_at` for now; a follow-up may move to a dedicated `crm.lead_status_history` table so a lead that flipped meeting → lost still surfaces the meeting date in the right window. Out of scope here — `updated_at` is right ~95% of the time and the schema migration is its own decision.

The same `interval %(d)s` binding bug existed in 6 sibling reports queries (`reports_kpis`, `reports_chart_series`, `reports_funnel`, `reports_sequence`, `reports_grade_mix`) — also fixed in 085b62f. Worth a separate "production read path covered by a smoke test that hits a real DB" story if this defect class repeats.

---

### US-031 · Per-lead deal value replaces the avg-deal estimate on Pipeline KPI

**As** a client receiving the report
**I want to** see the actual deal value for each won deal and a realistic per-meeting estimate for my account, not a flat agency-wide number
**So that** the £ figures on my report are mine — not "what the average UK B2B service deal looks like", which I have no reason to trust

**Priority:** P0
**Status:** Draft

**Acceptance criteria**
- [ ] `crm.leads` gains a nullable `deal_value` integer column (GBP, no decimals — pence don't matter at £k+ scale)
- [ ] `crm.clients` gains a nullable `default_deal_value` integer column for the per-meeting estimate (replaces hard-coded `REPORTS_DEFAULT_AVG_DEAL_VALUE = 6000`)
- [ ] `reports_kpis()` Pipeline value computes as: sum(`deal_value`) for `won` leads in window + (count of `meeting` leads in window × client's `default_deal_value`)
- [ ] If `default_deal_value` is unset on the client, Pipeline value still renders but the "est. £X per meeting" subline is replaced with a `Set deal value →` link to the client detail page
- [ ] Wins card shows the actual `deal_value` on `won` rows, dash on rows where it's unset (operator hasn't filled it yet)
- [ ] Lead detail page gains an editable `deal_value` field on the lifecycle card — only editable when status = `won`; greyed-out otherwise with tooltip "Set when the deal closes"
- [ ] Client detail page gains `default_deal_value` field on the targeting / commercial card
- [ ] Migration is idempotent (`add column if not exists`) and back-fills no values — operator fills them in as deals close

**Notes** — `deal_value` lives on the lead, not on a separate `crm.deals` table, because in our model a single lead can only become one deal (no upsells, no multi-product split — we sell retainers). If the model ever supports multi-deal accounts, promote to `crm.deals`. Until then, one column on `crm.leads` is the right complexity.

---

### US-032 · Click a win row to open the lead detail page

**As** an operator
**I want to** click a row on the Wins this period card and land on that lead's detail page
**So that** when a client asks "tell me more about Nina Patel at Harbor Legal", I'm one click away from the conversation history and notes — not searching the Leads table

**Priority:** P1
**Status:** Draft

**Acceptance criteria**
- [ ] Each `.wins-row` becomes an `<a>` (or wraps its content in one) pointing to `/leads/<lead_id>`
- [ ] Hover state matches the existing row-hover pattern on the Leads table (existing token, no new colour)
- [ ] Keyboard focus state visible (focus ring in `--accent`); Enter/Space activates
- [ ] Cursor is `pointer` on the whole row, not just the contact name
- [ ] In `@media print`, the link styling collapses — rows render as plain text so a printed report doesn't show underlines under names
- [ ] Fixture rows render as inert (no `<a>`) since fixture wins have no real `lead_id` — clicking them in demo would 404; keep the demo card clearly read-only

**Notes** — Lead detail page already exists per US-020. This story is the connective tissue between Reports and the operator's working surface. Acceptable trade-off: the report is read-only for the client (when a public share URL exists per US-035), so "click to drill in" only matters for the operator-private view.

---

### US-034 · Per-client goals replace global `REPORTS_TARGETS_MONTHLY`

**As** an operator
**I want to** set per-client targets (meetings/month, reply rate %) instead of using the agency-wide defaults
**So that** the **target met** / **of N target** indicators on the Reports KPIs reflect what each client actually pays for — Vidora's £3,500 retainer expects more meetings/month than a £1,500 audit-day-only client

**Priority:** P1
**Status:** Draft

**Acceptance criteria**
- [ ] `crm.clients` gains a nullable `goals` JSONB column. Shape: `{"meetings_per_month": 5, "reply_rate_pct": 8.0}`. Idempotent migration (`add column if not exists`)
- [ ] `reports_targets(days)` becomes `reports_targets(client_id, days)` — reads from `crm.clients.goals`, falls back to `REPORTS_TARGETS_MONTHLY` when the field is null or missing keys
- [ ] Client detail page gains an editable **Performance goals** card with two inputs: meetings/month (integer, 1–50), reply rate target % (number, 1–30, one decimal)
- [ ] Save persists to `crm.clients.goals`, no page reload, brief confirmation toast
- [ ] Reports page **Meetings booked** tile shows the client's per-month meetings target (pro-rated for the period as today: monthly × days/30)
- [ ] Reports page **Reply rate** tile uses the client's reply-rate target for the under/ok colour state
- [ ] When goals are unset, both tiles fall back to the existing global defaults (5 meetings/month, 8.0% reply rate) — no UI difference, no "set targets" nag (operator can set them when they want to)

**Notes** — JSONB rather than two columns because more goal fields are likely later (open rate target, deal-value target per period) and we don't want a migration per metric. Schema-on-read is acceptable here: one writer (the client edit form), one reader (`reports_targets`), both Python-typed, and the field count is small enough that a JSONB column doesn't hide the bugs it would in a multi-team codebase.

---

### US-035 · Sharable public report URL with single-password gate

**As** a client
**I want to** click a link in Sammy's email and see my own performance report in the browser, without logging into anything Innovite-internal
**So that** the report I get is the live page — not a screenshot or a stale CSV — and I can revisit it during the month without asking for an updated copy

**Priority:** P1
**Status:** Draft

**Acceptance criteria**
- [ ] New table `crm.report_share_tokens` (`client_id`, `token` text unique, `created_at`, `revoked_at` nullable). Token is a 32-char URL-safe random string
- [ ] New route `GET /report/<token>` renders the same `templates/reports.html` for the matched client, in read-only mode (no client picker, no edit affordances, no kebab menu — only the period switcher and Print)
- [ ] Public route requires the existing single-password gate (per project Open items in `crm/CLAUDE.md`) — same password as the operator-side CRM, set in env. Auth is a one-step form, session cookie scoped to `/report/*`
- [ ] Reports page (operator side) gains a **Share link** action in the kebab menu — generates a token if none exists for the client, copies `https://innovite-crm.onrender.com/report/<token>` to clipboard, shows a confirmation toast
- [ ] Same kebab menu gains **Revoke share link** when a token exists — sets `revoked_at`; future hits to that URL render a clean "Link revoked — request a new one from your account manager" page
- [ ] Mailto body (existing on the Reports page) updates to include the share URL alongside the recap text, when a non-revoked token exists
- [ ] Public route does NOT show: client picker, the **demo data** tag (production data only), the kebab menu, CSV export
- [ ] Public route DOES show: hero, KPIs, trend chart, Wins this period, Funnel, Sequence — same content shape as the operator view, just locked-down chrome

**Notes** — Single password + per-client token means the URL alone doesn't expose the report (need the password too) and the token alone scopes which client they see (one client can't see another's). Acceptable trade-off for a 1-person agency; full per-client auth (each client sets their own password, can self-revoke) is overkill at this scale and gets revisited if/when Innovite onboards a fifth client. Comment block at the top of the existing mailto code in `app.py:reports()` can be removed once this lands.

---

## Out of scope for this draft

These belong in later epics or separate docs — recording here so we don't lose them:

- **Auth** — POC has no auth (per `crm/CLAUDE.md` open items); single-password gate planned before public link-out (prereq for US-035).
- **`crm.inbound_leads` vs `public.leads` reconciliation** — affects Epic 4 acceptance; needs a schema decision before US-011 is scoped.
- **Background worker service on Render** — Epics 1, 2, 3 all assume a worker process. The decision on one-vs-two services should land before any of these are built.
- **Lead scoring rubric** — the schema already has `grade` (A/B/C/D/F) and `overall_score` columns, but the rubric for assigning them isn't specced yet. Three pillars sketched (Viability, Reachability, Buying signal) but the per-client weights need real conversion data to tune. Revisit after ~30 contacted leads have real reply data (smallest sample to start tuning weights). Until then the Leads page (US-021) treats grade as decoration — present if set, dash if null, never load-bearing for the op-state logic.
