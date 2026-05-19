-- Innovite CRM — persist email verification results from the
-- two-stage verifier (local pre-check + Reoon paid call).
--
-- Columns
--   email_verification_status
--     Final normalised verdict for the persisted email address.
--     Values:
--       syntax_invalid — fails RFC 5322-ish shape
--       no_mx          — domain has no MX or A record
--       disposable     — throwaway provider (mailinator etc.)
--       plausible      — passed local checks; Reoon disabled / errored
--       valid          — Reoon confirms mailbox accepts mail
--       invalid        — Reoon confirms mailbox rejects
--       catch_all      — domain accepts everything; can't prove mailbox
--       risky          — Reoon: role/disposable/dubious
--       unknown        — Reoon couldn't reach SMTP for a verdict
--
--   email_verification_confidence
--     high   — Reoon valid + score ≥ 90 + not catch-all + not role
--     medium — score 70-89 OR catch-all (mailbox unprovable)
--     low    — risky/unknown OR local-only 'plausible'
--     none   — syntax_invalid / no_mx / disposable / invalid
--
--   email_verification_source
--     'local_only'   — pre-check killed it, OR Reoon unavailable/errored
--     'reoon+local'  — both ran; final verdict from Reoon
--
--   email_verification_checked_at
--     UTC ts of when we ran the verifier. Used to decide when to re-
--     verify on subsequent runs (typical TTL ~30 days for cold lists).
--
-- All nullable. NULL means "never verified" (legacy leads, leads
-- where decision_maker.enrich didn't generate any candidates).
--
-- Idempotent via IF NOT EXISTS + named CHECK constraints we drop+re-add.

begin;

alter table crm.leads
  add column if not exists email_verification_status     text,
  add column if not exists email_verification_confidence text,
  add column if not exists email_verification_source     text,
  add column if not exists email_verification_checked_at timestamptz;

-- Status check. Drop any prior anonymous constraint first.
do $$
declare cname text;
begin
  select conname into cname
  from pg_constraint
  where conrelid = 'crm.leads'::regclass
    and pg_get_constraintdef(oid) like '%email_verification_status%'
  limit 1;
  if cname is not null then
    execute format('alter table crm.leads drop constraint %I', cname);
  end if;
end$$;

alter table crm.leads
  add constraint leads_email_verification_status_check
  check (email_verification_status in (
           'syntax_invalid','no_mx','disposable','plausible',
           'valid','invalid','catch_all','risky','unknown')
         or email_verification_status is null);

-- Confidence check.
do $$
declare cname text;
begin
  select conname into cname
  from pg_constraint
  where conrelid = 'crm.leads'::regclass
    and pg_get_constraintdef(oid) like '%email_verification_confidence%'
  limit 1;
  if cname is not null then
    execute format('alter table crm.leads drop constraint %I', cname);
  end if;
end$$;

alter table crm.leads
  add constraint leads_email_verification_confidence_check
  check (email_verification_confidence in ('high','medium','low','none')
         or email_verification_confidence is null);

-- Partial index on confidence — the leads page will want to filter
-- 'high-confidence only' when sender reputation is the priority.
create index if not exists idx_leads_email_verif_high
  on crm.leads (email_verification_confidence)
  where email_verification_confidence = 'high';

commit;
