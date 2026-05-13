-- Innovite CRM — suppressed addresses table (US-007)
-- Hard-suppression list applied across all clients before any send.
-- Sources:
--   - Hard bounces auto-add with reason='hard_bounce'
--   - Soft bounces add after 2 retries with reason='soft_bounce_x2'
--   - Operator-clicked Suppress on a bounce row with reason='manual'
--   - Inbound unsubscribe replies add with reason='unsubscribe'
--
-- One row per address. The address column is case-insensitive by
-- convention — engine lower-cases before lookup; this table stores
-- the original casing for audit but the unique index is on lower(address).
--
-- Idempotent. Safe to re-run.

begin;

create table if not exists crm.suppressed_addresses (
  id          bigint generated always as identity primary key,
  address     text   not null,
  reason      text   not null
              check (reason in ('hard_bounce','soft_bounce_x2','manual','unsubscribe','complaint')),
  added_by    text,        -- operator id or 'system'
  detail      text,        -- optional free text (e.g. specific bounce code)
  added_at    timestamptz not null default now()
);

create unique index if not exists suppressed_addresses_address_idx
  on crm.suppressed_addresses (lower(address));

create index if not exists suppressed_addresses_added_at_idx
  on crm.suppressed_addresses (added_at desc);

alter table crm.suppressed_addresses enable row level security;

commit;
