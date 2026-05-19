# Vidora / Media pipeline audit — 2026-05-19

State of the Vidora end-to-end after one day of live testing. Two real
searches ran in prod (`crm.searches.id=1` and `id=2`). One column-name
bug in MediaMode is fixed and pushed but not yet pulled on the VPS.
Read this end-to-end before deciding what to fix next.

---

## TL;DR

The code mostly works. The funnel doesn't.

- **Code path is correct end-to-end** — discovery → IG handle → snapshot
  → audit → recipient resolve → upsert → draft → queue → outreach tick
  → SMTP send. Every stage executes, the right tables get written.
- **The bottleneck is data availability**, not code.
  - Clinic / dentist / aesthetic-clinic websites 403 our residential
    proxies (Cloudflare / Sucuri / generic WAF).
  - Instagram `business_email` field is empty for ~70% of UK SMBs.
  - Google Places sometimes returns URLs that 404 — homepage migration
    on Squarespace / Wix.
  - Result: ~20% of discovered candidates produce a contactable lead.
- **No real email has been sent yet.** Search 2 had two leads that
  would have shipped, but they hit the column-name bug on the final
  UPDATE and got stranded as graded-but-not-queued.

---

## What works (don't touch)

### Database layer
- `crm.searches` table dispatching media-mode runs via
  `worker._claim_next_search` (`crm/worker.py:100`)
- `crm.leads.mode` + `crm.leads.email_status` + `crm.leads.audit_version`
  populated correctly
- `crm.mailboxes` has the dedicated row for `louisb@innoviteai.com`
  pointed at the Vidora Media client (after 0038 lands on the VPS)
- Schema is on `0037` per the VPS pre-flight; `0038` is in source,
  ready to apply

### Pipeline modules
- `scraper/instagram_link.py:find_for_website` — homepage + 5 fallback
  paths via proxy pool; now returns `homepage_body` for reuse
- `scraper/instagram_snapshot.py:snapshot` — pulls IG's public
  `web_profile_info`; surfaces `business_email` and `public_email`
- `pipeline/vidora_audit.py:audit` — Sonnet 4.6 vision scoring with
  6-dimension weighted grading. Refuses partial grids (<9 images)
  correctly.
- `pipeline/modes/media.py:_low_signal_reason` — pre-filter on
  followers / posts / inactivity / private. Tested in prod: caught 3/10
  dormant dentists in search 2.
- `pipeline/modes/media.py:_resolve_recipient` — three-step lookup
  (IG email → cached homepage HTML → website scrape)
- `pipeline/modes/media.py:_media_recipient_gate` — local-only MX
  check via `email_verifier_local.precheck`. No Reoon dependency.
- `pipeline/vidora_drafter.py:draft_day1` — Haiku-pinned, raises
  `TemplatedFallbackError` rather than shipping the fallback marker
- `outreach/engine.py:tick` — accepts new `crm.emails` rows with
  `needs_approval=false`, hits SMTP, marks sent

### Infrastructure
- VPS healthy: worker running, both env vars present, mailbox seeded,
  proxy pool loading 50 proxies
- Sender domain `innoviteai.com` SPF verified (DKIM/DMARC still
  pending — see "data problems" below)

---

## What's broken (code, fixable)

### 1. Column name bug — FIXED in commit `e0377af`, not yet on VPS
- `pipeline/modes/media.py:380` was writing to
  `crm.leads.decision_maker_email`, which doesn't exist. Column is
  `email`. Two leads in search 2 (135 and 142) errored on the post-
  audit UPDATE.
- **Action**: `git pull` on VPS + `sudo systemctl restart crm-worker`.
- **Severity**: critical (blocks all Vidora email shipping).

