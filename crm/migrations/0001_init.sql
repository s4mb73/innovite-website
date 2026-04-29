-- Innovite CRM — initial schema
-- Run in Supabase SQL Editor. Idempotent: safe to re-run.
--
-- After applying, go to Project Settings → API → Exposed schemas
-- and add `crm` to the list (alongside `public`) so supabase-py can reach it.

-- ─────────────────────────────────────────────────────────────────────
-- 1. Clean up the placeholder table that was created by mistake
-- ─────────────────────────────────────────────────────────────────────
drop table if exists public.crm;

-- ─────────────────────────────────────────────────────────────────────
-- 2. Schema
-- ─────────────────────────────────────────────────────────────────────
create schema if not exists crm;

-- ─────────────────────────────────────────────────────────────────────
-- 3. Updated-at trigger function (used by leads)
-- ─────────────────────────────────────────────────────────────────────
create or replace function crm.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

-- ─────────────────────────────────────────────────────────────────────
-- 4. Tables
-- ─────────────────────────────────────────────────────────────────────

-- Clients ─────────────────────────────────────────────
create table if not exists crm.clients (
  id                  bigint generated always as identity primary key,
  name                text not null unique,
  industry            text,
  contact_name        text,
  contact_email       text,
  target_industries   jsonb,
  target_locations    jsonb,
  target_min_reviews  integer,
  target_min_revenue  text,
  email_from_address  text,
  email_from_name     text,
  social_proof_line   text,
  pricing_tier        text,
  monthly_fee         numeric(10,2),
  status              text not null default 'active'
                      check (status in ('active','paused','churned')),
  onboarded_at        timestamptz,
  created_at          timestamptz not null default now()
);

-- Leads (deeply enriched outbound targets) ────────────
create table if not exists crm.leads (
  id                              bigint generated always as identity primary key,
  client_id                       bigint not null references crm.clients(id) on delete cascade,

  -- Business identity
  business_name                   text not null,
  address                         text,
  city                            text,
  phone                           text,
  email                           text,
  website                         text,

  -- Google data
  google_rating                   numeric(2,1),
  google_review_count             integer,
  google_maps_url                 text,

  -- Instagram
  instagram_handle                text,
  instagram_followers             integer,
  instagram_engagement_rate       numeric(5,2),
  instagram_posts_per_week        numeric(4,2),
  instagram_avg_likes             integer,
  instagram_last_post_date        date,

  -- Website
  website_score                   integer,
  website_ssl                     boolean,
  website_cta                     boolean,
  website_load_time_ms            integer,

  -- Companies House
  companies_house_number          text,
  companies_house_revenue_band    text,
  companies_house_sic_code        text,
  companies_house_incorporated    date,

  -- Decision maker
  linkedin_url                    text,
  decision_maker_name             text,
  decision_maker_title            text,

  -- Competitors
  competitor_1_name               text,
  competitor_1_reviews            integer,
  competitor_1_score              integer,
  competitor_2_name               text,
  competitor_2_reviews            integer,
  competitor_2_score              integer,
  competitor_3_name               text,
  competitor_3_reviews            integer,
  competitor_3_score              integer,

  -- Scoring
  competitor_rank                 integer,
  grade                           text check (grade in ('A','B','C','D','F') or grade is null),
  overall_score                   integer,
  hook_type                       text,
  weakness_profile                jsonb,
  revenue_gap_estimate            text,

  -- Email content (cached drafts before scheduling)
  email_subject                   text,
  email_body_day1                 text,
  email_body_day3                 text,
  email_body_day7                 text,
  pdf_path                        text,

  -- CRM state
  status                          text not null default 'new'
                                  check (status in ('new','contacted','replied','meeting','won','lost','closed')),
  source                          text not null default 'outbound'
                                  check (source in ('outbound','inbound','referral')),
  notes                           text,

  created_at                      timestamptz not null default now(),
  updated_at                      timestamptz not null default now()
);

create index if not exists leads_client_id_idx   on crm.leads(client_id);
create index if not exists leads_status_idx      on crm.leads(status);
create index if not exists leads_grade_idx       on crm.leads(grade);
create index if not exists leads_created_at_idx  on crm.leads(created_at desc);

drop trigger if exists leads_set_updated_at on crm.leads;
create trigger leads_set_updated_at
before update on crm.leads
for each row execute function crm.set_updated_at();

