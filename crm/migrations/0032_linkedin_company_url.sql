-- Innovite CRM — capture the LinkedIn /company/ URL discovered on the
-- target's own website.
--
-- Why a separate column from linkedin_url:
--   * linkedin_url is the personal-profile URL (the chosen officer's
--     /in/<slug> page). This drives decision-maker enrichment (their
--     title, recent posts, headline).
--   * linkedin_company_url is the firm's own /company/<slug> page.
--     Useful when no /in/ link is available (the dominant case for
--     UK accountancy / agency sites), and for follower count + recent
--     company posts that feed the 'growth_scaling' / 'recent post'
--     hook types in the drafter.
--
-- Both nullable. NULL means "not discovered on the website" — distinct
-- from "no LinkedIn presence" which we can't actually prove from
-- absence on the website alone.
--
-- Idempotent via IF NOT EXISTS.

begin;

alter table crm.leads
  add column if not exists linkedin_company_url text;

-- Extend the linkedin_status CHECK to allow 'verified_company' —
-- the status emitted when we parsed signals from the /company/ page
-- rather than a personal /in/ profile. Lets the operator tell at a
-- glance whether the follower count / recent-post fields refer to a
-- person or a company.
--
-- The original constraint in 0027 was anonymous (Postgres auto-named
-- it), so we look up the actual constraint name before dropping.
do $$
declare
  cname text;
begin
  select conname into cname
  from pg_constraint
  where conrelid = 'crm.leads'::regclass
    and pg_get_constraintdef(oid) like '%linkedin_status%'
  limit 1;
  if cname is not null then
    execute format('alter table crm.leads drop constraint %I', cname);
  end if;
end$$;

alter table crm.leads
  add constraint leads_linkedin_status_check
  check (linkedin_status in ('verified','verified_company','blocked','not_found','no_url')
         or linkedin_status is null);

commit;
