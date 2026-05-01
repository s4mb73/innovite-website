-- Innovite CRM — Reports page demo seed (NOT FOR PRODUCTION)
--
-- Purpose: make the Reports page (Step 9) show realistic numbers across
-- KPIs, funnel, sequence performance, and the Wins this period card. The
-- existing 0002 seed runs once and its `now() - interval` dates go stale
-- after a few weeks, so the report ends up reading 0 across every tile.
-- This migration adds a fresh, time-anchored set of leads + emails +
-- replies + activity that always falls inside the 7d / 30d / 90d windows
-- the report queries against.
--
-- Aligns the 'meeting' and 'won' rows with the localhost fixture names
-- (Sarah Cole, James Whitford, Nina Patel, Olivia Bennett, etc.) so the
-- prospect demo tells one coherent story across Inbox -> Leads -> Reports
-- regardless of whether DATABASE_URL is set.
--
-- Idempotent. Tagged via notes = 'REPORTS_DEMO_v1'. To re-seed with
-- fresh dates after a few weeks, run:
--
--   delete from crm.leads where notes = 'REPORTS_DEMO_v1';
--
-- then re-apply this migration. (Cascade drops the matching emails,
-- replies, and activity_log rows automatically.)

begin;

do $$
declare
  v_vidora bigint;
  v_roca   bigint;
