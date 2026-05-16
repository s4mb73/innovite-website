-- Innovite CRM — reply taxonomy v2
--
-- Two related upgrades to how the reply engine classifies inbound mail:
--
-- 1. Sentiment categories expand from 4 to 6.
--    'referral' is the highest-conversion inbound type — someone inside
--    the company is recommending you, so the cold-touch resistance is
--    gone. Today it gets bucketed as 'positive' or 'neutral' and triages
--    with the same urgency as everything else.
--    'wrong_person' is also recoverable pipeline — the right contact is
--    usually named in the reply, so we can re-pursue immediately instead
--    of marking the lead dead.
--
-- 2. Bounce category column. The DSN parser currently records severity
--    (hard/soft) but not category — so 'no-such-user', 'policy-block',
--    'content-block', and 'auth-fail' all look identical. Content-block
--    and auth-fail are infrastructure/template issues, NOT bad addresses,
--    so they should not auto-suppress. Adding a category column lets
--    the bounce engine make that distinction.
--
-- Idempotent. Safe to re-run.

begin;

-- ── crm.replies: expand sentiment values ────────────────────────────
-- Drop the auto-generated CHECK and re-add with the expanded set.
-- Existing rows pass cleanly (the old set is a subset of the new one).
alter table crm.replies drop constraint if exists replies_sentiment_check;
alter table crm.replies add constraint replies_sentiment_check
  check (sentiment in
    ('positive','negative','neutral','ooo','referral','wrong_person')
    or sentiment is null);

-- ── crm.bounces: add taxonomy column ────────────────────────────────
-- Six categories that map to distinct operator/engine actions:
--   no_such_user    — 5.1.x — address invalid → permanent suppress
--   mailbox_full    — 4.2.2 / 5.2.2 — recipient inbox full → retry then suppress
--   policy_block    — 5.7.1 — sender blocked at policy level → suppress + ops alert
--   content_block   — 5.7.x with content keywords → DON'T suppress, alert ops (template issue)
--   auth_fail       — 5.7.0 / 5.7.26 — SPF/DKIM failed → DON'T suppress, alert ops (infra bug)
--   greylist        — 4.x.x first attempt → retry naturally, no suppress
--   unknown         — fallback when SMTP code doesn't classify
alter table crm.bounces
  add column if not exists category text;

alter table crm.bounces drop constraint if exists bounces_category_check;
alter table crm.bounces add constraint bounces_category_check
  check (category in
    ('no_such_user','mailbox_full','policy_block','content_block',
     'auth_fail','greylist','unknown')
    or category is null);

create index if not exists bounces_category_idx
  on crm.bounces (category)
  where category is not null;

commit;
