-- Innovite CRM — wipe demo / pre-launch data
--
-- DESTRUCTIVE. Run once, manually, before the first live campaign.
-- Not auto-applied — sits in migrations/ for the record but should
-- only be executed when the operator is deliberately resetting to a
-- clean state.
--
-- What this clears (operational data — none of it is meant to survive
-- the demo phase):
--   crm.outreach_actions    — engine audit trail
--   crm.bounces              — bounce history
--   crm.replies              — inbound reply rows
--   crm.emails               — every queued / sent / cancelled email
--   crm.pipeline_runs        — run history (counts useless without leads)
--   crm.activity_log         — audit trail for the above
--   crm.leads                — every scraped or demo prospect
--   crm.campaign_templates   — saved templates (none worth keeping yet)
--   crm.campaigns            — catchall campaigns auto-created in 0022
--   crm.mailbox_state        — IMAP cursors (forces full re-poll on
--                              the first REPLY_MODE=live tick)
--
-- What this preserves (deliberate — never wipe without the operator
-- saying so):
--   crm.clients              — Vidora, ROCA, anyone real. Operator
--                              edits/deletes manually if needed.
--   crm.mailboxes            — structure stays so Batch C can rewrite
--                              credentials via the Settings UI. Counter
--                              fields (sent_today, last_send_at) get
--                              zeroed below.
--   crm.suppressed_addresses — compliance record. Per UK GDPR Art. 21,
--                              we must prove we didn't re-contact opt-outs.
--                              Never wiped.
--   crm.settings             — profile, sending hours, cadence — all
--                              operator-curated.
--   crm.inbound_leads        — real form submissions from innovite.io.
--                              Some demo rows from migration 0004 also
--                              live here. Wipe manually if needed (see
--                              optional commands at the bottom).
--
-- Run order matters because of FK cascades. Going outer-in.

begin;

-- Engine audit + bounce + reply layers first.
delete from crm.outreach_actions;
delete from crm.bounces;
delete from crm.replies;

-- Email rows (FK target for replies and outreach_actions — must be
-- cleared after them since on-delete-set-null leaves orphan refs we
-- want gone). The on-delete-cascade from leads will also pick these
-- up but explicit is safer.
delete from crm.emails;

-- Campaign templates → campaigns (templates reference campaigns;
-- campaigns are referenced by emails via on-delete-set-null which
-- doesn't fire on cascade — handled above).
delete from crm.campaign_templates;
delete from crm.campaigns;

-- Pipeline-run history.
delete from crm.pipeline_runs;

-- Activity log.
delete from crm.activity_log;

-- Leads (the big one — emails cascaded above but be explicit).
delete from crm.leads;

-- Per-mailbox IMAP cursors — force the next live REPLY_MODE poll to
-- re-fetch everything from UID 1, since the engine's idea of "last
-- processed" is now meaningless against an empty replies table.
truncate crm.mailbox_state;

-- Zero send counters on mailboxes (preserve config + credentials).
update crm.mailboxes
   set sent_today    = 0,
       last_send_at  = null;

commit;

-- ─────────────────────────────────────────────────────────
-- OPTIONAL — operator runs manually if needed
-- ─────────────────────────────────────────────────────────
--
-- 1) Wipe ALL form submissions (if you're certain none are real):
--      delete from crm.inbound_leads;
--
-- 2) Wipe placeholder mailboxes (operator will re-add real ones via
--    Settings → Mailboxes once Batch C ships):
--      delete from crm.mailboxes;
--
-- 3) Wipe clients you don't want to keep (manual per-row, never blanket):
--      select id, name, status, monthly_fee from crm.clients;
--      -- then for each row you don't want:
--      delete from crm.clients where id = <id>;
--
-- 4) Reset Postgres sequences so new IDs start at 1 (cosmetic only —
--    fine to skip):
--      alter sequence crm.leads_id_seq           restart with 1;
--      alter sequence crm.emails_id_seq          restart with 1;
--      alter sequence crm.replies_id_seq         restart with 1;
--      alter sequence crm.bounces_id_seq         restart with 1;
--      alter sequence crm.campaigns_id_seq       restart with 1;
--      alter sequence crm.pipeline_runs_id_seq   restart with 1;
--      alter sequence crm.activity_log_id_seq    restart with 1;
--      alter sequence crm.outreach_actions_id_seq restart with 1;
