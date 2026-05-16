-- Innovite CRM — automatic mailbox warming ramp
--
-- Today `crm.mailboxes.daily_cap` is a static number the operator sets.
-- New mailboxes that jump straight from 0 → 50/day get throttled by
-- Gmail/Yahoo as suspicious — proper practice is a gradual ramp from
-- ~5/day up to the target over 14 days.
--
-- This migration adds:
--   - warming_started_at — timestamp the mailbox was added (or warming
--     was explicitly restarted by ops)
--   - target_daily_cap   — the operator's intended steady-state cap
--
-- The outreach engine's effective cap becomes:
--   min(target_daily_cap, 5 + 3 * days_since_warming_started)
--
-- After ~15 days, ramp == target, and the math collapses to the target.
-- Existing mailboxes get warming_started_at backfilled to created_at,
-- which means anything already-warm immediately sees effective_cap ==
-- target. New mailboxes (warming_started_at = now()) ramp from 5.
--
-- Idempotent. Safe to re-run.

begin;

alter table crm.mailboxes
  add column if not exists warming_started_at timestamptz,
  add column if not exists target_daily_cap   int;

-- Backfill: existing mailboxes are presumed warm. Set warming_started_at
-- far enough back that the ramp is fully unlocked, and copy daily_cap
-- into target_daily_cap so the effective-cap helper returns the same
-- number it would have before.
update crm.mailboxes
   set warming_started_at = coalesce(warming_started_at, created_at - interval '30 days'),
       target_daily_cap   = coalesce(target_daily_cap, daily_cap)
 where warming_started_at is null
    or target_daily_cap is null;

-- New mailboxes get a sane default — set the target to whatever
-- daily_cap was provided at insert; warming_started_at defaults to
-- insert time. Trigger keeps both in sync going forward.
create or replace function crm.mailbox_default_warming() returns trigger
language plpgsql as $$
begin
  if new.warming_started_at is null then
    new.warming_started_at := now();
  end if;
  if new.target_daily_cap is null then
    new.target_daily_cap := new.daily_cap;
  end if;
  return new;
end;
$$;

drop trigger if exists trg_mailbox_default_warming on crm.mailboxes;
create trigger trg_mailbox_default_warming
  before insert on crm.mailboxes
  for each row execute function crm.mailbox_default_warming();

commit;
