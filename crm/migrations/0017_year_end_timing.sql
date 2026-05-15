-- Innovite CRM — Companies House timing + pain signals
--
-- Adds the columns the new CH-derived signals write to. Each column maps
-- to a specific scoring lever (see crm/pipeline/scoring.py):
--
--   year_end_month, months_to_year_end        Item 1 — year-end timing
--   company_age_days                          Item 2 — new incorporation
--   recent_director_change, director_appointed_days_ago
--                                             Item 3 — director change
--   accounts_overdue, confirmation_overdue    Item 4 — overdue filings
--   pain_score                                Item 5 — sortable pain rollup
--
-- Idempotent — every ADD COLUMN uses IF NOT EXISTS.

begin;

alter table crm.leads
  add column if not exists companies_house_year_end_month        smallint;

alter table crm.leads
  add column if not exists companies_house_months_to_year_end    smallint;

alter table crm.leads
  add column if not exists companies_house_company_age_days      integer;

alter table crm.leads
  add column if not exists companies_house_recent_director_change boolean;

alter table crm.leads
  add column if not exists companies_house_director_appointed_days_ago integer;

alter table crm.leads
  add column if not exists companies_house_accounts_overdue       boolean;

alter table crm.leads
  add column if not exists companies_house_confirmation_overdue   boolean;

alter table crm.leads
  add column if not exists pain_score                             smallint;

-- Sort index for the Leads page "Sort · Pain score" option. Partial index
-- excludes nulls (the default unscored state) to keep it tight.
create index if not exists leads_pain_score_idx
  on crm.leads (pain_score desc, created_at desc)
  where pain_score is not null;

commit;
