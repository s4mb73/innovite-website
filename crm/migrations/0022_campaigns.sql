-- Innovite CRM — Campaigns + saved templates
--
-- A Campaign is the entity that ties together (client, hook angle,
-- saved templates, outbound state). It replaces the abstract "Approvals
-- queue" concept with a first-class object that has a name, a target
-- audience (one client, one hook_type), pause/active state, and a
-- collection of templates that capture proven copy.
--
-- The template model is "AI generates, operator saves the best ones".
-- The drafter writes from scratch on Day 1 of a new campaign; once an
-- operator marks a draft "save as template", future drafts for the
-- same (client, hook_type) seed Anthropic with the saved template
-- rather than starting cold. The system learns the operator's voice
-- without forcing them to write templates upfront.
--
-- Idempotent. Safe to re-run.

begin;

-- ── crm.campaigns ────────────────────────────────────────────────────
create table if not exists crm.campaigns (
  id           bigint generated always as identity primary key,
  client_id    bigint not null references crm.clients(id) on delete cascade,
  name         text   not null,
  hook_type    text,                          -- null = catchall for this client
  status       text   not null default 'active'
               check (status in ('active','paused','archived')),
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

-- One campaign per (client, hook_type). Allows a default catchall
-- (hook_type IS NULL) plus per-hook campaigns alongside.
create unique index if not exists campaigns_client_hook_idx
  on crm.campaigns (client_id, coalesce(hook_type, ''))
  where status != 'archived';

create index if not exists campaigns_client_idx
  on crm.campaigns (client_id) where status = 'active';

-- ── crm.campaign_templates ───────────────────────────────────────────
-- Saved subject + body templates per (campaign, step). Operator can
-- save any approved draft as a template; the drafter then uses it as
-- the structural reference for future Day-N drafts in this campaign.
--
-- `is_default` picks which template the drafter seeds with by default
-- per step. Only one per (campaign, step) — enforced by partial unique.
-- `times_used` / `times_replied` track per-template performance so
-- /reports can rank templates by reply rate.
create table if not exists crm.campaign_templates (
  id               bigint generated always as identity primary key,
  campaign_id      bigint not null references crm.campaigns(id) on delete cascade,
  step             int    not null check (step in (1, 2, 3)),
  subject_template text   not null,
  body_template    text   not null,
  is_default       bool   not null default false,
  source_email_id  bigint references crm.emails(id) on delete set null,
  times_used       int    not null default 0,
  times_replied    int    not null default 0,
  created_at       timestamptz not null default now()
);

create unique index if not exists campaign_templates_default_idx
  on crm.campaign_templates (campaign_id, step)
  where is_default = true;

create index if not exists campaign_templates_campaign_step_idx
  on crm.campaign_templates (campaign_id, step);

-- ── crm.emails ← campaign_id link ────────────────────────────────────
alter table crm.emails
  add column if not exists campaign_id bigint
    references crm.campaigns(id) on delete set null;

create index if not exists emails_campaign_idx
  on crm.emails (campaign_id) where campaign_id is not null;

-- ── Backfill: a catchall campaign per existing client ────────────────
-- Existing emails get linked to the client's catchall so the Campaigns
-- page can list every historic send under its parent campaign card.
insert into crm.campaigns (client_id, name, hook_type, status)
select c.id, 'Default — all hooks', null, 'active'
  from crm.clients c
 where not exists (
   select 1 from crm.campaigns ec
    where ec.client_id = c.id
      and ec.hook_type is null
      and ec.status != 'archived'
 );

update crm.emails e
   set campaign_id = (
     select c.id from crm.campaigns c
      where c.client_id = e.client_id
        and c.hook_type is null
        and c.status != 'archived'
      limit 1
   )
 where campaign_id is null;

commit;
