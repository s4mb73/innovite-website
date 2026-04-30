-- Innovite CRM — Step 7 (Client Onboarding) schema additions
-- Idempotent. Safe to re-run.
--
-- Adds the universal targeting filters captured on the new-client form
-- (US-017 in docs/user-stories.md). Stored as a single JSONB blob rather
-- than discrete columns because the shape is still evolving — when it
-- stabilises we can promote individual keys to columns + indexes; until
-- then JSONB keeps migrations cheap.
--
-- Schema:
--   {
--     "min_company_age_years": int|null,
--     "employee_bands":        ["1-10","11-50","51-200","200+"],
--     "active_filing_only":    bool,
--     "exclusion_list":        ["domain.tld" | "Company Ltd", ...]
--   }

begin;

alter table crm.clients
  add column if not exists targeting_filters jsonb not null default '{}'::jsonb;

comment on column crm.clients.targeting_filters is
  'Universal targeting filters from the new-client form (US-017). Keys: '
  'min_company_age_years (int), employee_bands (text[]), '
  'active_filing_only (bool), exclusion_list (text[]).';

commit;
