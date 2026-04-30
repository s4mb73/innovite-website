-- Innovite CRM — Step 8 (Inbound) demo seed (NOT FOR PRODUCTION)
-- Populates crm.inbound_leads with 14 realistic UK B2B form submissions
-- across the score (hot/warm/cold) and status pipeline so the Inbound
-- page renders with usable demo content. Idempotent — skips if any row
-- already exists.
--
-- To wipe before launch:
--   truncate table crm.inbound_leads restart identity;
--
-- All names / companies below are fictional and tagged in `notes`
-- with the marker '[demo]' so the wipe pattern is obvious.

begin;

do $$
declare
  v_now timestamptz := now();
begin
  if exists (select 1 from crm.inbound_leads limit 1) then
    raise notice 'Inbound leads already exist — skipping demo seed.';
    return;
  end if;

  -- ── 14 inbound submissions ────────────────────────────────────────
  --   Score distribution: 4 hot, 7 warm, 3 cold
  --   Status mix: 5 new, 3 contacted, 2 called, 1 proposal, 1 won, 2 lost
  --   Auto-response sent: 12 of 14 (skips 2 most-recent cold rows to
  --   make the "needs response" panel feel real)
  insert into crm.inbound_leads
    (name, company, email, phone, industry, deal_value,
     current_method, clients_wanted, score, status,
     auto_response_sent, auto_response_sent_at, notes, created_at)
  values

    -- ── Hot: high-intent, recent ─────────────────────────────────────
    ('Marcus Webb', 'Webb & Co Solicitors', 'marcus.webb@webbco.uk',
     '+44 20 7946 0312', 'Legal services', '£5-10k',
     'Word of mouth and a referral partner — ad hoc, no system.',
     '4-6 a month', 'hot', 'new',
     true, v_now - interval '3 hours' + interval '11 seconds',
     '[demo] Read ROCA case study before submitting.',
     v_now - interval '3 hours'),

    ('Sarah Patel', 'Verde Strategy', 'sarah@verde-strategy.co.uk',
     '+44 161 408 2210', 'Strategy consulting', '£2-5k',
     'LinkedIn outbound by hand, ~30 messages a week, low conversion.',
     '3 a month', 'hot', 'new',
     true, v_now - interval '5 hours' + interval '14 seconds',
     '[demo]', v_now - interval '5 hours'),

    ('David Kim', 'Kim Architects', 'david@kimarch.co.uk',
     '+44 117 902 4456', 'Architecture', '£2-5k',
     'Cold calls + Architects Journal directory listings.',
     '2 a month', 'hot', 'contacted',
     true, v_now - interval '18 hours' + interval '9 seconds',
     '[demo] Replied with a meeting request — booking pending.',
     v_now - interval '18 hours'),

    ('Olivia Bennett', 'Bennett & Cole Accountants',
     'olivia@bennettcole.co.uk', '+44 113 555 8821',
     'Accountancy', '£2-5k',
     'Referrals plus paid Google ads — CPL is climbing.',
     '5 a month', 'hot', 'called',
     true, v_now - interval '2 days' + interval '13 seconds',
     '[demo] Discovery call done — fits ROCA mould.',
     v_now - interval '2 days'),

    -- ── Warm: solid fit, mid-funnel ──────────────────────────────────
    ('Rachel Hughes', 'Hughes Recruitment', 'rachel@hughesrecruit.co.uk',
     '+44 151 408 7733', 'Recruitment', '£2-5k',
     'LinkedIn Recruiter + cold email on Apollo.',
     '6-8 a month', 'warm', 'proposal',
     true, v_now - interval '6 days' + interval '10 seconds',
     '[demo] Proposal sent — chasing for sign-off this week.',
     v_now - interval '6 days'),

    ('James Whitfield', 'Whitfield Wealth Advisors',
     'james@whitfieldwealth.co.uk', '+44 131 558 4490',
     'Financial advisory', '£5-10k',
     'Print ads in The Scotsman + a quarterly seminar.',
     '2 a month', 'warm', 'won',
     true, v_now - interval '12 days' + interval '17 seconds',
     '[demo] Signed £3.5k retainer — Sammy is lead.',
     v_now - interval '12 days'),

    ('Daniel O''Brien', 'O''Brien & Murphy LLP',
     'daniel@obrienmurphy.co.uk', '+44 20 7100 5544',
     'Legal services', '£5-10k',
     'Repeat clients only — zero outbound.',
     '3 a month', 'warm', 'contacted',
     true, v_now - interval '3 days' + interval '12 seconds',
     '[demo] Sent the legal-vertical case study.',
     v_now - interval '3 days'),

    ('Hannah Wright', 'Wright Bookkeeping', 'hannah@wrightbooks.co.uk',
     '+44 29 2055 1190', 'Accountancy / bookkeeping', '£1-2k',
     'Yell + Facebook page, mostly local search.',
     '3 a month', 'warm', 'new',
     true, v_now - interval '9 hours' + interval '15 seconds',
     '[demo]', v_now - interval '9 hours'),

    ('Liam Foster', 'Foster Marketing', 'liam@fostermarketing.co.uk',
     '+44 114 282 9905', 'Marketing agency', '£2-5k',
     'Inbound from existing portfolio + LinkedIn content.',
     '4 a month', 'warm', 'contacted',
     true, v_now - interval '4 days' + interval '8 seconds',
     '[demo] Slightly cautious — competitor in our stack.',
     v_now - interval '4 days'),

    ('Charlotte Reed', 'Reed & Park Insurance Brokers',
     'charlotte@reedpark.co.uk', '+44 161 408 2188',
     'Insurance brokerage', '£5-10k',
     'Networking + a paid SEO retainer.',
     '5 a month', 'warm', 'called',
     true, v_now - interval '5 days' + interval '11 seconds',
     '[demo] Discovery call done — sending proposal Monday.',
     v_now - interval '5 days'),

    ('Amara Okonkwo', 'Okonkwo Cardiology', 'amara@okonkwocardio.co.uk',
     '+44 20 7946 8842', 'Private healthcare', '£5-10k',
     'GP referrals + a small Google Ads budget.',
     '2-3 a month', 'warm', 'new',
     true, v_now - interval '11 hours' + interval '12 seconds',
     '[demo]', v_now - interval '11 hours'),

    -- ── Cold: weak fit / disqualifiers ───────────────────────────────
    ('Tom Davies', 'Solo IT', 'tom@soloit.uk', '+44 121 555 0099',
     'IT support (1-person)', '<£1k',
     'Word of mouth — looking for £500/mo packages.',
     '1 a month', 'cold', 'lost',
     true, v_now - interval '8 days' + interval '14 seconds',
     '[demo] DQ — budget below floor.',
     v_now - interval '8 days'),

    ('Priya Nair', 'Nair Design Studio', 'priya@nairdesign.co.uk',
     '+44 20 7946 1133', 'Design agency', '£1-2k',
     'Behance + Dribbble + referrals.',
     '2 a month', 'cold', 'lost',
     false, null,
     '[demo] DQ — wrong segment, no auto-reply triggered.',
     v_now - interval '14 days'),

    ('Ben Holloway', 'Holloway Construction', 'ben@hollowayconstruct.uk',
     '+44 191 408 5566', 'Construction', '£2-5k',
     'Trade press + tender platforms.',
     '1 a month', 'cold', 'new',
     false, null,
     '[demo] No auto-reply triggered — qualifier flagged segment.',
     v_now - interval '6 hours');

  raise notice 'Inserted 14 demo inbound submissions.';
end $$;

commit;
