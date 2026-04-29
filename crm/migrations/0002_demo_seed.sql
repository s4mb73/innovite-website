-- Innovite CRM — demo seed data (NOT FOR PRODUCTION)
-- Populates ~30 days of leads / emails / replies / activity so the
-- Overview dashboard renders with realistic-looking numbers while we
-- build out Steps 4–10. Idempotent — uses on-conflict-do-nothing
-- where possible. To wipe before launch, run:
--
--   truncate table crm.activity_log, crm.replies, crm.emails, crm.leads
--           restart identity cascade;
--
-- (Clients table is untouched.)

begin;

-- ─────────────────────────────────────────────────────────────────────
-- Resolve client IDs by name so this works regardless of seed order
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
    raise exception 'Seed clients not found — run 0001_init.sql first.';
  end if;

  -- Skip if leads already seeded
  if exists (select 1 from crm.leads limit 1) then
    raise notice 'Leads already exist — skipping demo seed.';
    return;
  end if;

  -- ── Leads (40 rows, spread over 30 days) ───────────────────────────
  insert into crm.leads
    (client_id, business_name, city, phone, email, website,
     google_rating, google_review_count, decision_maker_name, decision_maker_title,
     overall_score, grade, status, source, created_at)
  values
    -- ROCA targets (accountancy prospects)
    (v_roca, 'Walsh & Pearson',         'Manchester', '0161 234 5678', 'info@walshpearson.co.uk',  'walshpearson.co.uk',  4.7, 38,  'James Walsh',     'Managing Partner',  87, 'A', 'meeting',   'outbound', v_now - interval '2 days'),
    (v_roca, 'Highbury Tax Advisory',   'Liverpool',  '0151 555 0102', 'hello@highburytax.co.uk',  'highburytax.co.uk',   4.5, 22,  'Sarah Bennett',   'Director',          81, 'A', 'replied',   'outbound', v_now - interval '3 days'),
    (v_roca, 'Clarke Foster Accountants','Leeds',     '0113 555 0103', 'office@clarkefoster.co.uk','clarkefoster.co.uk',  4.3, 17,  'Michael Clarke',  'Senior Partner',    74, 'B', 'replied',   'outbound', v_now - interval '4 days'),
    (v_roca, 'Bennett Hayes',           'Manchester', '0161 555 0104', null,                       'bennetthayes.co.uk',  4.8, 51,  'Olivia Hayes',    'Founder',           91, 'A', 'contacted', 'outbound', v_now - interval '4 days'),
    (v_roca, 'Northgate Financial',     'Leeds',      '0113 555 0105', 'team@northgatefin.co.uk',  'northgatefin.co.uk',  4.1, 12,  'Robert Singh',    'Director',          68, 'B', 'contacted', 'outbound', v_now - interval '5 days'),
    (v_roca, 'Pearson & Doyle',         'Stockport',  '0161 555 0106', 'mail@pearsondoyle.co.uk',  'pearsondoyle.co.uk',  4.6, 29,  'Helen Doyle',     'Managing Partner',  79, 'B', 'contacted', 'outbound', v_now - interval '5 days'),
    (v_roca, 'Mancunian Tax',           'Manchester', '0161 555 0107', 'info@mancuniantax.co.uk',  'mancuniantax.co.uk',  4.2, 14,  'David Patel',     'Director',          71, 'B', 'contacted', 'outbound', v_now - interval '6 days'),
    (v_roca, 'Sterling Lane Advisory',  'Manchester', '0161 555 0108', null,                       'sterlinglane.co.uk',  4.9, 67,  'Emily Sterling',  'Founder',           94, 'A', 'meeting',   'outbound', v_now - interval '6 days'),
    (v_roca, 'Crompton Accountancy',    'Bolton',     '01204 555 109', 'info@cromptonacc.co.uk',   'cromptonacc.co.uk',   3.9, 8,   'James Crompton',  'Senior Partner',    61, 'C', 'contacted', 'outbound', v_now - interval '7 days'),
    (v_roca, 'Lowry Hill Partners',     'Salford',    '0161 555 0110', 'office@lowryhill.co.uk',   'lowryhill.co.uk',     4.4, 19,  'Anna Lowry',      'Managing Partner',  76, 'B', 'contacted', 'outbound', v_now - interval '7 days'),
    (v_roca, 'Northwood Advisory',      'Manchester', '0161 555 0111', 'hello@northwoodadv.co.uk', 'northwoodadv.co.uk',  4.7, 33,  'Hannah Whitcombe','Director',          85, 'A', 'replied',   'outbound', v_now - interval '8 days'),
    (v_roca, 'Linton & Hale',           'Liverpool',  '0151 555 0112', 'mail@lintonhale.co.uk',    'lintonhale.co.uk',    4.0, 11,  'Marcus Linton',   'Partner',           65, 'C', 'new',       'outbound', v_now - interval '9 days'),
    (v_roca, 'Anand Recruitment',       'Manchester', '0161 555 0113', 'priya@anandrec.co.uk',     'anandrec.co.uk',      4.6, 41,  'Priya Anand',     'MD',                83, 'A', 'meeting',   'outbound', v_now - interval '10 days'),
    (v_roca, 'Berridge IT Services',    'Leeds',      '0113 555 0114', 'tom@berridgeit.co.uk',     'berridgeit.co.uk',    4.3, 24,  'Tom Berridge',    'Founder',           74, 'B', 'contacted', 'outbound', v_now - interval '11 days'),
    (v_roca, 'Maclean Wilkes',          'Manchester', '0161 555 0115', null,                       'macleanwilkes.co.uk', 3.8, 6,   'Iain Maclean',    'Director',          58, 'C', 'lost',      'outbound', v_now - interval '13 days'),
    (v_roca, 'Highfield Bookkeeping',   'Stockport',  '0161 555 0116', 'admin@highfieldbk.co.uk',  'highfieldbk.co.uk',   4.2, 16,  'Rachel Mason',    'Owner',             69, 'B', 'lost',      'outbound', v_now - interval '14 days'),
    (v_roca, 'Carlton Tax Group',       'Liverpool',  '0151 555 0117', 'office@carltontax.co.uk',  'carltontax.co.uk',    4.5, 28,  'Daniel Carlton',  'Partner',           80, 'B', 'won',       'outbound', v_now - interval '17 days'),
    (v_roca, 'Eastgate Advisory',       'Manchester', '0161 555 0118', 'info@eastgateadv.co.uk',   'eastgateadv.co.uk',   4.4, 22,  'Sophie Hill',     'Director',          78, 'B', 'contacted', 'outbound', v_now - interval '19 days'),
    (v_roca, 'Pemberton Finance',       'Wigan',      '01942 555 119', null,                       'pembertonfin.co.uk',  4.1, 13,  'Jack Pemberton',  'Owner',             67, 'C', 'new',       'outbound', v_now - interval '21 days'),
    (v_roca, 'Westbridge Accountants',  'Bolton',     '01204 555 120', 'mail@westbridgeacc.co.uk', 'westbridgeacc.co.uk', 4.0, 9,   'Tariq Hussain',   'Managing Partner',  64, 'C', 'new',       'outbound', v_now - interval '23 days'),
    (v_roca, 'Apex Financial Partners', 'Manchester', '0161 555 0121', 'enquiries@apexfp.co.uk',   'apexfp.co.uk',        4.8, 56,  'Charlotte Reed',  'Managing Director', 92, 'A', 'replied',   'outbound', v_now - interval '1 day'),
    (v_roca, 'Sterling Partners LLP',   'Manchester', '0161 555 0122', null,                       'sterlingpartners.uk', 4.7, 44,  'Ed Sterling',     'Senior Partner',    88, 'A', 'meeting',   'outbound', v_now - interval '2 days'),
    (v_roca, 'Manchester Skin Clinic',  'Manchester', '0161 555 0123', 'admin@manskin.co.uk',      'manskin.co.uk',       4.5, 31,  'Dr Lisa Yang',    'Founder',           77, 'B', 'contacted', 'outbound', v_now - interval '3 days'),
    (v_roca, 'Summit Strategy',         'Manchester', '0161 555 0124', 'hello@summitstrat.co.uk',  'summitstrat.co.uk',   4.6, 26,  'Andrew Summit',   'Director',          82, 'A', 'contacted', 'outbound', v_now - interval '6 days'),
    (v_roca, 'Peak Consulting',         'Leeds',      '0113 555 0125', null,                       'peakconsulting.co.uk',4.4, 21,  'Megan Peak',      'Founder',           75, 'B', 'meeting',   'outbound', v_now - interval '8 days'),
    (v_roca, 'Bloom Aesthetics',        'Manchester', '0161 555 0126', 'info@bloomaesth.co.uk',    'bloomaesth.co.uk',    4.2, 18,  'Dr Hannah Bloom', 'Founder',           71, 'B', 'contacted', 'outbound', v_now - interval '12 days'),

    -- Vidora targets (B2B service prospects)
    (v_vidora, 'Mancunian Marketing',   'Manchester', '0161 555 0201', 'team@mancunianmkt.co.uk',  'mancunianmkt.co.uk',  4.5, 23,  'Lucy Adeyemi',    'Founder',           80, 'A', 'replied',   'outbound', v_now - interval '4 days'),
    (v_vidora, 'Northern Lights Agency','Leeds',      '0113 555 0202', 'hello@northlightsag.co.uk','northlightsag.co.uk', 4.3, 19,  'Owen Drake',      'MD',                73, 'B', 'contacted', 'outbound', v_now - interval '5 days'),
    (v_vidora, 'Fielder Property',      'Manchester', '0161 555 0203', 'enquiries@fielderpro.co.uk','fielderpro.co.uk',   4.6, 35,  'Greta Fielder',   'Director',          84, 'A', 'meeting',   'outbound', v_now - interval '6 days'),
    (v_vidora, 'Holden Architects',     'Salford',    '0161 555 0204', 'office@holdenarch.co.uk',  'holdenarch.co.uk',    4.4, 28,  'Theo Holden',     'Senior Architect',  76, 'B', 'contacted', 'outbound', v_now - interval '7 days'),
    (v_vidora, 'Quinn Legal',           'Manchester', '0161 555 0205', 'info@quinnlegal.co.uk',    'quinnlegal.co.uk',    4.7, 41,  'Aoife Quinn',     'Senior Partner',    86, 'A', 'contacted', 'outbound', v_now - interval '9 days'),
    (v_vidora, 'Carter Coaching',       'Liverpool',  '0151 555 0206', null,                       'cartercoaching.co.uk',3.9, 7,   'Ben Carter',      'Founder',           60, 'C', 'new',       'outbound', v_now - interval '10 days'),
    (v_vidora, 'Highbridge Recruit',    'Manchester', '0161 555 0207', 'team@highbridgerec.co.uk', 'highbridgerec.co.uk', 4.2, 17,  'Selina Mota',     'Director',          70, 'B', 'lost',      'outbound', v_now - interval '15 days'),
    (v_vidora, 'Vance & Webb',          'Manchester', '0161 555 0208', 'info@vancewebb.co.uk',     'vancewebb.co.uk',     4.5, 24,  'Robert Vance',    'Managing Partner',  78, 'B', 'contacted', 'outbound', v_now - interval '17 days'),
    (v_vidora, 'Aldridge Studios',      'Manchester', '0161 555 0209', 'studios@aldridge.co.uk',   'aldridge.co.uk',      4.8, 49,  'Naomi Aldridge',  'Owner',             89, 'A', 'won',       'outbound', v_now - interval '20 days'),
    (v_vidora, 'Edge Communications',   'Manchester', '0161 555 0210', 'mail@edgecomms.co.uk',     'edgecomms.co.uk',     4.0, 11,  'Patrick Edge',    'Founder',           65, 'C', 'new',       'outbound', v_now - interval '24 days'),
    (v_vidora, 'Brightside Property',   'Stockport',  '0161 555 0211', 'enquiries@brightsidepro.co.uk','brightsidepro.co.uk', 4.3, 16, 'Ella Brightside','Director',     72, 'B', 'new',       'outbound', v_now - interval '1 day'),
    (v_vidora, 'Halcyon Insurance',     'Manchester', '0161 555 0212', 'info@halcyoninsure.co.uk', 'halcyoninsure.co.uk', 4.1, 13,  'Marcus Halcyon',  'MD',                66, 'C', 'new',       'outbound', v_now - interval '2 days'),
    (v_vidora, 'Foreshore Branding',    'Liverpool',  '0151 555 0213', 'studio@foreshorebrand.co.uk','foreshorebrand.co.uk', 4.6, 32,'Asha Foreshore', 'Founder',          83, 'A', 'replied',   'outbound', v_now - interval '3 days'),
    (v_vidora, 'Kestrel HR',            'Manchester', '0161 555 0214', 'hello@kestrelhr.co.uk',    'kestrelhr.co.uk',     4.4, 20,  'Owen Kestrel',    'Director',          74, 'B', 'contacted', 'outbound', v_now - interval '8 days');

  -- ── Emails (a sample of sent + scheduled) ───────────────────────────
  insert into crm.emails (lead_id, client_id, email_number, subject, body, status, sent_at, replied_at)
  select
    l.id, l.client_id,
    1,
    'A quick thought on ' || l.business_name,
    'Hi ' || coalesce(l.decision_maker_name, l.business_name) || ', noticed a few things about your setup that might be worth talking about.',
    'sent',
    l.created_at + interval '1 hour',
    case when l.status = 'replied' or l.status = 'meeting' or l.status = 'won' then l.created_at + interval '2 days' else null end
  from crm.leads l
  where l.status in ('contacted','replied','meeting','won','lost');

  -- ── Replies (a few realistic ones) ──────────────────────────────────
  insert into crm.replies (lead_id, from_address, subject, body, sentiment, detected_at)
  select
    l.id,
    coalesce(l.email, lower(replace(l.business_name,' ','')) || '@example.co.uk'),
    'Re: A quick thought on ' || l.business_name,
    case
      when l.status = 'meeting' then 'Sure, happy to chat. What does your week look like?'
      when l.status = 'won'     then 'Yes, lets get this moving.'
      else 'Could you send a bit more info?'
    end,
    case when l.status in ('meeting','won') then 'positive' else 'neutral' end,
    l.created_at + interval '2 days'
  from crm.leads l
  where l.status in ('replied','meeting','won');

  -- ── Activity log (mixed events) ─────────────────────────────────────
  insert into crm.activity_log (client_id, lead_id, action, detail, created_at)
  select l.client_id, l.id, 'lead_created', l.business_name || ' — ' || l.city, l.created_at
  from crm.leads l;

  insert into crm.activity_log (client_id, lead_id, action, detail, created_at)
  select l.client_id, l.id, 'reply_received', l.business_name || ' (' || coalesce(r.sentiment, 'neutral') || ')', r.detected_at
  from crm.leads l join crm.replies r on r.lead_id = l.id;

  insert into crm.activity_log (client_id, lead_id, action, detail, created_at)
  select l.client_id, l.id, 'meeting_booked', l.business_name || ' — Thursday 2:30pm', l.created_at + interval '3 days'
  from crm.leads l where l.status = 'meeting';

  insert into crm.activity_log (client_id, lead_id, action, detail, created_at) values
    (v_roca,   null, 'pipeline_run', 'ROCA Accountants — 23 new leads',   v_now - interval '4 hours'),
    (v_vidora, null, 'pipeline_run', 'Vidora Media — 14 new leads',       v_now - interval '6 hours'),
    (v_roca,   null, 'inbound_lead', 'Hannah Whitcombe — Northwood Advisory', v_now - interval '3 hours'),
    (v_vidora, null, 'inbound_lead', 'Theo Holden — Holden Architects',   v_now - interval '11 hours'),
    (v_roca,   null, 'pipeline_run', 'ROCA Accountants — 19 new leads',   v_now - interval '1 day');

end$$;

commit;
