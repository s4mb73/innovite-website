-- Innovite CRM — LinkedIn profile-verification columns on crm.leads
--
-- Stage 4 of the enrichment pipeline. Per-lead lookup of the public
-- /in/<slug> profile page extracts og:title + og:description meta
-- tags. Refreshes Apollo's decision-maker title (which ages weeks
-- to months as people change jobs) and acts as a liveness check
-- on the LinkedIn URL.
--
-- HTTP-only constraint means we can't get work history / activity /
-- mutual connections — those live behind LinkedIn's auth wall.
-- We get name, current headline, and a verified-vs-blocked flag.
--
-- Statuses:
--   verified   — profile fetched, og:title parsed, current_title fresh
--   blocked    — fetch failed (LinkedIn 999 or proxy reject) — common
--   not_found  — page returned but no og:title (deleted / redirected)
--   no_url     — lead has no usable /in/<slug> URL — skip silently
--
-- Idempotent. Safe to re-run.

begin;

alter table crm.leads
  add column if not exists linkedin_status text
    check (linkedin_status in ('verified','blocked','not_found','no_url')
           or linkedin_status is null),
  add column if not exists linkedin_current_title   text,
  add column if not exists linkedin_headline        text,
  add column if not exists linkedin_last_checked_at timestamptz;

commit;
