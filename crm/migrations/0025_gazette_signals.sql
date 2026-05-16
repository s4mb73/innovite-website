-- Innovite CRM — Gazette distress-signal columns on crm.leads
--
-- The Gazette is the UK government's official journal of record for
-- insolvency events (winding-up petitions, administrator appointments,
-- creditors' voluntary liquidations, strike-off notices). For an
-- accountancy firm prospecting, a target with an active insolvency
-- notice is either gold (turnaround/insolvency specialist) or skip
-- (general practice, won't beat the appointed IP). Either way, surfacing
-- it on the lead row makes the operator's filtering decisions explicit
-- rather than buried.
--
-- We store the binary status + count + most-recent notice link rather
-- than persisting full notice text. The whole text lives at last_url
-- on thegazette.co.uk; we only need enough to grade and route the lead.
--
-- Idempotent. Safe to re-run.

begin;

alter table crm.leads
  add column if not exists gazette_status text
    check (gazette_status in ('clear','distressed','unknown')
           or gazette_status is null),
  add column if not exists gazette_notice_count     int,
  add column if not exists gazette_last_notice_date date,
  add column if not exists gazette_last_notice_url  text;

-- Partial index: 'distressed' is the actionable subset, ~5% of leads.
-- Lets the "show me leads with insolvency signals" query be a
-- bitmap-index scan rather than a seq scan over all leads.
create index if not exists leads_gazette_distressed_idx
  on crm.leads (gazette_last_notice_date desc)
  where gazette_status = 'distressed';

commit;
