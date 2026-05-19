-- Innovite CRM — persist Companies House live status + match provenance.
--
-- Until now the CH enricher pulled `status` (active / dissolved /
-- liquidation / etc) into the in-memory biz dict but never wrote it
-- to crm.leads. That meant:
--   * The Gazette override ("CH says active → suppress distress flag")
--     was opaque to the operator — they couldn't see why the flag was
--     suppressed in the lead-detail UI.
--   * The Search page couldn't offer a "hide dissolved" filter when
--     the same company gets re-discovered from Google Maps weeks after
--     dissolution.
--   * No audit trail of whether the CH match came from the high-trust
--     footer scrape (Tier 1) or the fuzzy multi-signal path (Tier 2).
--
-- Two columns, both nullable. NULL means "no CH match attempted /
-- attempted and failed" — distinct from any concrete status value.
--
-- Idempotent via IF NOT EXISTS.

begin;

alter table crm.leads
  add column if not exists companies_house_status         text,
  add column if not exists companies_house_match_source   text;

-- Partial index on dissolved/liquidation statuses — supports the
-- Search page filter to hide companies we already know are dead.
-- Predicate covers only the rows we'd want to exclude, keeping the
-- index small.
create index if not exists idx_leads_ch_status_inactive
  on crm.leads (companies_house_status)
  where companies_house_status in ('dissolved', 'liquidation', 'receivership', 'administration');

commit;
