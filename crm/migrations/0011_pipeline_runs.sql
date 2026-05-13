-- Innovite CRM — pipeline_runs table (decision #6 from backend-plan.md)
-- Tracks every Find-new-leads execution: who triggered, what client, how
-- many leads found / skipped / errored, per-source progress for polling.
-- Idempotent. Safe to re-run.

begin;

create table if not exists crm.pipeline_runs (
  id              bigint generated always as identity primary key,
  client_id       bigint not null references crm.clients(id) on delete cascade,

  -- Lifecycle
  status          text not null default 'pending'
                  check (status in ('pending','running','succeeded','partial','failed','cancelled')),
  mode            text not null default 'manual'
                  check (mode in ('manual','scheduled','onboarding')),
  triggered_by    text,  -- operator id ('sammy') or 'cron' or 'system'

  -- Counts (filled by the worker)
  leads_added     integer not null default 0,
  leads_skipped   integer not null default 0,  -- duplicates / exclusion-list hits
  leads_errored   integer not null default 0,

  -- Per-source progress: { "google_places": "running", "companies_house": "queued", ... }
  -- Also carries the latest status message for the UI to render.
  progress        jsonb not null default '{}'::jsonb,
  error_msg       text,

  -- Timing
  started_at      timestamptz,
  finished_at     timestamptz,
  created_at      timestamptz not null default now()
);

create index if not exists pipeline_runs_client_id_idx
  on crm.pipeline_runs(client_id);
create index if not exists pipeline_runs_status_idx
  on crm.pipeline_runs(status);
create index if not exists pipeline_runs_created_at_idx
  on crm.pipeline_runs(created_at desc);

-- Partial index so the worker's "claim next pending run" query is a
-- single index scan, not a heap scan as the table grows.
create index if not exists pipeline_runs_pending_idx
  on crm.pipeline_runs(created_at)
  where status = 'pending';

alter table crm.pipeline_runs enable row level security;

commit;
