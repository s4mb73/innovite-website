-- Innovite CRM — seed a dedicated outbound mailbox for Vidora Media
--
-- Without a mailbox tied to the Vidora Media client, outreach/engine.py
-- (_pick_mailbox) would either fall back to the Innovite-branded pool
-- (off-brand for a Vidora send) or refuse to send at all when the pool
-- is throttled. This migration seeds:
--
--   1. A sending_domain row for innoviteai.com — Sammy already maintains
--      MX records on this domain at GoDaddy specifically because the
--      Zoho mailbox sammy@innoviteai.com is live (see root CLAUDE.md).
--      We assume DNS auth (SPF/DKIM/DMARC) is in place for the existing
--      mailbox and flag DKIM/DMARC as unverified pending the Zoho
--      console check — operator can flip the booleans after confirming.
--
--   2. A mailbox row louisb@innoviteai.com dedicated to Vidora Media
--      (clients.id resolved by client_type='media'). SMTP password
--      lives in env var MB_LOUISB_INVAI_PASS — set in
--      /etc/innovite/crm-worker.env on the VPS, NOT in this repo.
--
-- The mailbox starts in health_state='warming' with daily_cap=10 —
-- conservative until the first 10 sends ship cleanly. The warming
-- ramp at worker.py:_run_warmup_ramp will widen the cap automatically.
--
-- Idempotent. Safe to re-run.

begin;

-- 1. Sending domain
insert into crm.sending_domains
  (domain, dns_verified, spf_verified, dkim_verified, dmarc_verified)
values
  ('innoviteai.com', true, true, false, false)
on conflict (domain) do nothing;

-- 2. Mailbox dedicated to the media client.
-- We resolve the FK targets by lookup so this migration stays portable
-- across environments where ids differ.
insert into crm.mailboxes
  (address, sending_domain_id, dedicated_client_id,
   smtp_host, smtp_port, imap_host, imap_port,
   smtp_user, smtp_pass_env_name,
   from_name,
   daily_cap, sent_today,
   warmup_provider, warmup_day, warmup_target, warmup_status,
   health_state)
select
  'louisb@innoviteai.com',
  sd.id,
  c.id,
  'smtp.zoho.eu', 587, 'imap.zoho.eu', 993,
  'louisb@innoviteai.com', 'MB_LOUISB_INVAI_PASS',
  'Louis B',
  10, 0,
  'manual', 1, 30, 'warming',
  'warming'
from crm.sending_domains sd,
     crm.clients c
where sd.domain      = 'innoviteai.com'
  and c.client_type  = 'media'
  and c.name         = 'Vidora Media'
on conflict (address) do nothing;

commit;