### 2. Vidora mailbox FK — FIXED via migration 0038, not yet applied
- The `crm.mailboxes` row for `louisb@innoviteai.com` exists but
  `dedicated_client_id IS NULL` (migration 0036's `ON CONFLICT DO
  NOTHING` no-op'd because the row pre-existed). Without the FK,
  outreach falls back to Innovite-branded mailboxes for Vidora sends.
- **Action**: Apply `crm/migrations/0038_link_vidora_mailbox.sql` on
  VPS.
- **Severity**: high (brand-mismatch on send-from).

### 3. Audit image fetch bypasses proxy pool
- `pipeline/vidora_audit.py:189 _download_image` uses raw `urllib`
  to fetch IG CDN thumbnails. The proxy pool is bypassed.
- IG CDN rate-limits eventually. When that fires, the partial-grid
  guard refuses to score (correctly), and the audit silently returns
  None.
- **Action**: route image fetches through `scraper/client.fetch` like
  the rest of the pipeline.
- **Severity**: medium (will degrade over time, not immediately).

### 4. Anthropic model alias unpinned
- `pipeline/vidora_audit.py:42 ANTHROPIC_MODEL = "claude-sonnet-4-6"`
  — floats to whatever Anthropic ships. Audit grades aren't
  reproducible across model bumps.
- `pipeline/vidora_drafter.py:33` is pinned to a dated version
  correctly.
- **Action**: pin to a dated Sonnet 4.6 revision.
- **Severity**: low for first 100 leads, high once you're disputing
  grades with clients.

### 5. No retry on Anthropic vision call
- `pipeline/vidora_audit.py:219 _call_anthropic` returns None on
  first HTTP 5xx / timeout. No backoff, no second attempt. Transient
  Anthropic blip = lost audit.
- **Action**: 2-attempt retry with 2-second backoff.
- **Severity**: low (rare failure mode).

### 6. No per-candidate watchdog in MediaMode
- If a website hangs (e.g. `dermaskin.co.uk` in search 1, timing out
  on every proxy across 18 fetches), the search hangs for ~5 minutes
  on that one candidate. No upper bound on per-candidate time.
- **Action**: enforce a 60-second timeout per candidate.
- **Severity**: low (annoying, not blocking).

### 7. Search-history UI is missing
- After a search is queued, the only way to see progress is
  `select * from crm.searches`. No `/searches` page, no progress
  spinner, no funnel breakdown view.
- **Action**: add a Jinja page rendering `crm.searches` with status
  + funnel result.
- **Severity**: medium (UX hole, blocks operator confidence).

### 8. `leads.html` doesn't filter by `mode` or `email_status`
- Accountancy + media leads mix on the same page. No filter chip for
  the `email_status='not_found'` "needs manual lookup" filter we
  added at the schema level.
- **Action**: two URL params + two chips on the filter sidebar.
- **Severity**: low (URL params work as a workaround).

---

## What's broken (data, not code-fixable)

These are the real bottleneck.

### A. Cloudflare / WAF blocking on clinic + dental sites
- ~30-40% of UK clinic and dentist sites return 403 to residential
  proxies (visible in search 1 and 2 logs on
  `theacademyclinic.co.uk`, `thalia-aesthetics.com`, etc).
- No code fix will solve this. Options:
  - **Buy enriched leads** (Apollo, Cognism — ~$0.50-2 per verified
    contact). Bypass the scraper entirely for recipient resolution.
    Keep the audit for personalisation only.
  - **Add datacenter proxies for harder targets** (gets blocked
    differently — usually faster).
  - **Accept ~20% hit rate** and just run more searches.

### B. Instagram `business_email` rarely set
- The IG Professional dashboard field is empty for ~70% of UK SMBs
  in the testing so far. Even when set, the email is often a
  generic `info@` role address.
- No code fix. Just the data.

### C. Google Places returning stale URLs
- Several candidates in search 1 had URLs that 404'd
  (`www.forever22.co.uk`, `www.drandyanddrandrew.com`,
  `www.dermatransform.co.uk`). Business has migrated to a new
  domain but Google hasn't updated their listing.
- Optional code fix: a `_resolve_real_homepage()` step that follows
  redirects + tries `domain.com` (no `www`) variants. Low ROI.

### D. DKIM / DMARC not yet verified on `innoviteai.com`
- Migration 0036 marked DKIM and DMARC as `false` pending Zoho
  console check. Real sends will land in spam folders until these
  are aligned. SMTP "sent" status will lie.
- **Action**: Zoho admin → verify DKIM + add DMARC TXT record →
  flip the booleans in `crm.sending_domains`.
- **Severity**: critical for inbox placement; doesn't show up as
  a code error.

### E. Recipient-hit rate after the column fix
- Best estimate from search 1+2 data: ~20-30% of discovered
  candidates produce a `email_status='found'` lead. Not enough for
  high-volume cold outreach as a sole source.
- The audit + draft work is still valuable as a **layer on top of
  bought leads** — paste a name + email + website + IG handle into
  the system, get a graded prospect with image-grounded weaknesses
  and a personalised pitch.

---

## What's untested

- **Reply engine on Vidora leads** — `reply/engine.py` polls
  `louisb@innoviteai.com` IMAP, classifies sentiment. Never been
  exercised on a real Vidora reply. Untested for Vidora-pitch
  responses.
- **Day 3 / Day 7 follow-ups** — `outreach.engine._maybe_schedule_followup`
  uses `pipeline/drafter.py` (accountancy-flavoured). Vidora follow-
  ups will sound like ROCA follow-ups. Not necessarily wrong, but
  not Vidora-tuned.
- **Bounce handling on the Vidora mailbox** — never had a real bounce
  to test the reply engine's bounce classifier on.
- **Mailbox warmup ramp** — louisb starts at `daily_cap=10` with
  `health_state='warming'`. The ramp logic in
  `worker.py:_run_warmup_ramp` is shared with accountancy. Untested
  on this mailbox.
- **Inbound replies → CRM thread linking** — `crm.replies` table
  expects a `lead_id`. The reply engine matches by Message-ID. No
  Vidora threads exist yet.

---

## Priority-ordered fix list

### Now (to ship the first real email)

1. **Pull `e0377af` on VPS** + restart worker → fixes the column bug
2. **Apply migration 0038** on VPS → fixes the mailbox FK
3. **Verify DKIM/DMARC** on `innoviteai.com` at Zoho → fixes
   deliverability before any real volume
4. **Re-run search** (dentist / Manchester / limit 10) — should now
   produce 1-3 emails that actually send

### This week (to make the pipeline useful at scale)

5. **Decide on bought leads vs scraping yield.** If you want >50
   pitches/week, buy from Apollo or Cognism and let the
   audit/draft modules run on top of CSV imports.
6. **Pin the vision audit model** (#4 above)
7. **Add the search-history page** (#7 above) — operator confidence
8. **Mode filter on /leads** (#8 above)

### Eventually (polish)

9. Image fetch via proxy pool (#3 above)
10. Anthropic retry (#5 above)
11. Per-candidate watchdog (#6 above)
12. Optional `_resolve_real_homepage` step (data issue C)

---

## What this commit removes

Dead code, no behavior change:

- `crm/pipeline/sources/apollo.py` — replaced by
  `pipeline/sources/decision_maker.py`. Not imported anywhere; only
  stale comment references remain in `gazette.py` and the
  `decision_maker.py` docstring.
- `pipeline/vidora_drafter.py:_fallback()` — unreachable since the
  drafter now raises `TemplatedFallbackError` instead of returning
  the fallback dict. The `_FALLBACK_MARKER` constant stays — still
  used by the belt-and-braces marker check on Anthropic responses.

Nothing else is deleted. The Vidora pipeline stays — it works, the
funnel is the problem, not the code.

---

## Honest read

If the goal is "Vidora cold-emails 50+ clinics next week", buying
a list is faster than fixing this pipeline. The audit + drafter
still earn their keep — they produce personalised pitches no bought
list does. Treat them as the personalisation layer on top of bought
leads.

If the goal is "build a defensible IP that no agency can buy",
finish the scraper-side fixes and accept that recipient-resolution
hit rate will live in the 20-40% range. That's not a code problem
to solve — it's a structural feature of small-business UK web infra.

Stop blaming the pipeline. The bottleneck is data sourcing.
