-- Innovite CRM — Mode (Accountancy vs Media) for clients, leads, searches
--
-- The vision audit engine (Claude Sonnet 4.6 scoring Instagram grids) is
-- the keeper IP from the Vidora pivot. Discovery + snapshot got rebuilt
-- native on the Linux VPS — Google Places + public IG web_profile_info,
-- no login required (see 0034_vidora_bridge.sql).
--
-- This migration introduces the Mode abstraction so the CRM can hold
-- two pipelines under one roof:
--   - Accountancy Mode -> ROCA pipeline (Companies House + DNS +
--                         LinkedIn + Gazette + Reed + website scrape)
--   - Media Mode       -> Vidora pipeline (Google Places + IG audit)
--
-- A universal lead schema with mode-conditional fields. We denormalise
-- `mode` onto leads (instead of always joining through clients) so the
-- leads page query path stays one read. Clients don't change type in
-- practice, so drift risk is zero.
--
-- Per-dimension Vidora vision scores already live in leads.vidora_data
-- (jsonb, added by 0034) — no per-column promotion here. The universal
-- columns (grade, overall_score, weakness_profile, email_*) cover the
-- promoted summary fields for both modes.
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

-- Backfill: Vidora Media is the only seeded media client (0034 inserts it).
update crm.clients
   set client_type = 'media'
 where name ilike 'vidora%'
   and client_type <> 'media';

-- ─────────────────────────────────────────────────────────────────────
-- 2. crm.leads.mode (denormalised from clients.client_type)
-- ─────────────────────────────────────────────────────────────────────
-- Naming note: this is the "pipeline strategy" mode, distinct from
-- crm.pipeline_runs.mode which is the trigger-type mode
-- (manual/scheduled/onboarding/enrich_assigned). Two different axes,
-- two different scopes — overload accepted.
alter table crm.leads
  add column if not exists mode text not null default 'accountancy'
    check (mode in ('accountancy','media'));

-- Backfill by client type.
update crm.leads l
   set mode = c.client_type
  from crm.clients c
 where l.client_id = c.id
   and c.client_type = 'media'
   and l.mode <> 'media';

-- Belt-and-braces: any existing vidora_instagram-sourced lead is media.
update crm.leads
   set mode = 'media'
 where source = 'vidora_instagram'
   and mode <> 'media';

create index if not exists leads_mode_idx on crm.leads(mode);

-- ─────────────────────────────────────────────────────────────────────
-- 3. crm.leads.audit_version
-- ─────────────────────────────────────────────────────────────────────
-- Tracks which audit-engine version graded this row. Lets us re-grade
-- when the audit logic / model alias changes without losing prior
-- context. Top-level column (not in vidora_data) so the leads page
-- can filter on it cheaply.
alter table crm.leads
  add column if not exists audit_version text;

-- ─────────────────────────────────────────────────────────────────────
-- 4. crm.searches — search-run log (per-mode dispatch record)
-- ─────────────────────────────────────────────────────────────────────
-- Each row is one user-initiated search from the UI. The worker reads
-- pending rows, dispatches to the mode strategy, writes leads_found
-- + status back. Parallel to crm.pipeline_runs (which tracks recurring
-- enrichment jobs) — searches are one-shot, user-triggered.
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
