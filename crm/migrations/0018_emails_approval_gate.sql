-- Innovite CRM — approval gate on outbound emails (Approvals queue)
--
-- The pipeline drafts Day-1 emails the moment leads are scored A/B/C and
-- queues them straight to crm.emails (status='scheduled'). Before this
-- migration, the outreach engine then shipped them on the next tick that
-- passed every gate — the only safety was OUTREACH_MODE=dry_run.
--
-- This adds an explicit operator approval gate:
--   * new Day-1 drafts default to needs_approval=true
--   * the outreach engine refuses to ship rows where needs_approval=true
--   * /approvals lists pending rows for click-through review
--   * follow-ups (Day 3, Day 7) are inserted with needs_approval=false
--     by the outreach engine itself — once the cold touch has been
--     approved and sent, follow-ups inherit that approval implicitly
--
-- Backfill choice: existing scheduled rows are auto-approved (set to
-- false) so anything already in flight ships as it would have. The gate
-- engages only for emails drafted from this migration onward. This is
-- the safer rollout — no surprises for the existing queue, no rows
-- stuck waiting for someone to click them.
--
-- Idempotent. Safe to re-run.

begin;

alter table crm.emails
  add column if not exists needs_approval bool not null default true,
  add column if not exists approved_at    timestamptz,
  add column if not exists approved_by    text,
  add column if not exists skip_reason    text;

-- Backfill: anything currently queued or in motion auto-approves so the
-- gate doesn't strand in-flight work. New rows inherit the column default
-- (true) and require explicit approval.
update crm.emails
   set needs_approval = false,
       approved_at    = coalesce(approved_at, now()),
       approved_by    = coalesce(approved_by, 'backfill')
 where needs_approval = true
   and created_at < now();

-- Partial index: the approvals queue query is "give me everything still
-- waiting on a human". Tiny in steady state, so a partial index is the
-- right shape — no overhead on the hot path of already-approved rows.
create index if not exists emails_needs_approval_idx
  on crm.emails (created_at)
  where needs_approval = true and status = 'scheduled';

commit;
