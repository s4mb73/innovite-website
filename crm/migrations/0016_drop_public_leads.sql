-- Innovite CRM — drop legacy public.leads table
--
-- Context (2026-05-13):
--   The marketing form at innovite.io posts via api/submit.js to the
--   submit_inbound_lead() RPC, which writes to crm.inbound_leads. An older
--   code path (now removed) wrote directly to public.leads; that table
--   still exists with 1 legacy row from before the RPC cutover.
--
-- This migration:
--   1. Copies any rows from public.leads into crm.inbound_leads, preserving
--      the original uuid in raw_data._legacy_uuid for provenance.
--   2. Drops public.leads and revokes prior grants on it.
--
-- Idempotent: re-running is safe. The copy is gated by a NOT EXISTS check
-- on _legacy_uuid in raw_data, and the drop uses IF EXISTS.
--
-- ⚠️  MANUAL REVIEW BEFORE EXECUTION
--   - Confirm crm.inbound_leads has matching rows after the copy step
--     (compare counts with the pre-existing public.leads row count).
--   - This is destructive for public.leads. There is no automatic rollback
--     once the DROP commits. Take a logical backup of the row first if you
--     want belt-and-braces (pg_dump --table=public.leads).

begin;

-- ── 1. Migrate any remaining rows ────────────────────────────────────
-- Column mapping:
--   public.leads.target_clients  ->  crm.inbound_leads.clients_wanted (renamed)
--   public.leads.id (uuid)       ->  raw_data._legacy_uuid (id types differ)
--   created_at preserved as-is.

insert into crm.inbound_leads (
  source, name, email, company, phone,
  industry, deal_value, current_method, clients_wanted,
  status, notes, raw_data, user_agent, ip, created_at
)
select
  pl.source, pl.name, pl.email, pl.company, pl.phone,
  pl.industry, pl.deal_value, pl.current_method, pl.target_clients,
  coalesce(nullif(pl.status, ''), 'new'),
  pl.notes,
  coalesce(pl.raw_data, '{}'::jsonb) || jsonb_build_object('_legacy_uuid', pl.id::text),
  pl.user_agent, pl.ip, pl.created_at
from public.leads pl
where not exists (
  select 1 from crm.inbound_leads il
  where il.raw_data ? '_legacy_uuid'
    and il.raw_data->>'_legacy_uuid' = pl.id::text
);

-- ── 2. Drop legacy table + revoke grants ─────────────────────────────
-- CASCADE in case anything still references it (none expected — the RPC
-- targets crm.inbound_leads).

revoke all on public.leads from anon, authenticated, service_role;
drop table if exists public.leads cascade;

commit;
