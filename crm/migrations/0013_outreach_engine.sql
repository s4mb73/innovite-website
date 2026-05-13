-- Innovite CRM — Outreach Engine schema additions
-- Scaffold for Service 3 from backend-plan.md. Default mode is dry-run:
-- the engine writes everything it WOULD have done to crm.outreach_actions
-- without opening SMTP, so we can verify rotation + caps + gates against
-- the audit trail before flipping a single email out.
--
-- Idempotent. Safe to re-run.

begin;

-- ── mailboxes: jitter timestamp + (constraint widening covered below) ──
alter table crm.mailboxes
  add column if not exists last_send_at timestamptz;

-- ── emails: kind + cancel_reason + message_id (decision #11) ─────────
alter table crm.emails
  add column if not exists kind text not null default 'cadence';

alter table crm.emails
  add column if not exists cancel_reason text;

alter table crm.emails
  add column if not exists message_id text;

-- Widen the status check to include dry_run_ready + cancelled.
-- Postgres needs the existing constraint dropped first; the name is
-- 'emails_status_check' (default).
alter table crm.emails drop constraint if exists emails_status_check;
alter table crm.emails add constraint emails_status_check check (
  status in ('scheduled','sent','bounced','failed','cancelled','dry_run_ready')
);

create index if not exists emails_message_id_idx on crm.emails(message_id);

-- ── Outreach actions audit ──────────────────────────────────────────
-- Every engine tick that touched an email writes here. Even no-ops
-- ("would have sent but mailbox X had no capacity") are recorded so
-- the operator can debug why a cadence stalled.
create table if not exists crm.outreach_actions (
  id            bigint generated always as identity primary key,
  email_id      bigint references crm.emails(id) on delete set null,
  lead_id       bigint references crm.leads(id) on delete set null,
  client_id     bigint references crm.clients(id) on delete set null,
  mailbox_id    bigint references crm.mailboxes(id) on delete set null,

  action        text not null,   -- 'would_send' | 'sent' | 'skipped' | 'cancelled'
  reason        text,            -- on skip / cancel — see policy.py constants
  mode          text not null,   -- 'dry_run' | 'live'

  created_at    timestamptz not null default now()
);

create index if not exists outreach_actions_email_id_idx on crm.outreach_actions(email_id);
create index if not exists outreach_actions_lead_id_idx  on crm.outreach_actions(lead_id);
create index if not exists outreach_actions_created_idx  on crm.outreach_actions(created_at desc);

-- RLS
alter table crm.outreach_actions enable row level security;

commit;
