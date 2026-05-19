-- Innovite CRM — back-link louisb@innoviteai.com to the Vidora Media client
--
-- Migration 0036 seeded the mailbox row only if it didn't exist:
--
--   insert into crm.mailboxes (... dedicated_client_id ...) select ...
--   on conflict (address) do nothing;
--
-- In environments where the row was already present (e.g. seeded by a
-- prior migration or a manual operator step before 0036 was authored),
-- the INSERT no-ops and the dedicated_client_id FK never gets populated.
--
-- Symptom: outreach/engine.py:_pick_mailbox can't find a dedicated
-- mailbox for Vidora sends, falls back to the Innovite-branded pool —
-- Vidora pitches go out from sammy@innovite-mail.com instead of
-- louisb@innoviteai.com. Brand mismatch, not a hard failure, but the
-- whole point of 0036 was to keep Vidora sends on the Vidora domain.
--
-- Fix: an idempotent UPDATE that only touches the row when the FK is
-- still NULL. Re-running this migration is a no-op once the link is
-- in place.
--
-- Idempotent. Safe to re-run.

begin;

update crm.mailboxes m
   set dedicated_client_id = c.id
  from crm.clients c
 where m.address              = 'louisb@innoviteai.com'
   and c.name                 = 'Vidora Media'
   and c.client_type          = 'media'
   and m.dedicated_client_id is null;

commit;
