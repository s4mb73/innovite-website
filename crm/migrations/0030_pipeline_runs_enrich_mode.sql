-- Innovite CRM — add 'enrich_assigned' to pipeline_runs.mode check.
--
-- The Search → Assign flow inserts lightweight lead rows from
-- discovery-only data, then enqueues a single pipeline_run row with
-- mode='enrich_assigned' and progress.lead_ids = [...]. The worker
-- picks it up and runs the full enrichment chain (CH + DNS + Apollo
-- + LinkedIn + website scrape + Reed + Gazette + scoring + draft)
-- per lead.
--
-- The existing CHECK constraint pinned mode to ('manual', 'scheduled',
-- 'onboarding'); adding the new value requires dropping + recreating
-- the constraint. Idempotent: skips when the new constraint is
-- already in place.

begin;

alter table crm.pipeline_runs
  drop constraint if exists pipeline_runs_mode_check;

alter table crm.pipeline_runs
  add constraint pipeline_runs_mode_check
  check (mode in ('manual', 'scheduled', 'onboarding', 'enrich_assigned'));

commit;
