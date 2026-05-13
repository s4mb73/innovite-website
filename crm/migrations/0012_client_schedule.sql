-- Innovite CRM — per-client pipeline schedule (US-002)
-- Adds two columns on crm.clients:
--   daily_pipeline_run_at TIME    — Europe/London local time to run, e.g. 07:00.
--                                   NULL = no auto-run.
--   pipeline_paused       BOOLEAN — pause the schedule without losing the time.
--
-- Per backend-plan decision #5 we keep this as columns rather than a separate
-- crm.client_pipeline_schedules table until a client asks for more than one
-- run per day. The promotion to a table is then localized to this column +
-- the worker's tick function.

begin;

alter table crm.clients
  add column if not exists daily_pipeline_run_at time;

alter table crm.clients
  add column if not exists pipeline_paused boolean not null default false;

commit;
