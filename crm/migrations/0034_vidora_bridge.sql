-- Vidora bridge — accept Instagram-discovered leads from the Windows
-- runner via POST /api/vidora/lead.
--
-- The existing leads schema already covers most Vidora fields
-- (instagram_*, website_*, competitor_*, grade, weakness_profile,
-- pdf_path, email_body_day1/3/7). This migration only adds what's
-- missing for the bridge:
--   1. Extend the source check constraint to allow 'vidora_instagram'
--   2. Add external_id for idempotent upsert from Vidora's lead.id
--   3. Add vidora_data jsonb for the long-tail fields that don't map
--      to existing columns (6-dim content scores, sales_notes, etc.)
--   4. Ensure a "Vidora Media" client row exists so the endpoint has
--      a client_id to attribute leads to.
--
-- Idempotent: safe to re-run.

-- 1. Allow vidora_instagram as a lead source
alter table crm.leads drop constraint if exists leads_source_check;
alter table crm.leads add constraint leads_source_check
  check (source in ('outbound','inbound','referral','vidora_instagram'));

-- 2. External ID for upsert from Vidora's lead.id
alter table crm.leads add column if not exists external_id text;

create unique index if not exists leads_source_external_id_uidx
  on crm.leads(source, external_id)
  where external_id is not null;

-- 3. Long-tail Vidora payload (6-dim vision scores, sales_notes, etc.)
alter table crm.leads add column if not exists vidora_data jsonb;

-- 4. Ensure the Vidora Media client exists (the FK target for incoming leads)
insert into crm.clients (name, status, email_from_address, email_from_name)
values ('Vidora Media', 'active', 'louisb@innoviteai.com', 'Louis B')
on conflict (name) do nothing;