-- Emails (scheduled / sent outbound messages) ─────────
create table if not exists crm.emails (
  id            bigint generated always as identity primary key,
  lead_id       bigint not null references crm.leads(id)   on delete cascade,
  client_id     bigint not null references crm.clients(id) on delete cascade,
  email_number  integer not null check (email_number in (1,2,3)),
  subject       text not null,
  body          text not null,
  from_address  text,
  to_address    text,
  status        text not null default 'scheduled'
                check (status in ('scheduled','sent','bounced','failed')),
  scheduled_at  timestamptz,
  sent_at       timestamptz,
  opened_at     timestamptz,
  replied_at    timestamptz,
  created_at    timestamptz not null default now()
);

create index if not exists emails_lead_id_idx       on crm.emails(lead_id);
create index if not exists emails_client_id_idx     on crm.emails(client_id);
create index if not exists emails_status_idx        on crm.emails(status);
create index if not exists emails_scheduled_idx     on crm.emails(scheduled_at) where status = 'scheduled';

-- Replies (inbound responses to our outreach) ─────────
create table if not exists crm.replies (
  id            bigint generated always as identity primary key,
  lead_id       bigint not null references crm.leads(id)  on delete cascade,
  email_id      bigint references crm.emails(id)          on delete set null,
  from_address  text,
  subject       text,
  body          text,
  sentiment     text check (sentiment in ('positive','negative','neutral','ooo') or sentiment is null),
  detected_at   timestamptz not null default now(),
  processed     boolean not null default false
);

create index if not exists replies_lead_id_idx     on crm.replies(lead_id);
create index if not exists replies_unprocessed_idx on crm.replies(processed) where processed = false;

-- Inbound leads (innovite.io website form submissions) ─
-- NOTE: overlaps with the existing public.leads table that
-- api/submit.js already writes to. Reconciliation deferred —
-- for now, the CRM Inbound page will read from public.leads.
-- This table exists to match the spec; safe to leave empty
-- until we decide where the source of truth should live.
create table if not exists crm.inbound_leads (
  id                      bigint generated always as identity primary key,
  name                    text not null,
  company                 text,
  email                   text not null,
  phone                   text,
  industry                text,
  deal_value              text,
  current_method          text,
  clients_wanted          text,
  score                   text check (score in ('hot','warm','cold') or score is null),
  status                  text not null default 'new'
                          check (status in ('new','contacted','called','proposal','won','lost')),
  auto_response_sent      boolean not null default false,
  auto_response_sent_at   timestamptz,
  notes                   text,
  created_at              timestamptz not null default now()
);

create index if not exists inbound_leads_status_idx     on crm.inbound_leads(status);
create index if not exists inbound_leads_score_idx      on crm.inbound_leads(score);
create index if not exists inbound_leads_created_at_idx on crm.inbound_leads(created_at desc);

-- Activity log (audit trail) ──────────────────────────
create table if not exists crm.activity_log (
  id          bigint generated always as identity primary key,
  client_id   bigint references crm.clients(id) on delete set null,
  lead_id     bigint references crm.leads(id)   on delete set null,
  action      text not null,
  detail      text,
  created_at  timestamptz not null default now()
);

create index if not exists activity_log_client_id_idx   on crm.activity_log(client_id);
create index if not exists activity_log_lead_id_idx     on crm.activity_log(lead_id);
create index if not exists activity_log_created_at_idx  on crm.activity_log(created_at desc);

-- ─────────────────────────────────────────────────────────────────────
-- 5. Row-level security
-- ─────────────────────────────────────────────────────────────────────
-- Lock everything down. The Flask backend uses the service_role key,
-- which bypasses RLS. Anon and authenticated keys can't touch crm.*
-- until we add explicit policies.

alter table crm.clients       enable row level security;
alter table crm.leads         enable row level security;
alter table crm.emails        enable row level security;
alter table crm.replies       enable row level security;
alter table crm.inbound_leads enable row level security;
alter table crm.activity_log  enable row level security;

-- ─────────────────────────────────────────────────────────────────────
-- 6. Seed clients (config fields left NULL — fill in via Clients UI)
-- ─────────────────────────────────────────────────────────────────────
insert into crm.clients (name, industry, status) values
  ('Vidora Media',     'Content production',     'active'),
  ('ROCA Accountants', 'Professional services',  'active')
on conflict (name) do nothing;
