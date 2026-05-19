-- Innovite CRM — DNS-derived tech-stack signals per lead.
--
-- Adds four signal columns populated by scraper/dns_signals.py:
--   email_provider — classified from MX records
--   website_host   — classified from PTR on the A record
--   dmarc_present  — TXT lookup on _dmarc.<domain>
--   spf_present    — TXT lookup on the apex
--
-- Why these columns:
--   * email_provider drives outreach copy ("noticed you're on M365…")
--     and feeds the digital-maturity tier on the leads list.
--   * website_host signals platform sophistication — Wix/Squarespace
--     leads are smaller-business; AWS/Cloudflare suggest dev resource.
--   * dmarc/spf are deliverability signals; absence is a concrete pain
--     point for any client offering email marketing or compliance work.
--
-- All four nullable — None means "couldn't query" not "doesn't exist".
-- Idempotent via IF NOT EXISTS.

begin;

alter table crm.leads
  add column if not exists email_provider text,
  add column if not exists website_host   text,
  add column if not exists dmarc_present  boolean,
  add column if not exists spf_present    boolean;

-- Partial index on email_provider — supports the future "filter by
-- provider" view on the leads page without bloating the index for the
-- common case of unenriched legacy rows.
create index if not exists idx_leads_email_provider
  on crm.leads (email_provider)
  where email_provider is not null;

commit;
