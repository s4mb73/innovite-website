-- Innovite CRM — website-scraped signals on crm.leads
--
-- The fourth enrichment source (after Google Places, Companies House,
-- Apollo). Fetches the lead's own homepage + about/services pages via
-- wreq + UK ISP proxies and extracts structured signals via Anthropic
-- Haiku. Output lands as JSONB so future fields don't require schema
-- changes.
--
-- Why JSONB rather than per-field columns: the parser will iterate.
-- Today we extract {summary, services, team_size_hint, recency_hint};
-- next month we might add {contact_form_present, pricing_visible,
-- recent_blog_count}. JSONB lets the scraper evolve without a
-- migration per signal.
--
-- Idempotent. Safe to re-run.

begin;

alter table crm.leads
  add column if not exists website_signals    jsonb,
  add column if not exists website_scraped_at timestamptz,
  add column if not exists website_scrape_status text
    check (website_scrape_status in
      ('ok','blocked','timeout','no_website','parse_failed','disabled')
      or website_scrape_status is null);

-- Partial index for the runner's "should we re-scrape this lead?" query.
-- Tiny because most rows are already-scraped or never-have-a-website.
create index if not exists leads_website_scrape_idx
  on crm.leads (website_scraped_at)
  where website_scraped_at is not null;

commit;
