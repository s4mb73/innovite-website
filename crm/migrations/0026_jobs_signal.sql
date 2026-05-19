-- Innovite CRM — open-jobs growth-signal columns on crm.leads
--
-- Stage 5 of the enrichment pipeline. Per-lead lookup against Reed.co.uk's
-- canonical /jobs/jobs-at-<slug> page extracts the open-jobs count.
-- Reed gracefully renders "0 Jobs At <X>" for unknown slugs, so the
-- signal value 'none' covers both "not hiring" and "not listed on Reed"
-- (functionally identical for outreach).
--
-- Classification:
--   0           → 'none'    (no hook)
--   1-4 jobs    → 'hiring'  (modest activity, soft hook)
--   5+ jobs     → 'scaling' (strong hook for advisory services)
--   query-fail  → 'unknown' (slug too short, fetch failed, etc.)
--
-- Source URL is stored so the operator can click through to the Reed
-- page for verification and to draft outreach referencing specific roles.
--
-- Idempotent. Safe to re-run.

begin;

alter table crm.leads
  add column if not exists jobs_signal text
    check (jobs_signal in ('none','hiring','scaling','unknown')
           or jobs_signal is null),
  add column if not exists jobs_open_count      int,
  add column if not exists jobs_last_checked_at timestamptz,
  add column if not exists jobs_source_url      text;

-- Partial index: 'scaling' is the actionable subset for an outreach
-- engine wanting to surface "growing businesses" — keeps that query
-- bitmap-indexed instead of a seq scan.
create index if not exists leads_jobs_scaling_idx
  on crm.leads (jobs_open_count desc)
  where jobs_signal = 'scaling';

commit;
