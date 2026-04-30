-- Innovite CRM — Step 7 (Outreach) schema additions + demo seed
-- Idempotent. Safe to re-run. Two parts:
--
--   1. SCHEMA  Add outreach_paused on clients, bounce_reason on emails.
--              Both narrowly scoped:
--                - clients.status (active/paused/churned) is the lifecycle
--                  field for billing/relationship state.
--                - clients.outreach_paused is the operational kill-switch
--                  on the campaign — Sammy can pause sends without
--                  flagging the client as paused on their side.
--                - emails.bounce_reason mirrors what every ESP returns
--                  (Hard / Soft / Mailbox does not exist / Quota exceeded /
--                  Domain not found), so the Bounces list is actionable
--                  rather than just "something went wrong".
--
--   2. DEMO    Scheduled / bounced / opened emails so the Outreach page
--              has realistic content out of the box. Skips if scheduled
--              rows already exist.

begin;

-- ─────────────────────────────────────────────────────────────────────
-- 1. Schema additions
-- ─────────────────────────────────────────────────────────────────────
alter table crm.clients
  add column if not exists outreach_paused boolean not null default false;

alter table crm.emails
  add column if not exists bounce_reason text;

create index if not exists emails_bounced_idx
  on crm.emails (created_at desc) where status = 'bounced';

-- ─────────────────────────────────────────────────────────────────────
-- 2. Demo data
-- ─────────────────────────────────────────────────────────────────────
do $$
declare
  v_vidora bigint;
  v_roca   bigint;
  v_now    timestamptz := now();
begin
  select id into v_vidora from crm.clients where name = 'Vidora Media';
  select id into v_roca   from crm.clients where name = 'ROCA Accountants';

  if v_vidora is null or v_roca is null then
    raise notice '0003: seed clients not found — run 0001_init.sql + 0002 first.';
    return;
  end if;

  -- Vidora paused on outreach so the page demos both states.
  -- Idempotent: same value on re-run.
  update crm.clients set outreach_paused = true  where id = v_vidora;
  update crm.clients set outreach_paused = false where id = v_roca;

  -- Skip if outreach demo already seeded
  if exists (select 1 from crm.emails where status = 'scheduled' limit 1)
     or exists (select 1 from crm.emails where status = 'bounced' limit 1) then
    raise notice '0003: outreach demo already seeded — skipping inserts.';
    return;
  end if;

  -- ── Today tab — scheduled sends for today (UK time) ────────────────
  -- Pin to today's date in Europe/London, then schedule across the day.
  insert into crm.emails
    (lead_id, client_id, email_number, subject, body, status, scheduled_at, from_address, to_address)
  select
    l.id, l.client_id,
    1,
    'A quick thought on ' || l.business_name,
    'Hi ' || coalesce(l.decision_maker_name, l.business_name) || ', noticed a few things on the public-facing side that might be worth a quick chat.',
    'scheduled',
    -- 6 staggered slots: 09:00, 09:15, 10:00, 11:00, 14:00, 15:30 UK time
    (date_trunc('day', v_now at time zone 'Europe/London') + slot.t) at time zone 'Europe/London',
    'sammy@innoviteai.com',
    coalesce(l.email, lower(replace(l.business_name,' ','')) || '@example.co.uk')
  from (
    select id, client_id, business_name, decision_maker_name, email,
           row_number() over (order by created_at desc) as rn
    from crm.leads
    where status = 'new' and client_id = v_roca
    limit 6
  ) l
  join (values
    (1, interval '9 hours'),
    (2, interval '9 hours 15 minutes'),
    (3, interval '10 hours'),
    (4, interval '11 hours'),
    (5, interval '14 hours'),
    (6, interval '15 hours 30 minutes')
  ) as slot(rn, t) on slot.rn = l.rn;

  -- ── Follow-ups tab — Day 3 and Day 7 sends scheduled this week ─────
  -- For every "contacted" lead, schedule a Day 3 and (if older) Day 7.
  insert into crm.emails
    (lead_id, client_id, email_number, subject, body, status, scheduled_at, from_address, to_address)
  select
    l.id, l.client_id,
    2,
    'Re: A quick thought on ' || l.business_name,
    'Following up briefly — would a 15-minute call this week be useful, or shall I park this?',
    'scheduled',
    -- Spread Day-3 follow-ups across days +1 to +5, mid-morning UK time
    (date_trunc('day', v_now at time zone 'Europe/London')
       + ((row_number() over (order by l.id) % 5 + 1) || ' days')::interval
       + interval '10 hours'
    ) at time zone 'Europe/London',
    'sammy@innoviteai.com',
    coalesce(l.email, lower(replace(l.business_name,' ','')) || '@example.co.uk')
  from crm.leads l
  where l.status = 'contacted' and l.client_id = v_roca;

  insert into crm.emails
    (lead_id, client_id, email_number, subject, body, status, scheduled_at, from_address, to_address)
  select
    l.id, l.client_id,
    3,
    'Re: A quick thought on ' || l.business_name,
    'Last note from me — happy to leave it here unless this lands. Either way, no follow-ups after this.',
    'scheduled',
    -- Day-7 emails staggered across days +3 to +6, afternoons UK time
    (date_trunc('day', v_now at time zone 'Europe/London')
       + ((row_number() over (order by l.id) % 4 + 3) || ' days')::interval
       + interval '14 hours'
    ) at time zone 'Europe/London',
    'sammy@innoviteai.com',
    coalesce(l.email, lower(replace(l.business_name,' ','')) || '@example.co.uk')
  from crm.leads l
  where l.status = 'contacted' and l.client_id = v_roca
  -- Only the older "contacted" rows get a Day-7 scheduled (the more recent
  -- ones haven't earned a Day-7 yet; their Day-3 is still in flight).
  and l.created_at < v_now - interval '4 days';

  -- ── Sent tab — give some sent rows an opened_at so the table shows
  --    mixed states (sent · opened · replied) instead of all identical.
  update crm.emails e
     set opened_at = e.sent_at + interval '3 hours'
   where e.status = 'sent'
     and e.replied_at is null
     and (e.id % 3) = 0;

  -- ── Bounces tab — three bounces with realistic reasons ─────────────
  insert into crm.emails
    (lead_id, client_id, email_number, subject, body, status, sent_at, bounce_reason, from_address, to_address)
  select l.id, l.client_id, 1,
         'A quick thought on ' || l.business_name,
         'Hi — noticed a couple of gaps on the public-facing side worth a quick chat.',
         'bounced',
         v_now - interval '2 days',
         reason,
         'sammy@innoviteai.com',
         to_addr
  from (values
    ('Walsh & Pearson',         'Hard — mailbox does not exist',  'james.walsh@walshpearson.co.uk'),
    ('Highbury Tax Advisory',   'Soft — quota exceeded',          'sarah.bennett@highburytax.co.uk'),
    ('Pemberton Finance',       'Hard — domain not found',        'jack@pembertonfin.co.uk')
  ) as b(business, reason, to_addr)
  join crm.leads l on l.business_name = b.business
  limit 3;

  -- ── Activity log — pause + a couple of scheduled-batch entries ─────
  insert into crm.activity_log (client_id, lead_id, action, detail, created_at) values
    (v_vidora, null, 'outreach_paused',  'Vidora Media — outreach paused',                          v_now - interval '2 days'),
    (v_roca,   null, 'outreach_batch',   'ROCA Accountants — 6 sends scheduled for today',          v_now - interval '1 hour'),
    (v_roca,   null, 'outreach_batch',   'ROCA Accountants — 14 follow-ups queued this week',       v_now - interval '1 hour');

end$$;

commit;
