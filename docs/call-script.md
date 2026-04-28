# Strategy Call Script

The 20-minute call with a qualified prospect. By the end of the call you should know
whether they're a fit, they should know what working with Innovite looks like, and
either side should feel okay walking away.

> Voice: direct, anti-fluff, UK. You're not "selling" — you're qualifying both ways.
> The site already promises "you leave knowing exactly what you'd change — whether
> you hire us or not." This call delivers on that.

---

## Before the call (3 minutes)

Open the lead in Supabase (Table editor → leads → search by email).
Read the row. Note:

- [ ] **Tier** (`tier` column) — hot / warm / cold / no-fit / null
- [ ] **Industry** (`industry`)
- [ ] **Deal value** (`deal_value`)
- [ ] **How they find clients now** (`current_method`)
- [ ] **Volume they want** (`target_clients`)
- [ ] **Qualifier answers if filled** (`qualifier_q1..q4`)

Quick web check on `company`:
- [ ] Their website — what do they sell, who's their ICP?
- [ ] LinkedIn — how big is the team? How long have they been around?
- [ ] Recent posts — anything in the last 3 months that signals growth or stagnation?

You should walk into the call already having an opinion on whether this is a fit.

---

## On the call

### 1. Open (60 seconds)

> "Cheers for jumping on. Quick brief on how I run these — 20 minutes, three parts.
> First five, I want to understand your pipeline today. Next ten, I'll show you what
> we'd actually do for a business like yours. Last five, I tell you whether I think
> we're a fit and what it'd cost. Sound good?"

You set the contract: short, direct, two-way.

### 2. Discovery (5–7 minutes)

Ask the four questions in order. Don't read them — ask them like a friend would.
Take notes. Don't sell yet.

**Q1 — What does a good client look like for you?**
> "Forget what's in your pipeline today. Describe a client where, six months in,
> you're thinking 'I want twenty more of these.' What do they do, what do they
> spend, what makes them easy to work with?"

What you're listening for: ICP clarity, deal size, retention. If they can't answer,
that's a real problem you'll have to solve before any outbound works.

**Q2 — What have you tried before and why did it fail?**
> "Before this call, what's been your go-to for getting new clients? And if you've
> tried anything that didn't work — agencies, freelancers, doing it yourself — what
> went wrong?"

What you're listening for: scars. People who've been burned by an agency are
either gun-shy or sceptics. Both fine, but you handle them differently.

**Q3 — Could you actually close?**
> "If I told you I could put five qualified conversations on your calendar next month,
> people who already know what you do and want to talk — could you close at least one?"

What you're listening for: capacity, conviction, sales process. If they hesitate,
the bottleneck isn't lead gen, it's their close rate. Don't sell them outbound.

