-- 0010_inbound_leads_form_fields.sql
-- Decision #1 from backend-plan.md: consolidate inbound form submissions
-- onto crm.inbound_leads, retiring public.leads as a write target.
--
-- This migration:
--   1. Adds the form-time fields (qualifier breakdown, raw payload, IP, UA)
--      that the Vercel api/submit.js currently writes to public.leads.
--   2. Extends the score check constraint to allow 'no-fit' so we can
--      preserve hard-disqualified submissions instead of dropping them.
--   3. Defines a SECURITY DEFINER RPC `public.submit_inbound_lead` so the
--      Vercel function can write across schemas via the Supabase REST
--      API without us having to expose the crm schema globally.
--
-- All idempotent — safe to re-run.

begin;

-- 1. Schema extensions for form-time fields ---------------------------
alter table crm.inbound_leads
  add column if not exists source           text,
  add column if not exists qualifier_q1     text,
  add column if not exists qualifier_q2     text,
  add column if not exists qualifier_q3     text,
  add column if not exists qualifier_q4     text,
  add column if not exists qualifier_score  integer,
  add column if not exists raw_data         jsonb,
  add column if not exists user_agent       text,
  add column if not exists ip               text;

-- 2. Allow 'no-fit' score (B2C / sub-2k deal value -> hard disqualifier) -
alter table crm.inbound_leads
  drop constraint if exists inbound_leads_score_check;
alter table crm.inbound_leads
  add constraint inbound_leads_score_check
    check (score = any (array['hot','warm','cold','no-fit']) or score is null);

-- 3. RPC for cross-schema inserts via Supabase REST -------------------
-- The Vercel form handler calls this with a single JSONB payload. We
-- whitelist the columns we accept, defaulting nullable ones, and return
-- the new lead id. SECURITY DEFINER lets us bypass RLS without exposing
-- the crm schema in the Supabase API config.

create or replace function public.submit_inbound_lead(payload jsonb)
returns bigint
language plpgsql
security definer
set search_path = public, crm
as $$
declare
  new_id bigint;
begin
  insert into crm.inbound_leads (
    source,
    name, email, company, phone,
    industry, deal_value, current_method, clients_wanted,
    qualifier_q1, qualifier_q2, qualifier_q3, qualifier_q4, qualifier_score,
    score, status,
    raw_data, user_agent, ip
  ) values (
    nullif(payload->>'source', ''),
    payload->>'name',
    lower(payload->>'email'),
    nullif(payload->>'company', ''),
    nullif(payload->>'phone', ''),
    nullif(payload->>'industry', ''),
    nullif(payload->>'deal_value', ''),
    nullif(payload->>'current_method', ''),
    nullif(payload->>'clients_wanted', ''),
    nullif(payload->>'qualifier_q1', ''),
    nullif(payload->>'qualifier_q2', ''),
    nullif(payload->>'qualifier_q3', ''),
    nullif(payload->>'qualifier_q4', ''),
    case when (payload->>'qualifier_score') ~ '^-?\d+$'
         then (payload->>'qualifier_score')::int else null end,
    nullif(payload->>'score', ''),
    coalesce(nullif(payload->>'status', ''), 'new'),
    case when jsonb_typeof(payload->'raw_data') = 'object'
         then payload->'raw_data' else null end,
    nullif(payload->>'user_agent', ''),
    nullif(payload->>'ip', '')
  )
  returning id into new_id;

  return new_id;
end;
$$;

-- Allow the service_role to call the function via the REST API
grant execute on function public.submit_inbound_lead(jsonb) to service_role;

commit;
