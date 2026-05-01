-- Innovite CRM — mailboxes as a first-class resource
-- Step 7 backbone. Replaces the single-mailbox model from 0005_settings.sql
-- (sending_email k/v stays as a "default SMTP for new mailboxes" hint;
-- the wizard reads it as form pre-fill — Slice 3).
--
-- Decisions:
--   - Multiple lookalike sending domains for deliverability (3-5 max,
--     each holding 3-5 mailboxes — one domain with 20 aliases is a death
--     sentence for inbox placement).
--   - Pool vs dedicated assignment via nullable dedicated_client_id.
--     NULL = pool (rotates across all clients). NOT NULL = dedicated to
--     that client (sends only for them, "from" is the client's identity).
--   - Daily cap is per-mailbox, set by the warmup stage. The send
--     scheduler picks healthy mailboxes with capacity to hit the
--     per-client target — no per-client cap needed at this layer.
--   - Warmup is third-party (Lemwarm / Instantly). We store provider /
--     day / target / status as fields. Manual entry for now; API pull
--     can land later. Building warmup in-house is a 3-week side quest
--     against mature tools — not worth it.
--   - SMTP password lives in env vars. smtp_pass_env_name points to
--     the env-var name. Keeps creds out of Postgres before the secrets
--     manager lands.
--
-- Idempotent on re-run.

begin;

create table if not exists crm.sending_domains (
  id              bigint generated always as identity primary key,
  domain          text        not null unique,
  dns_verified    boolean     not null default false,
  spf_verified    boolean     not null default false,
  dkim_verified   boolean     not null default false,
  dmarc_verified  boolean     not null default false,
  created_at      timestamptz not null default now()
);

create table if not exists crm.mailboxes (
  id                    bigint generated always as identity primary key,
  address               text        not null unique,
  sending_domain_id     bigint      not null references crm.sending_domains(id) on delete restrict,
  dedicated_client_id   bigint      references crm.clients(id) on delete set null,

  smtp_host             text        not null,
  smtp_port             int         not null,
  imap_host             text        not null,
  imap_port             int         not null,
  smtp_user             text        not null,
  smtp_pass_env_name    text        not null,

  from_name             text,
  signature_override    text,

  daily_cap             int         not null default 10,
  sent_today            int         not null default 0,

  warmup_provider       text,
  warmup_day            int,
  warmup_target         int,
  warmup_status         text        check (warmup_status is null or warmup_status in
                                           ('warming','complete','paused','not_started')),

  health_state          text        not null default 'unassigned'
                        check (health_state in
                               ('healthy','warming','throttled','paused','disconnected','unassigned')),
  last_error            text,
  last_error_at         timestamptz,
  paused                boolean     not null default false,

  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);

create index if not exists mailboxes_sending_domain_id_idx on crm.mailboxes(sending_domain_id);
create index if not exists mailboxes_dedicated_client_id_idx on crm.mailboxes(dedicated_client_id);
create index if not exists mailboxes_health_state_idx on crm.mailboxes(health_state);

-- RLS enabled, no policies — service-role bypasses, everyone else locked out.
alter table crm.sending_domains enable row level security;
alter table crm.mailboxes enable row level security;

-- ── Seed: 4 lookalike domains, 18 mailboxes ──────────────────────────
insert into crm.sending_domains (domain, dns_verified, spf_verified, dkim_verified, dmarc_verified) values
  ('innovite-mail.com',     true,  true,  true,  true),
  ('mail-innovite.co.uk',   true,  true,  true,  true),
  ('innovitegroup.com',     true,  true,  true,  false),
  ('get-innovite.com',      false, true,  false, false)
on conflict (domain) do nothing;

-- innovite-mail.com (5 mailboxes — fully warmed pool, plus one dedicated to ROCA)
insert into crm.mailboxes (address, sending_domain_id, smtp_host, smtp_port, imap_host, imap_port,
  smtp_user, smtp_pass_env_name, from_name, daily_cap, sent_today,
  warmup_provider, warmup_day, warmup_target, warmup_status, health_state)
select v.address, sd.id, 'smtp.zoho.eu', 587, 'imap.zoho.eu', 993,
       v.address, v.env_name, v.from_name, v.cap, v.sent,
       v.provider, v.wd, v.wt, v.wstatus, v.hstate
from crm.sending_domains sd
cross join (values
  ('sammy@innovite-mail.com', 'MB_SAMMY_INVMAIL_PASS', 'Sammy Bimpson',  40, 18, 'Lemwarm',   60, 50, 'complete', 'healthy'),
  ('louis@innovite-mail.com', 'MB_LOUIS_INVMAIL_PASS', 'Louis Hartley',  50, 42, 'Lemwarm',   60, 50, 'complete', 'healthy'),
  ('hello@innovite-mail.com', 'MB_HELLO_INVMAIL_PASS', 'Innovite Team',  35, 22, 'Lemwarm',   45, 50, 'warming',  'healthy'),
  ('team@innovite-mail.com',  'MB_TEAM_INVMAIL_PASS',  'Innovite Team',  12,  4, 'Instantly', 12, 50, 'warming',  'warming'),
  ('sales@innovite-mail.com', 'MB_SALES_INVMAIL_PASS', 'Innovite Sales', 30, 12, 'Lemwarm',   30, 50, 'warming',  'healthy')
) as v(address, env_name, from_name, cap, sent, provider, wd, wt, wstatus, hstate)
where sd.domain = 'innovite-mail.com'
on conflict (address) do nothing;

-- mail-innovite.co.uk (5 mailboxes — mixed warmup)
insert into crm.mailboxes (address, sending_domain_id, smtp_host, smtp_port, imap_host, imap_port,
  smtp_user, smtp_pass_env_name, from_name, daily_cap, sent_today,
  warmup_provider, warmup_day, warmup_target, warmup_status, health_state)
select v.address, sd.id, 'smtp.zoho.eu', 587, 'imap.zoho.eu', 993,
       v.address, v.env_name, v.from_name, v.cap, v.sent,
       v.provider, v.wd, v.wt, v.wstatus, v.hstate
from crm.sending_domains sd
cross join (values
  ('sammy@mail-innovite.co.uk',  'MB_SAMMY_MAILINV_PASS',  'Sammy Bimpson',     35, 14, 'Lemwarm',   50, 50, 'complete', 'healthy'),
  ('louis@mail-innovite.co.uk',  'MB_LOUIS_MAILINV_PASS',  'Louis Hartley',     10,  3, 'Instantly',  9, 50, 'warming',  'warming'),
  ('hello@mail-innovite.co.uk',  'MB_HELLO_MAILINV_PASS',  'Innovite Team',     30, 19, 'Lemwarm',   40, 50, 'warming',  'healthy'),
  ('team@mail-innovite.co.uk',   'MB_TEAM_MAILINV_PASS',   'Innovite Team',      8,  0, 'Instantly',  5, 50, 'warming',  'warming'),
  ('growth@mail-innovite.co.uk', 'MB_GROWTH_MAILINV_PASS', 'Innovite Growth',   30, 11, 'Lemwarm',   35, 50, 'warming',  'healthy')
) as v(address, env_name, from_name, cap, sent, provider, wd, wt, wstatus, hstate)
where sd.domain = 'mail-innovite.co.uk'
on conflict (address) do nothing;

-- innovitegroup.com (5 mailboxes — DMARC missing, one throttled, one paused, one dedicated to Vidora)
insert into crm.mailboxes (address, sending_domain_id, smtp_host, smtp_port, imap_host, imap_port,
  smtp_user, smtp_pass_env_name, from_name, daily_cap, sent_today,
  warmup_provider, warmup_day, warmup_target, warmup_status, health_state, last_error, last_error_at, paused)
select v.address, sd.id, 'smtp.zoho.eu', 587, 'imap.zoho.eu', 993,
       v.address, v.env_name, v.from_name, v.cap, v.sent,
       v.provider, v.wd, v.wt, v.wstatus, v.hstate, v.err,
       case when v.err is null then null else now() - interval '2 hours' end,
       v.is_paused
from crm.sending_domains sd
cross join (values
  ('sammy@innovitegroup.com',   'MB_SAMMY_INVGRP_PASS',   'Sammy Bimpson',  25,  8, 'Lemwarm', 28, 50, 'warming',  'healthy',     null,                                  false),
  ('hello@innovitegroup.com',   'MB_HELLO_INVGRP_PASS',   'Innovite Team',  25,  6, 'Lemwarm', 28, 50, 'warming',  'healthy',     null,                                  false),
  ('team@innovitegroup.com',    'MB_TEAM_INVGRP_PASS',    'Innovite Team',  25,  0, 'Lemwarm', 35, 50, 'complete', 'throttled',   'Bounce rate 4.2% — auto-paused for 24h', false),
  ('sales@innovitegroup.com',   'MB_SALES_INVGRP_PASS',   'Innovite Sales',  0,  0, null,    null,null, null,      'paused',      null,                                  true),
  ('founder@innovitegroup.com', 'MB_FOUNDER_INVGRP_PASS', 'Sammy Bimpson',  50, 38, 'Lemwarm', 50, 50, 'complete', 'healthy',     null,                                  false)
) as v(address, env_name, from_name, cap, sent, provider, wd, wt, wstatus, hstate, err, is_paused)
where sd.domain = 'innovitegroup.com'
on conflict (address) do nothing;

-- get-innovite.com (3 mailboxes — DKIM + DMARC unverified)
insert into crm.mailboxes (address, sending_domain_id, smtp_host, smtp_port, imap_host, imap_port,
  smtp_user, smtp_pass_env_name, from_name, daily_cap, sent_today,
  warmup_provider, warmup_day, warmup_target, warmup_status, health_state)
select v.address, sd.id, 'smtp.zoho.eu', 587, 'imap.zoho.eu', 993,
       v.address, v.env_name, v.from_name, v.cap, v.sent,
       v.provider, v.wd, v.wt, v.wstatus, v.hstate
from crm.sending_domains sd
cross join (values
  ('sammy@get-innovite.com', 'MB_SAMMY_GETINV_PASS', 'Sammy Bimpson', 22, 9, 'Lemwarm',   22, 50, 'warming', 'healthy'),
  ('hello@get-innovite.com', 'MB_HELLO_GETINV_PASS', 'Innovite Team', 22, 7, 'Lemwarm',   22, 50, 'warming', 'healthy'),
  ('team@get-innovite.com',  'MB_TEAM_GETINV_PASS',  'Innovite Team',  8, 1, 'Instantly',  8, 50, 'warming', 'warming')
) as v(address, env_name, from_name, cap, sent, provider, wd, wt, wstatus, hstate)
where sd.domain = 'get-innovite.com'
on conflict (address) do nothing;

-- Dedicated assignments (idempotent — re-runs just re-write the same value)
update crm.mailboxes
   set dedicated_client_id = (select id from crm.clients where name ilike 'ROCA%' limit 1)
 where address = 'louis@innovite-mail.com'
   and dedicated_client_id is distinct from (select id from crm.clients where name ilike 'ROCA%' limit 1);

update crm.mailboxes
   set dedicated_client_id = (select id from crm.clients where name ilike 'Vidora%' limit 1)
 where address = 'founder@innovitegroup.com'
   and dedicated_client_id is distinct from (select id from crm.clients where name ilike 'Vidora%' limit 1);

commit;
