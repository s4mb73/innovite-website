-- Innovite CRM — Step 10 (Settings) k/v store
-- Single table for every configurable value: profile, email infra,
-- sending hours / cap, sequence cadence, global pause. Idempotent —
-- `if not exists` on the table + `on conflict do nothing` on seeds,
-- so re-runs leave existing values intact.
--
-- Why a k/v table (jsonb values) instead of a wide row:
--   - 1-person CRM, settings are sparse and grow ad-hoc
--   - jsonb keeps related fields together (sending_hours has 4 sub-fields)
--   - no migration needed to add a new setting — just `settings_set('foo', ...)`
--   - admin UI can iterate over a known list of keys
--
-- Wipe pattern (only if you want to reset to defaults):
--   delete from crm.settings;
--   then re-run this file.

begin;

create table if not exists crm.settings (
  key         text        primary key,
  value       jsonb       not null,
  updated_at  timestamptz not null default now()
);

create index if not exists settings_updated_at_idx on crm.settings(updated_at desc);

-- RLS enabled (no policies) — Flask service-role bypasses; everyone else is locked out.
alter table crm.settings enable row level security;

-- Seed defaults. ON CONFLICT keeps any user-edited values intact.
insert into crm.settings (key, value) values
  ('profile', '{
     "name":        "Sammy Bimpson",
     "email":       "sammy@innoviteai.com",
     "booking_url": "https://cal.com/sammy/innovite-strategy",
     "signature":   "— Sammy / Innovite"
   }'::jsonb),

  ('sending_email', '{
     "smtp_host":    "smtp.zoho.eu",
     "smtp_port":    587,
     "imap_host":    "imap.zoho.eu",
     "imap_port":    993,
     "from_address": "sammy@innoviteai.com"
   }'::jsonb),

  ('sending_hours', '{
     "start":         "09:00",
     "end":           "17:00",
     "tz":            "Europe/London",
     "skip_weekends": true
   }'::jsonb),

  ('daily_send_cap', '120'::jsonb),

  ('cadence', '{
     "day1_enabled":  true,
     "day3_enabled":  true,
     "day7_enabled":  true,
     "skip_weekends": true
   }'::jsonb),

  ('system_outreach_paused', 'false'::jsonb)

on conflict (key) do nothing;

commit;
