-- Innovite CRM — Leads page triage demo seed (NOT FOR PRODUCTION)
-- Idempotent. Safe to re-run.
--
-- Purpose: populate the redesigned Leads page (US-021) with a realistic
-- mix of triage states. Adds 16 leads (8 per client) with names distinct
-- from 0002 so this can run alongside or independently of that seed.
--
-- Spread of statuses + stage times so every op-state border colour
-- renders at least once and the needs-attention banner has content:
--
--   replied · fresh   → amber border, "needs response" CTA
--   replied · older   → amber border
--   meeting · booked  → green border (counts in needs-attention)
--   won · recent      → green dim, terminal positive
--   lost · stale      → grey dim, terminal
--   contacted · fresh → no border, in cadence
--   contacted · cadence-end → no border but old (pre auto-lost)
--   new · fresh       → grey
--
-- updated_at is explicitly set per lead so stage_time on the page
-- reflects "time in current status" rather than "time since created".
--
-- To wipe just this demo set:
--   delete from crm.leads where source = 'triage_demo';

begin;

do $$
declare
  v_vidora bigint;
  v_roca   bigint;
  v_now    timestamptz := now();
begin
  select id into v_vidora from crm.clients where name = 'Vidora Media';
  select id into v_roca   from crm.clients where name = 'ROCA Accountants';

  if v_vidora is null or v_roca is null then
    raise notice 'Seed clients not found — skipping triage demo. Run 0001_init.sql + 0002_demo_seed.sql first.';
    return;
  end if;

  if exists (select 1 from crm.leads where source = 'triage_demo') then
    raise notice 'Triage demo already seeded — skipping.';
    return;
  end if;

  -- ── ROCA targets (accountancy firms chasing local trades & SMEs) ──
  insert into crm.leads
    (client_id, business_name, city, phone, email, website,
     google_rating, google_review_count, decision_maker_name, decision_maker_title,
     overall_score, grade, status, source, created_at, updated_at)
  values
    (v_roca, 'Pinnacle Construction',  'Manchester', '0161 700 0301', 'james@pinnacleconst.co.uk', 'pinnacleconst.co.uk', 4.7, 42, 'James Whitfield',  'Managing Director', 88, 'A', 'replied',   'triage_demo', v_now - interval '5 days',   v_now - interval '4 hours'),
    (v_roca, 'Marston & Hayes',        'Liverpool',  '0151 700 0302', 'p.marston@marstonhayes.co.uk','marstonhayes.co.uk',4.6, 36, 'Peter Marston',    'Senior Partner',    84, 'A', 'replied',   'triage_demo', v_now - interval '8 days',   v_now - interval '1 day'),
    (v_roca, 'Greenfield Property',    'Leeds',      '0113 700 0303', 'sarah@greenfieldprop.co.uk','greenfieldprop.co.uk',4.5, 28, 'Sarah Greenfield', 'Owner',             82, 'A', 'meeting',   'triage_demo', v_now - interval '12 days',  v_now - interval '2 days'),
    (v_roca, 'Aldridge Architects',    'Manchester', '0161 700 0304', 'aldridge@aldridgearch.co.uk','aldridgearch.co.uk', 4.8, 51, 'Naomi Aldridge',   'Founder',           90, 'A', 'won',       'triage_demo', v_now - interval '21 days',  v_now - interval '5 days'),
    (v_roca, 'Northbridge Builders',   'Bolton',     '01204 700 305', 'office@northbridge.co.uk',  'northbridge.co.uk',  3.9, 9,  'Iain Crawford',    'Director',          62, 'C', 'lost',      'triage_demo', v_now - interval '24 days',  v_now - interval '8 days'),
    (v_roca, 'Pemberton Group',        'Manchester', '0161 700 0306', 'admin@pembertongroup.co.uk','pembertongroup.co.uk',4.4, 23, 'Hannah Pemberton', 'Managing Partner',  77, 'B', 'contacted', 'triage_demo', v_now - interval '2 days',   v_now - interval '14 hours'),
    (v_roca, 'Whitfield Surveyors',    'Leeds',      '0113 700 0307', 'team@whitfieldsurv.co.uk', 'whitfieldsurv.co.uk',4.6, 33, 'Owen Whitfield',   'Director',          81, 'A', 'contacted', 'triage_demo', v_now - interval '6 days',   v_now - interval '4 days'),
    (v_roca, 'Apex Holdings',          'Manchester', '0161 700 0308', null,                        'apexholdings.co.uk', 4.7, 38, 'Rachel Apex',      'Founder',           86, 'A', 'new',       'triage_demo', v_now - interval '3 hours',  v_now - interval '3 hours'),

    -- ── Vidora targets (content production agency chasing creative SMEs) ──
    (v_vidora, 'Sterling Studios',     'Manchester', '0161 700 0401', 'ed@sterlingstudios.co.uk', 'sterlingstudios.co.uk',4.7, 44, 'Ed Sterling',     'Founder',           88, 'A', 'replied',   'triage_demo', v_now - interval '4 days',   v_now - interval '6 hours'),
    (v_vidora, 'Foreshore Branding',   'Liverpool',  '0151 700 0402', 'asha@foreshorebrand.co.uk','foreshorebrand.co.uk',4.6, 32, 'Asha Foreshore',  'Founder',           83, 'A', 'meeting',   'triage_demo', v_now - interval '9 days',   v_now - interval '1 day'),
    (v_vidora, 'Kestrel Communications','Manchester','0161 700 0403', 'owen@kestrelcomms.co.uk', 'kestrelcomms.co.uk',  4.8, 47, 'Owen Kestrel',    'Director',          89, 'A', 'won',       'triage_demo', v_now - interval '18 days',  v_now - interval '3 days'),
    (v_vidora, 'Ridgeway Marketing',   'Leeds',      '0113 700 0404', 'office@ridgewaymkt.co.uk','ridgewaymkt.co.uk',  3.8, 6,  'Patrick Ridge',   'MD',                58, 'C', 'lost',      'triage_demo', v_now - interval '26 days',  v_now - interval '10 days'),
    (v_vidora, 'Lattice Creative',     'Manchester', '0161 700 0405', 'studio@latticecreative.co.uk','latticecreative.co.uk',4.5,21,'Greta Lattice','Senior Designer',   75, 'B', 'contacted', 'triage_demo', v_now - interval '4 days',   v_now - interval '2 days'),
    (v_vidora, 'Brightside Productions','Stockport', '0161 700 0406', 'enquiries@brightsidepro.co.uk','brightsidepro.co.uk',4.3,17,'Ella Brightside','Director',         71, 'B', 'contacted', 'triage_demo', v_now - interval '10 days',  v_now - interval '7 days'),
    (v_vidora, 'Aurora Films',         'Manchester', '0161 700 0407', null,                        'aurorafilms.co.uk',  4.7, 35, 'Lila Aurora',     'Founder',           85, 'A', 'new',       'triage_demo', v_now - interval '1 hour',   v_now - interval '1 hour'),
    (v_vidora, 'Midland Photography',  'Birmingham', '0121 700 0408', 'team@midlandphoto.co.uk',  'midlandphoto.co.uk', 4.4, 19, 'Theo Midland',    'Owner',             73, 'B', 'new',       'triage_demo', v_now - interval '1 day',    v_now - interval '1 day');

  -- ── Emails (one per non-new lead — the Day-1 send) ──────────────────
  insert into crm.emails (lead_id, client_id, email_number, subject, body, status, sent_at, replied_at)
  select
    l.id, l.client_id, 1,
    'A quick thought on ' || l.business_name,
    'Hi ' || coalesce(l.decision_maker_name, l.business_name) || ', noticed a few things about your setup that might be worth talking about.',
    'sent',
    l.created_at + interval '1 hour',
    case when l.status in ('replied','meeting','won') then l.updated_at else null end
  from crm.leads l
  where l.source = 'triage_demo' and l.status != 'new';

  -- ── Replies (for replied / meeting / won) ───────────────────────────
  insert into crm.replies (lead_id, from_address, subject, body, sentiment, detected_at)
  select
    l.id,
    coalesce(l.email, lower(replace(l.business_name, ' ', '')) || '@example.co.uk'),
    'Re: A quick thought on ' || l.business_name,
    case
      when l.status = 'won'     then 'Yes — great. Let''s get this moving. Diary?'
      when l.status = 'meeting' then 'Sure, happy to chat. What does your week look like?'
      else                           'Could you send a bit more info on what you had in mind?'
    end,
    case when l.status in ('meeting','won') then 'positive' else 'neutral' end,
    l.updated_at
  from crm.leads l
  where l.source = 'triage_demo' and l.status in ('replied','meeting','won');

  -- ── Activity log ────────────────────────────────────────────────────
  insert into crm.activity_log (client_id, lead_id, action, detail, created_at)
  select l.client_id, l.id, 'lead_created',
         l.business_name || ' — ' || l.city, l.created_at
  from crm.leads l where l.source = 'triage_demo';

  insert into crm.activity_log (client_id, lead_id, action, detail, created_at)
  select l.client_id, l.id, 'reply_received',
         l.business_name || ' (' || coalesce(r.sentiment, 'neutral') || ')',
         r.detected_at
  from crm.leads l join crm.replies r on r.lead_id = l.id
  where l.source = 'triage_demo';

  insert into crm.activity_log (client_id, lead_id, action, detail, created_at)
  select l.client_id, l.id, 'meeting_booked',
         l.business_name || ' — Thursday 2:30pm', l.updated_at + interval '1 hour'
  from crm.leads l
  where l.source = 'triage_demo' and l.status = 'meeting';

end$$;

commit;
