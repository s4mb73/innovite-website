-- Innovite CRM — Reply Engine schema additions
-- Service 4 from backend-plan.md. Adds the two pieces of persistent state
-- the reply poller needs: per-mailbox IMAP cursor and a bounces audit table.
--
-- Idempotent. Safe to re-run.

begin;

-- ── mailbox_state ────────────────────────────────────────────────────
-- The poller needs to remember the last IMAP UID it processed per mailbox
-- so exactly-once handling holds across worker restarts. A separate table
-- (not a column on mailboxes) because mailbox state is read+written every
-- 10 minutes by the reply engine while mailboxes itself is mostly admin-rate.

create table if not exists crm.mailbox_state (
  mailbox_id     bigint primary key references crm.mailboxes(id) on delete cascade,
  last_uid       bigint not null default 0,
  last_polled_at timestamptz,
  last_error     text,
  last_error_at  timestamptz
);

alter table crm.mailbox_state enable row level security;

-- ── bounces ──────────────────────────────────────────────────────────
-- One row per parsed DSN. Joined to the originating email when possible
-- via email_id; standalone otherwise (e.g. DSN for a hand-sent message).
-- severity 'hard' triggers auto-suppression; 'soft' retries up to 2 times
-- (retry policy lives in the outreach engine when soft bounces are wired).

create table if not exists crm.bounces (
  id              bigint generated always as identity primary key,
  email_id        bigint references crm.emails(id) on delete set null,
  lead_id         bigint references crm.leads(id)  on delete set null,
  mailbox_id      bigint references crm.mailboxes(id) on delete set null,

  bounced_address text   not null,
  severity        text   not null check (severity in ('hard','soft','unknown')),
  smtp_code       text,    -- e.g. '5.1.1' / '4.2.2' from the DSN Status field
  reason          text,    -- short human-readable summary
  raw             text,    -- the original DSN body (truncated to ~4KB)

  detected_at     timestamptz not null default now()
);

create index if not exists bounces_address_idx    on crm.bounces (lower(bounced_address));
create index if not exists bounces_severity_idx   on crm.bounces (severity);
create index if not exists bounces_detected_idx   on crm.bounces (detected_at desc);

alter table crm.bounces enable row level security;

commit;
