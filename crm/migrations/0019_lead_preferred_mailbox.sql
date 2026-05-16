-- Innovite CRM — per-recipient sender stickiness
--
-- Email clients group messages into a single thread by (sender, subject).
-- If Day 1 ships from mailbox A to lead X and Day 3 ships from mailbox B,
-- the recipient sees two unrelated cold emails from two different people
-- instead of one continuous follow-up — and any reply they send to
-- mailbox A's address doesn't match the lead's expected thread on B.
--
-- preferred_mailbox_id locks the lead to whichever mailbox sent Day 1.
-- Outreach engine sets it on the first successful send and never
-- overwrites it; _pick_mailbox honours it before falling back to the
-- dedicated/pool selection. nullable + on-delete-set-null so deleting
-- a mailbox doesn't cascade-delete leads.
--
-- Idempotent. Safe to re-run.

begin;

alter table crm.leads
  add column if not exists preferred_mailbox_id bigint
    references crm.mailboxes(id) on delete set null;

-- Partial index — the stickiness lookup is a single-row query per send.
-- Only indexing rows where it's set keeps the index tiny.
create index if not exists leads_preferred_mailbox_idx
  on crm.leads (preferred_mailbox_id)
  where preferred_mailbox_id is not null;

commit;