begin
  select id into v_vidora from crm.clients where name = 'Vidora Media';
  select id into v_roca   from crm.clients where name = 'ROCA Accountants';

  if v_vidora is null or v_roca is null then
    raise exception 'Seed clients not found — run 0001_init.sql first.';
  end if;

  if exists (select 1 from crm.leads where notes = 'REPORTS_DEMO_v1') then
    raise notice 'Reports demo already seeded — skipping. To re-seed, run: delete from crm.leads where notes = ''REPORTS_DEMO_v1'';';
    return;
  end if;

  -- ── ROCA Accountants — 25 leads ──────────────────────────────────────
  -- meeting / won rows mirror the wins fixture names + ages so the Wins
  -- this period card reads identically to the localhost demo.
  insert into crm.leads
    (client_id, business_name, city, phone, email, website,
     google_rating, google_review_count, decision_maker_name, decision_maker_title,
     overall_score, grade, status, source, notes, created_at, updated_at)
  values
    -- Wins fixture (6 rows): meeting + won, dated to fall in 7d/30d/90d windows
    (v_roca, 'Harbor Legal',              'London',     '020 7946 0801', 'nina@harborlegal.co.uk',         'harborlegal.co.uk',         4.8, 47, 'Nina Patel',      'Senior Partner',    91, 'A', 'meeting', 'outbound', 'REPORTS_DEMO_v1', now() - interval '8 days',  now() - interval '3 days'),
    (v_roca, 'Cole & Reeves Accountants', 'Manchester', '0161 555 0801', 'sarah@coleandreeves.co.uk',      'coleandreeves.co.uk',       4.7, 35, 'Sarah Cole',      'Managing Partner',  88, 'A', 'meeting', 'outbound', 'REPORTS_DEMO_v1', now() - interval '12 days', now() - interval '5 days'),
    (v_roca, 'Marsh & Trent Audit',       'Liverpool',  '0151 555 0802', 'david@marshtrent.co.uk',         'marshtrent.co.uk',          4.5, 29, 'David Marsh',     'Director',          82, 'A', 'meeting', 'outbound', 'REPORTS_DEMO_v1', now() - interval '17 days', now() - interval '11 days'),
    (v_roca, 'Bennett Tax Group',         'Leeds',      '0113 555 0803', 'olivia@bennetttax.co.uk',        'bennetttax.co.uk',          4.6, 41, 'Olivia Bennett',  'Founder',           86, 'A', 'won',     'outbound', 'REPORTS_DEMO_v1', now() - interval '28 days', now() - interval '19 days'),
    (v_roca, 'Coastal Bookkeeping Ltd',   'Liverpool',  '0151 555 0804', 'tom@coastalbk.co.uk',            'coastalbk.co.uk',           4.3, 18, 'Tom Reeves',      'Owner',             74, 'B', 'meeting', 'outbound', 'REPORTS_DEMO_v1', now() - interval '41 days', now() - interval '34 days'),
    (v_roca, 'Hill & Daughter Audit',     'Manchester', '0161 555 0805', 'marcus@hilldaughter.co.uk',      'hilldaughter.co.uk',        4.4, 24, 'Marcus Hill',     'Senior Partner',    79, 'B', 'won',     'outbound', 'REPORTS_DEMO_v1', now() - interval '57 days', now() - interval '48 days'),

    -- Replied (recent — feeds reply rate KPI + funnel 'replied' bucket)
    (v_roca, 'Apex Financial Partners',   'Manchester', '0161 555 0806', 'charlotte@apexfp.co.uk',         'apexfp.co.uk',              4.8, 56, 'Charlotte Reed',  'Managing Director', 92, 'A', 'replied', 'outbound', 'REPORTS_DEMO_v1', now() - interval '2 days',  now() - interval '2 days'),
    (v_roca, 'Northwood Advisory',        'Manchester', '0161 555 0807', 'hannah@northwoodadv.co.uk',      'northwoodadv.co.uk',        4.7, 33, 'Hannah Whitcombe','Director',          85, 'A', 'replied', 'outbound', 'REPORTS_DEMO_v1', now() - interval '4 days',  now() - interval '4 days'),
    (v_roca, 'Sterling Partners LLP',     'Manchester', '0161 555 0808', 'ed@sterlingpartners.uk',         'sterlingpartners.uk',       4.7, 44, 'Ed Sterling',     'Senior Partner',    88, 'A', 'replied', 'outbound', 'REPORTS_DEMO_v1', now() - interval '6 days',  now() - interval '6 days'),

    -- Contacted (most of the funnel — Day 1 sent, awaiting reply)
    (v_roca, 'Walsh & Pearson',           'Manchester', '0161 555 0809', 'james@walshpearson.co.uk',       'walshpearson.co.uk',        4.7, 38, 'James Walsh',     'Managing Partner',  87, 'A', 'contacted', 'outbound', 'REPORTS_DEMO_v1', now() - interval '5 days',  now() - interval '5 days'),
    (v_roca, 'Northgate Financial',       'Leeds',      '0113 555 0810', 'robert@northgatefin.co.uk',      'northgatefin.co.uk',        4.1, 12, 'Robert Singh',    'Director',          68, 'B', 'contacted', 'outbound', 'REPORTS_DEMO_v1', now() - interval '7 days',  now() - interval '7 days'),
    (v_roca, 'Pearson & Doyle',           'Stockport',  '0161 555 0811', 'helen@pearsondoyle.co.uk',       'pearsondoyle.co.uk',        4.6, 29, 'Helen Doyle',     'Managing Partner',  79, 'B', 'contacted', 'outbound', 'REPORTS_DEMO_v1', now() - interval '9 days',  now() - interval '9 days'),
    (v_roca, 'Carlton Tax Group',         'Liverpool',  '0151 555 0812', 'daniel@carltontax.co.uk',        'carltontax.co.uk',          4.5, 28, 'Daniel Carlton',  'Partner',           80, 'B', 'contacted', 'outbound', 'REPORTS_DEMO_v1', now() - interval '11 days', now() - interval '11 days'),
    (v_roca, 'Eastgate Advisory',         'Manchester', '0161 555 0813', 'sophie@eastgateadv.co.uk',       'eastgateadv.co.uk',         4.4, 22, 'Sophie Hill',     'Director',          78, 'B', 'contacted', 'outbound', 'REPORTS_DEMO_v1', now() - interval '13 days', now() - interval '13 days'),
    (v_roca, 'Summit Strategy',           'Manchester', '0161 555 0814', 'andrew@summitstrat.co.uk',       'summitstrat.co.uk',         4.6, 26, 'Andrew Summit',   'Director',          82, 'A', 'contacted', 'outbound', 'REPORTS_DEMO_v1', now() - interval '15 days', now() - interval '15 days'),
    (v_roca, 'Peak Consulting',           'Leeds',      '0113 555 0815', 'megan@peakconsulting.co.uk',     'peakconsulting.co.uk',      4.4, 21, 'Megan Peak',      'Founder',           75, 'B', 'contacted', 'outbound', 'REPORTS_DEMO_v1', now() - interval '18 days', now() - interval '18 days'),
    (v_roca, 'Lowry Hill Partners',       'Salford',    '0161 555 0816', 'anna@lowryhill.co.uk',           'lowryhill.co.uk',           4.4, 19, 'Anna Lowry',      'Managing Partner',  76, 'B', 'contacted', 'outbound', 'REPORTS_DEMO_v1', now() - interval '22 days', now() - interval '22 days'),
    (v_roca, 'Crompton Accountancy',      'Bolton',     '01204 555 817', 'james@cromptonacc.co.uk',        'cromptonacc.co.uk',         3.9,  8, 'James Crompton',  'Senior Partner',    61, 'C', 'contacted', 'outbound', 'REPORTS_DEMO_v1', now() - interval '25 days', now() - interval '25 days'),
    (v_roca, 'Westbridge Accountants',    'Bolton',     '01204 555 818', 'tariq@westbridgeacc.co.uk',      'westbridgeacc.co.uk',       4.0,  9, 'Tariq Hussain',   'Managing Partner',  64, 'C', 'contacted', 'outbound', 'REPORTS_DEMO_v1', now() - interval '28 days', now() - interval '28 days'),

    -- New (queue — leads sourced this week, not yet contacted)
    (v_roca, 'Pemberton Finance',         'Wigan',      '01942 555 819', 'jack@pembertonfin.co.uk',        'pembertonfin.co.uk',        4.1, 13, 'Jack Pemberton',  'Owner',             67, 'C', 'new',     'outbound', 'REPORTS_DEMO_v1', now() - interval '1 day',   now() - interval '1 day'),
    (v_roca, 'Manchester Skin Clinic',    'Manchester', '0161 555 0820', 'lisa@manskin.co.uk',             'manskin.co.uk',             4.5, 31, 'Dr Lisa Yang',    'Founder',           77, 'B', 'new',     'outbound', 'REPORTS_DEMO_v1', now() - interval '2 days',  now() - interval '2 days'),
    (v_roca, 'Bloom Aesthetics',          'Manchester', '0161 555 0821', 'hannah@bloomaesth.co.uk',        'bloomaesth.co.uk',          4.2, 18, 'Dr Hannah Bloom', 'Founder',           71, 'B', 'new',     'outbound', 'REPORTS_DEMO_v1', now() - interval '3 days',  now() - interval '3 days'),

    -- Lost (closed funnel for the period — historical context)
    (v_roca, 'Maclean Wilkes',            'Manchester', '0161 555 0822', 'iain@macleanwilkes.co.uk',       'macleanwilkes.co.uk',       3.8,  6, 'Iain Maclean',    'Director',          58, 'C', 'lost',    'outbound', 'REPORTS_DEMO_v1', now() - interval '38 days', now() - interval '32 days'),
    (v_roca, 'Highfield Bookkeeping',     'Stockport',  '0161 555 0823', 'rachel@highfieldbk.co.uk',       'highfieldbk.co.uk',         4.2, 16, 'Rachel Mason',    'Owner',             69, 'B', 'lost',    'outbound', 'REPORTS_DEMO_v1', now() - interval '52 days', now() - interval '45 days'),
    (v_roca, 'Linton & Hale',             'Liverpool',  '0151 555 0824', 'marcus@lintonhale.co.uk',        'lintonhale.co.uk',          4.0, 11, 'Marcus Linton',   'Partner',           65, 'C', 'lost',    'outbound', 'REPORTS_DEMO_v1', now() - interval '70 days', now() - interval '63 days');

  -- ── Vidora Media — 25 leads ──────────────────────────────────────────
  insert into crm.leads
    (client_id, business_name, city, phone, email, website,
     google_rating, google_review_count, decision_maker_name, decision_maker_title,
     overall_score, grade, status, source, notes, created_at, updated_at)
  values
    -- Wins fixture (6 rows)
    (v_vidora, 'Whitford Property Group', 'Manchester', '0161 555 0901', 'james@whitfordproperty.co.uk',   'whitfordproperty.co.uk',    4.5, 23, 'James Whitford',  'MD',                80, 'A', 'meeting', 'outbound', 'REPORTS_DEMO_v1', now() - interval '9 days',   now() - interval '4 days'),
    (v_vidora, 'Shah Studios',             'London',     '020 7946 0902', 'priya@shahstudios.co.uk',        'shahstudios.co.uk',         4.6, 31, 'Priya Shah',      'Founder',           83, 'A', 'meeting', 'outbound', 'REPORTS_DEMO_v1', now() - interval '13 days',  now() - interval '6 days'),
    (v_vidora, 'Webb Creator Network',     'Manchester', '0161 555 0903', 'daniel@webbcreator.co.uk',       'webbcreator.co.uk',         4.7, 38, 'Daniel Webb',     'Founder',           87, 'A', 'won',     'outbound', 'REPORTS_DEMO_v1', now() - interval '18 days',  now() - interval '9 days'),
    (v_vidora, 'Pyke & Co Films',          'London',     '020 7946 0904', 'aaron@pykefilms.co.uk',          'pykefilms.co.uk',           4.4, 21, 'Aaron Pyke',      'Director',          76, 'B', 'meeting', 'outbound', 'REPORTS_DEMO_v1', now() - interval '25 days',  now() - interval '18 days'),
    (v_vidora, 'Marsh Creative House',     'Liverpool',  '0151 555 0905', 'helena@marshcreative.co.uk',     'marshcreative.co.uk',       4.3, 17, 'Helena Marsh',    'Owner',             73, 'B', 'meeting', 'outbound', 'REPORTS_DEMO_v1', now() - interval '40 days',  now() - interval '33 days'),
    (v_vidora, 'Caldwell Brothers Media',  'Manchester', '0161 555 0906', 'ross@caldwellmedia.co.uk',       'caldwellmedia.co.uk',       4.5, 28, 'Ross Caldwell',   'MD',                80, 'A', 'won',     'outbound', 'REPORTS_DEMO_v1', now() - interval '60 days',  now() - interval '52 days'),

    -- Replied
    (v_vidora, 'Mancunian Marketing',      'Manchester', '0161 555 0907', 'lucy@mancunianmkt.co.uk',        'mancunianmkt.co.uk',        4.5, 23, 'Lucy Adeyemi',    'Founder',           80, 'A', 'replied', 'outbound', 'REPORTS_DEMO_v1', now() - interval '4 days',   now() - interval '4 days'),
    (v_vidora, 'Foreshore Branding',       'Liverpool',  '0151 555 0908', 'asha@foreshorebrand.co.uk',      'foreshorebrand.co.uk',      4.6, 32, 'Asha Foreshore',  'Founder',           83, 'A', 'replied', 'outbound', 'REPORTS_DEMO_v1', now() - interval '5 days',   now() - interval '5 days'),
    (v_vidora, 'Fielder Property',         'Manchester', '0161 555 0909', 'greta@fielderpro.co.uk',         'fielderpro.co.uk',          4.6, 35, 'Greta Fielder',   'Director',          84, 'A', 'replied', 'outbound', 'REPORTS_DEMO_v1', now() - interval '7 days',   now() - interval '7 days'),

    -- Contacted
    (v_vidora, 'Northern Lights Agency',   'Leeds',      '0113 555 0910', 'owen@northlightsag.co.uk',       'northlightsag.co.uk',       4.3, 19, 'Owen Drake',      'MD',                73, 'B', 'contacted', 'outbound', 'REPORTS_DEMO_v1', now() - interval '6 days',   now() - interval '6 days'),
    (v_vidora, 'Holden Architects',        'Salford',    '0161 555 0911', 'theo@holdenarch.co.uk',          'holdenarch.co.uk',          4.4, 28, 'Theo Holden',     'Senior Architect',  76, 'B', 'contacted', 'outbound', 'REPORTS_DEMO_v1', now() - interval '8 days',   now() - interval '8 days'),
    (v_vidora, 'Quinn Legal',              'Manchester', '0161 555 0912', 'aoife@quinnlegal.co.uk',         'quinnlegal.co.uk',          4.7, 41, 'Aoife Quinn',     'Senior Partner',    86, 'A', 'contacted', 'outbound', 'REPORTS_DEMO_v1', now() - interval '10 days',  now() - interval '10 days'),
    (v_vidora, 'Aldridge Studios',         'Manchester', '0161 555 0913', 'naomi@aldridge.co.uk',           'aldridge.co.uk',            4.8, 49, 'Naomi Aldridge',  'Owner',             89, 'A', 'contacted', 'outbound', 'REPORTS_DEMO_v1', now() - interval '12 days',  now() - interval '12 days'),
    (v_vidora, 'Vance & Webb',             'Manchester', '0161 555 0914', 'robert@vancewebb.co.uk',         'vancewebb.co.uk',           4.5, 24, 'Robert Vance',    'Managing Partner',  78, 'B', 'contacted', 'outbound', 'REPORTS_DEMO_v1', now() - interval '14 days',  now() - interval '14 days'),
    (v_vidora, 'Edge Communications',      'Manchester', '0161 555 0915', 'patrick@edgecomms.co.uk',        'edgecomms.co.uk',           4.0, 11, 'Patrick Edge',    'Founder',           65, 'C', 'contacted', 'outbound', 'REPORTS_DEMO_v1', now() - interval '17 days',  now() - interval '17 days'),
    (v_vidora, 'Kestrel HR',               'Manchester', '0161 555 0916', 'owen@kestrelhr.co.uk',           'kestrelhr.co.uk',           4.4, 20, 'Owen Kestrel',    'Director',          74, 'B', 'contacted', 'outbound', 'REPORTS_DEMO_v1', now() - interval '20 days',  now() - interval '20 days'),
    (v_vidora, 'Brightside Property',      'Stockport',  '0161 555 0917', 'ella@brightsidepro.co.uk',       'brightsidepro.co.uk',       4.3, 16, 'Ella Brightside', 'Director',          72, 'B', 'contacted', 'outbound', 'REPORTS_DEMO_v1', now() - interval '23 days',  now() - interval '23 days'),
    (v_vidora, 'Halcyon Insurance',        'Manchester', '0161 555 0918', 'marcus@halcyoninsure.co.uk',     'halcyoninsure.co.uk',       4.1, 13, 'Marcus Halcyon',  'MD',                66, 'C', 'contacted', 'outbound', 'REPORTS_DEMO_v1', now() - interval '27 days',  now() - interval '27 days'),

    -- New
    (v_vidora, 'Highbridge Recruit',       'Manchester', '0161 555 0919', 'selina@highbridgerec.co.uk',     'highbridgerec.co.uk',       4.2, 17, 'Selina Mota',     'Director',          70, 'B', 'new',     'outbound', 'REPORTS_DEMO_v1', now() - interval '1 day',    now() - interval '1 day'),
    (v_vidora, 'Carter Coaching',          'Liverpool',  '0151 555 0920', 'ben@cartercoaching.co.uk',       'cartercoaching.co.uk',      3.9,  7, 'Ben Carter',      'Founder',           60, 'C', 'new',     'outbound', 'REPORTS_DEMO_v1', now() - interval '2 days',   now() - interval '2 days'),
    (v_vidora, 'Mendel Productions',       'Manchester', '0161 555 0921', 'rita@mendelpro.co.uk',           'mendelpro.co.uk',           4.4, 22, 'Rita Mendel',     'Founder',           74, 'B', 'new',     'outbound', 'REPORTS_DEMO_v1', now() - interval '3 days',   now() - interval '3 days'),

    -- Lost
    (v_vidora, 'Perrin Design Studio',     'Manchester', '0161 555 0922', 'olivia@perrindesign.co.uk',      'perrindesign.co.uk',        4.0, 10, 'Olivia Perrin',   'Founder',           63, 'C', 'lost',    'outbound', 'REPORTS_DEMO_v1', now() - interval '36 days',  now() - interval '30 days'),
    (v_vidora, 'Halewood Branding',        'Liverpool',  '0151 555 0923', 'george@halewoodbrand.co.uk',     'halewoodbrand.co.uk',       4.2, 14, 'George Halewood', 'MD',                68, 'C', 'lost',    'outbound', 'REPORTS_DEMO_v1', now() - interval '54 days',  now() - interval '47 days'),
    (v_vidora, 'Duxbury Creative',         'Manchester', '0161 555 0924', 'mike@duxburycreat.co.uk',        'duxburycreat.co.uk',        4.1, 12, 'Mike Duxbury',    'Founder',           67, 'C', 'lost',    'outbound', 'REPORTS_DEMO_v1', now() - interval '72 days',  now() - interval '65 days');

  -- ── Emails — Day 1 (sent for every lead past 'new') ─────────────────
  insert into crm.emails (lead_id, client_id, email_number, subject, body, status, sent_at, replied_at)
  select
    l.id, l.client_id, 1,
    'A quick thought on ' || l.business_name,
    'Hi ' || coalesce(split_part(l.decision_maker_name, ' ', 1), l.business_name)
      || ', noticed a few things about your setup that might be worth a 25-min chat.',
    'sent',
    l.created_at + interval '1 hour',
    case when l.status in ('replied','meeting','won') then l.created_at + interval '2 days' else null end
  from crm.leads l
  where l.notes = 'REPORTS_DEMO_v1' and l.status != 'new';

  -- ── Emails — Day 3 follow-up (sent for engaged + lost paths) ────────
  insert into crm.emails (lead_id, client_id, email_number, subject, body, status, sent_at, replied_at)
  select
    l.id, l.client_id, 2,
    'Following up on ' || l.business_name,
    'Hi ' || coalesce(split_part(l.decision_maker_name, ' ', 1), 'there')
      || ', circling back on the note from earlier in the week.',
    'sent',
    l.created_at + interval '3 days',
    null
  from crm.leads l
  where l.notes = 'REPORTS_DEMO_v1' and l.status in ('contacted','replied','meeting','won','lost')
    and l.created_at + interval '3 days' < now();

  -- ── Emails — Day 7 (mix of sent for older leads, scheduled for newer) ─
  insert into crm.emails (lead_id, client_id, email_number, subject, body, status, sent_at, replied_at)
  select
    l.id, l.client_id, 3,
    'Last note ' || l.business_name,
    'Hi ' || coalesce(split_part(l.decision_maker_name, ' ', 1), 'there')
      || ', last one from me — happy to leave it here if the timing is wrong.',
    'sent',
    l.created_at + interval '7 days',
    null
  from crm.leads l
  where l.notes = 'REPORTS_DEMO_v1' and l.status in ('contacted','lost')
    and l.created_at + interval '7 days' < now();

  insert into crm.emails (lead_id, client_id, email_number, subject, body, status, scheduled_at)
  select
    l.id, l.client_id, 3,
    'Last note ' || l.business_name,
    '(scheduled — not yet sent)',
    'scheduled',
    l.created_at + interval '7 days'
  from crm.leads l
  where l.notes = 'REPORTS_DEMO_v1' and l.status = 'contacted'
    and l.created_at + interval '7 days' >= now();

  -- ── Replies (one per lead in replied/meeting/won) ───────────────────
  insert into crm.replies (lead_id, from_address, subject, body, sentiment, detected_at, processed)
  select
    l.id,
    coalesce(l.email, lower(replace(l.business_name,' ','')) || '@example.co.uk'),
    'Re: A quick thought on ' || l.business_name,
    case
      when l.status = 'meeting' then 'Sounds good — happy to do a 25-min call. Tuesday or Thursday afternoon work?'
      when l.status = 'won'     then 'Yes, let''s get this moving. What''s the next step on your side?'
      else 'Could you send a bit more on what this looks like in practice?'
    end,
    case when l.status in ('meeting','won') then 'positive' else 'neutral' end,
    l.created_at + interval '2 days',
    case when l.status in ('meeting','won') then true else false end
  from crm.leads l
  where l.notes = 'REPORTS_DEMO_v1' and l.status in ('replied','meeting','won');

  -- ── Activity log ────────────────────────────────────────────────────
  insert into crm.activity_log (client_id, lead_id, action, detail, created_at)
  select l.client_id, l.id, 'lead_created',
         l.business_name || ' — ' || coalesce(l.city, 'unknown'),
         l.created_at
  from crm.leads l where l.notes = 'REPORTS_DEMO_v1';

  insert into crm.activity_log (client_id, lead_id, action, detail, created_at)
  select l.client_id, l.id, 'reply_received',
         l.business_name || ' (' || coalesce(r.sentiment, 'neutral') || ')',
         r.detected_at
  from crm.leads l
  join crm.replies r on r.lead_id = l.id
  where l.notes = 'REPORTS_DEMO_v1';

  insert into crm.activity_log (client_id, lead_id, action, detail, created_at)
  select l.client_id, l.id, 'meeting_booked',
         l.business_name || ' — ' || coalesce(l.decision_maker_name, 'contact'),
         l.updated_at
  from crm.leads l
  where l.notes = 'REPORTS_DEMO_v1' and l.status = 'meeting';

  insert into crm.activity_log (client_id, lead_id, action, detail, created_at)
  select l.client_id, l.id, 'deal_won',
         l.business_name || ' — closed',
         l.updated_at
  from crm.leads l
  where l.notes = 'REPORTS_DEMO_v1' and l.status = 'won';

end$$;

commit;