**Q4 (optional, if they're slow on Q3) — What's stopping you scaling right now?**
> "What's the actual constraint? More leads, more time, better closers, the work itself?"

This catches people who'd be a bad client — overstretched, no team, can't deliver
on more work — even if they want to be one.

### 3. Show (5–7 minutes)

Two things, both visual. Share screen.

**Show 1 — Live pipeline dashboard** *(once Item #5 is built)*
Pick a current client in the same industry or similar deal-value tier.
Anonymise the brand name. Walk through:

- Prospects identified this week
- Qualified vs. unqualified
- Reply rate
- Meetings booked

> "This is week three for [anonymous client]. They're an accounting firm in Manchester,
> deal size £8k average. We started 16 days ago. They've had two booked calls this week.
> They spend roughly fifteen minutes a week on this — replying when prospects come back."

> *(Pre-#5: keep a static screenshot or PDF on hand. The hero card on the website is
> a stylised version — use that or build a real one in Notion / Google Sheets.)*

**Show 2 — Industry-specific intelligence report**
Pull up an example outbound research dossier for a hypothetical prospect in
**their** industry. Walk them through:

- The data we found (tech stack, hiring patterns, recent press, weak spots)
- The angle the email took
- The reply we got

> "This is the kind of research that goes into every email. It's not a template
> with their company name dropped in. The opening line references something that
> happened in their business in the last 30 days. That's why the reply rate
> looks the way it does."

### 4. Pitch + price (3–5 minutes)

If they're a fit (your call):

> "I think we'd work well. Here's how we structure it."

Lay out the three tiers in order, lowest to highest:

**Tier 1 — Outbound only — £1,500/mo**
- AI outbound running daily (research → score → personalised message)
- Three-email follow-up sequence
- Reply monitoring, handover to you
- Weekly report
- Dashboard access
- *"This is the right entry point if you've got content + ads handled and you
  just need a steady drip of qualified conversations."*

**Tier 2 — Outbound + Content — £2,500/mo**
- Everything in Tier 1
- Half-day shoot per month (we come to you, or studio in [city])
- 4 short-form videos produced
- Social content calendar
- *"Right tier if you don't have a content engine and you want one without
  hiring a video team."*

**Tier 3 — Full system — £3,500/mo**
- Everything in Tier 2
- Meta + Google ads managed end-to-end
- Landing page built and optimised for the campaigns
- AI follow-up on every ad lead, inside 5 minutes
- Monthly strategy call
- *"Right tier if you want the whole thing — outbound, content, ads — pulling
  in the same direction. Most clients land here within 60 days."*

**Audit day — £750**
- One full day of filming
- Intelligence report on their five biggest competitors
- Social strategy document
- *"If you're not ready to commit to a retainer, we do a one-off audit day. You
  walk away with a content library, a competitive analysis, and a strategy doc.
  Half the clients who do this convert to retainer within 60 days."*

Recommend one tier explicitly. Don't make them pick three options cold.

> "For where you're at — [their context] — I'd start you on Tier 2. You don't have
> a content engine. Outbound without content is harder than it needs to be. £2,500
> a month, month-to-month, no setup fees. Want me to send the agreement after this?"

### 5. Close (60 seconds)

Three possible outcomes. Be straight about which one this is.

**Yes** —
> "Brilliant. I'll send the agreement and an onboarding form within the hour.
> Once that's signed and the first month's in, we book the build-week. First
> qualified conversation usually lands inside 14 days."

**Maybe** —
> "Take the night. I'll send a one-pager with everything we covered and the
> agreement. If you want to move ahead just sign — if not, no follow-up calls,
> no chasing. You'll know."

**No** —
> "I don't think we're the right fit and I'd rather tell you straight than
> waste both our time. Reasons: [be specific — sub-£2k deals, B2C, no ICP
> clarity, not enough capacity to handle leads, etc.]. Here's what I'd do
> instead: [recommend an alternative — see below]."

---

## After the call (5 minutes)

- [ ] Update Supabase: `status` → `proposal-sent` / `not-fit` / `no-show`
- [ ] Add `notes` with the headline of the call (one sentence)
- [ ] If yes: send the agreement + onboarding form within the hour
- [ ] If maybe: send the one-pager
- [ ] If no: send the alternative recommendation in writing
- [ ] If they're a maybe and you want to nudge: schedule a 7-day follow-up

---

# Objection handling

## "It's too expensive"

> "Compared to what? If you're comparing it to hiring an SDR, you're looking at
> £40k base + tools + management for one person who'll take three months to ramp.
> If you're comparing it to a content freelancer, you're getting a production team
> that films Premier League footballers. The price isn't the question — the question
> is whether the maths works for your deal size."

Then: ask what their average deal value is. Show the maths:
*One client at £8k retainer pays for the entire year of Tier 1.*

## "How are you different from [other agency]?"

> "Most agencies run one channel. We run three and they feed each other.
> The outbound generates booked calls; those calls become content; the content
> warms the audience for the ads. It's the same loop the big consumer brands use,
> rebuilt for B2B service firms with five-figure deals."

Then point at a specific shared client outcome on the showcase.

## "I want to think about it"

> "Cool. Few questions before you go — is the tension price, fit, or timing?"

Listen. Most "I want to think about it" is one of those three. Address whichever
they name. If they can't name it, they're not actually thinking — they're
politely declining. Treat it as a No and move on.

## "Can you guarantee meetings?"

> "No, and anyone who does is lying. What I can guarantee: the system runs daily,
> the research is real, the messages are personalised, you get a weekly report
> with the numbers. If those numbers don't move in your favour after 60 days I'd
> tell you to stop paying us, because the system isn't fitting your market."

Month-to-month + no-lock-in is your guarantee. Keep saying it.

## "Send me a proposal first"

> "I will, but I want to flag — if I send a deck without us having had this
> conversation, the proposal will be generic. The 20 minutes is what makes
> everything I send useful. If you've got 20 minutes this week, I'll have
> a proposal in your inbox by tomorrow that actually fits your business."

If they still won't book, send a generic deck and write them off as low-intent.

## "We tried outbound before and it didn't work"

> "What was the reply rate? And was it templates or research-based? Almost every
> outbound effort that fails is template-based at scale. The maths on personalised
> outbound is different — fewer messages, much higher reply rate. Want me to walk
> you through the difference?"

Then go to the intelligence-report demo (Show 2 above).

## "I need to talk to my partner / co-founder"

Legitimate. Two options:

> "Want to book a follow-up with both of you? I'd rather have the same conversation
> with both of you than try to relay it through you. What's their week look like?"

Or:

> "Send me their email and I'll send the recap of what we covered. If they want
> to chat, they can grab a slot directly."

## "I don't have time for this right now"

> "Honest answer: outbound is a three-hour-a-month thing for the client.
> Onboarding is a one-hour intake form and a half-day content shoot in week one.
> If you don't have those hours in the next two weeks, this isn't the right time.
> I'd rather you book in a month when you do."

Then schedule the follow-up explicitly. Don't leave it as "ping me later" —
that's where leads go to die.

---

# "Not a fit" — alternative recommendations

When you tell someone no, give them somewhere to go. It costs nothing and
they'll remember.

| Reason they're not a fit | Where to send them |
|---|---|
| **Deal value under £2k** | Tell them outbound economics don't work; suggest building a referral programme + investing in SEO content. Mention you'll revisit if their offer evolves. |
| **B2C only** | Recommend a paid-ads-first agency or a creator agency. Explicitly say Innovite is wrong for them and you don't want to fake it. |
| **No clear ICP** | Tell them to spend the next 30 days mapping who their five best clients are before any agency can help them. Offer the £750 audit day as a one-off if they want help structuring that thinking. |
| **No sales capacity** | Tell them they don't have a lead-gen problem, they have a closing/operations problem. Recommend they fix that first or hire a fractional sales lead. |
| **Just exploring, not buying for 6+ months** | Tell them honestly to come back when timing's right. Don't add to nurture sequences. |
| **Bad culture fit on the call** | Tell them you're at capacity and refer to a competitor. (You don't owe a stranger an honest reason; you do owe them a respectful no.) |

---

# After three months: what good looks like

A call you've handled well will produce one of these outcomes within 7 days:

- ✅ Agreement signed, first month paid, onboarding kicked off
- ✅ A clear no with mutual respect — they tell their network
- ❌ Ghosted — re-examine: did you ask Q3 (capacity to close) properly?
- ❌ "Send me more info" with no follow-through — your pitch was too soft

Track close rate by tier (Hot / Warm / Cold) in Supabase. Aim for:

- Hot lead close rate: > 50%
- Warm lead close rate: > 20%
- Cold lead close rate: > 5% (mostly these should be filtered before the call)
