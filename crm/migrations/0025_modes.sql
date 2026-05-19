-- Innovite CRM — Mode (Accountancy vs Media) for clients, leads, searches
--
-- The vision audit engine (Claude Sonnet 4.6 scoring Instagram grids) is
-- the keeper IP from the Vidora rebuild. Everything else got rebuilt on
-- the Linux VPS — Google Places, public IG profile snapshot, no login.
--
-- This migration introduces the Mode abstraction so the CRM can hold
-- two pipelines under one roof:
--   - Accountancy Mode -> ROCA pipeline (Companies House + LinkedIn)
--   - Media Mode       -> Vidora pipeline (Google Places + IG audit)
--
-- A universal lead schema with mode-conditional fields. We denormalise
-- `mode` onto leads (instead of always joining through clients) so the
-- leads table query path stays one read. Clients don't change type in
-- practice, so drift risk is zero.
--
-- Why CHECK constraints rather than enums: matches the existing schema
-- convention and lets us add modes later without an ALTER TYPE dance.
--
-- Idempotent. Safe to re-run.

begin;

-- ─────────────────────────────────────────────────────────────────────
-- 1. crm.clients.client_type
-- ─────────────────────────────────────────────────────────────────────
alter table crm.clients
  add column if not exists client_type text not null default 'accountancy'
    check (client_type in ('accountancy','media'));

-- Backfill: Vidora Media is the only media client currently seeded.
update crm.clients
   set client_type = 'media'
 where name ilike 'vidora%'
   and client_type <> 'media';

-- ─────────────────────────────────────────────────────────────────────
-- 2. crm.leads.mode (denormalised from clients.client_type)
-- ─────────────────────────────────────────────────────────────────────
alter table crm.leads
  add column if not exists mode text not null default 'accountancy'
    check (mode in ('accountancy','media'));

-- Backfill: any lead whose client is media-type.
update crm.leads l
   set mode = c.client_type
  from crm.clients c
 where l.client_id = c.id
   and c.client_type = 'media'
   and l.mode <> 'media';

create index if not exists leads_mode_idx on crm.leads(mode);

-- ─────────────────────────────────────────────────────────────────────
-- 3. crm.leads.audit_version
-- ─────────────────────────────────────────────────────────────────────
-- Tracks which audit-engine version graded this lead. Lets us re-grade
-- a row when the audit logic changes without losing prior context.
alter table crm.leads
  add column if not exists audit_version text;

-- ─────────────────────────────────────────────────────────────────────
-- 4. Vidora vision-audit columns (media-mode-specific, all nullable)
-- ─────────────────────────────────────────────────────────────────────
-- Wide-table approach. With two modes and ~6 net-new columns, sidecar
-- tables would cost more in joins than they save in schema cleanliness.
-- Revisit if mode count hits 4+.
alter table crm.leads
  add column if not exists vidora_audit_grade text
    check (vidora_audit_grade in ('A','B','C','D','F') or vidora_audit_grade is null),
  add column if not exists vidora_audit_score integer,
  add column if not exists vidora_audit_weaknesses jsonb,
  add column if not exists vidora_sales_hook text,
  add column if not exists vidora_snapshot jsonb,
  add column if not exists vidora_audited_at timestamptz;

-- ─────────────────────────────────────────────────────────────────────
-- 5. crm.searches — search-run log (per-mode dispatch record)
-- ─────────────────────────────────────────────────────────────────────
-- Each row is one user-initiated search from the UI. The worker reads
-- pending rows, dispatches to the mode strategy, writes leads_found
-- and status back.
create table if not exists crm.searches (
  id            bigint generated always as identity primary key,
  client_id     bigint not null references crm.clients(id) on delete cascade,
  mode          text   not null check (mode in ('accountancy','media')),
  params        jsonb  not null default '{}'::jsonb,
  status        text   not null default 'pending'
                check (status in ('pending','running','complete','failed')),
  leads_found   integer,
  error         text,
  created_at    timestamptz not null default now(),
  started_at    timestamptz,
  completed_at  timestamptz
);

create index if not exists searches_client_idx on crm.searches(client_id);
create index if not exists searches_status_idx on crm.searches(status)
  where status in ('pending','running');

-- Match the rest of crm.*: RLS on, no policies. Service-role key bypasses.
alter table crm.searches enable row level security;

commit;
