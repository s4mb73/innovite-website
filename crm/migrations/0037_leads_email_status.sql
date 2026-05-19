-- Innovite CRM — leads.email_status (recipient-lookup outcome)
--
-- Distinct from leads.email_verification_status (0033), which captures
-- *the verifier's verdict on an email we already found*. This column
-- captures the higher-level question: did we find one at all, and was
-- it good enough to queue an outreach send?
--
-- Values:
--   'found'      — recipient set on the row + email queued in crm.emails
--   'not_found'  — neither IG business_email nor website mailto: scrape
--                  produced a verifier-approved address. Operator gets
--                  a clean "needs manual lookup" filter.
--
-- Null = legacy rows (pre-Mode era, plus all accountancy leads that
-- predate this column). Accountancy enrichment doesn't write this
-- field today — only the Vidora drafter path uses it. Adding it as a
-- pure flag means existing accountancy rows aren't disturbed.
--
-- Idempotent. Safe to re-run.

begin;

alter table crm.leads
  add column if not exists email_status text
    check (email_status in ('found','not_found') or email_status is null);

-- Partial index on 'not_found' so the operator's "needs manual lookup"
-- filter is cheap. Tiny because most rows are null (accountancy) or
-- 'found' (verified successfully).
create index if not exists leads_email_status_notfound_idx
  on crm.leads (email_status)
  where email_status = 'not_found';

commit;
