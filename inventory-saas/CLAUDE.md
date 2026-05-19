# Stockwise

## What this is
Marketing site (and eventually app) for **Stockwise** — inventory OS for ticket brokers.
- One inventory, every marketplace: StubHub, SeatGeek, Vivid Seats, Ticketmaster, TickPick, AXS, Gametime, TicketNetwork, Tixstar, Ticombo.
- Currently a static single-page marketing site with a working waitlist form. Private beta — no public app yet.
- Production target: `https://stockwise.app` (Vercel-hosted)
- Repo: `github.com/s4mb73/stockwise`

## Audience
**Ticket brokers** — secondary-market resellers. Primary persona is a broker running 2,000–10,000 active listings across 4+ marketplaces, currently on SkyBox or a spreadsheet. They pay $500–$3,000/month for tooling today and they hate every minute of it.

Competitor context: SkyBox (Vivid-owned, dominant), Brokergenius / AutoProcessor (pricing only), Eventiny, in-house tools. Frame against SkyBox by default.

## Tech stack
- Static HTML / CSS / JS (no framework)
- Vercel for hosting + serverless functions
- `api/waitlist.js` — Node 18+, native `fetch`, zero deps. Validates + fans out to optional Slack + Resend.
- Optional integrations (each unlocks independently):
  - Slack webhook (`SLACK_WEBHOOK_URL`) — new-signup pings
  - Resend (`RESEND_API_KEY`, `FROM_EMAIL`, `NOTIFY_EMAIL`) — founder notification email
- Google Fonts: **Outfit** (300–700) + **Newsreader** (400, 500, italic 400)

## File layout
- `index.html` — single-page marketing site
- `styles.css` — all CSS, organized by section
- `app.js` — nav, reveal observer, FAQ accordion, 4-step waitlist modal
- `api/waitlist.js` — Vercel serverless function for form submissions
- `vercel.json` — security headers + cache rules
- `robots.txt`
- `.env.example` — env-var template

## Design system (do not invent new values)

### Colors
- `--bg: #0b0c11` — page background
- `--s1: #14151d` — surface 1 (cards on background)
- `--s2: #1a1c25` — surface 2 (hover state, nested surface)
- `--s3: #242732` — surface 3 (inputs, tags)
- `--border: rgba(255,255,255,.05)` — primary
- `--b2: rgba(255,255,255,.09)` — secondary border
- `--t1: #ecedf3` — text primary
- `--t2: #9a9da8` — text secondary (passes WCAG AA)
- `--t3: #5e6069` — text tertiary (eyebrow labels, captions)
- `--accent: #8FB7FF` — single accent (cool blue)
- `--accent-h: #a8c8ff` — accent hover
- `--accent-bg: rgba(143,183,255,.10)` — accent tint
- `--green: #3ecf8e` — success
- `--orange: #e8a43a` — warning (used sparingly)

### Type scale
- Hero `<h1>`: clamp(38px, 5vw, 58px) Newsreader 400, line-height 1.1
- Section `<h2>` (`.sh`): clamp(26px, 3.5vw, 40px) Newsreader 400
- CTA `<h2>`: clamp(28px, 4vw, 42px) Newsreader 400
- Body: 13–16px Outfit 300–400, line-height 1.6
- Eyebrow labels: 11.5px uppercase, letter-spacing 1px
- Min font size: **11.5px**

### Radius tokens
- `--r: 10px` — small (buttons, inputs)
- `--rlg: 16px` — cards
- `--rxl: 24px` — CTA block

### Easing
- `--ease: cubic-bezier(.2,.6,.2,1)` — quiet, no overshoot

## Anti-patterns — never ship these
1. **Purple gradients** anywhere
2. **Cards inside cards** — surfaces stay flat
3. **Bounce / elastic animations** — use `var(--ease)` only
4. **Film grain or noise overlays**
5. **Pure grey** — all neutrals are blue-tinted (see palette)
6. **Inter or system fonts** — Outfit + Newsreader only
7. **Body text below 12px** (eyebrow labels can be 11.5px)
8. **Generic unicode dingbats as icons** — use inline SVGs (see Features section for the pattern: stroke-width 16 on 256×256 viewBox)
9. **Multiple sections with identical layouts** — Problem and Features are both card grids and that's the max; How-it-works is a timeline (`.tline` / `.tstep`), Who-it's-for is a directory list (`.wlist` / `.wrow`). Don't collapse these into a third grid.
10. **Fabricated dashboard data with implausible numbers** — the hero `.lp` card and `.pp` platform preview both carry "Example dashboard — private beta data" / "Beta access only" captions. Be honest about what's real and what's mock.

## Copy rules
- US English (`<html lang="en-US">`). Use US spellings: organize, personalize, optimize, analyze.
- Voice: direct, operator-to-operator, no agency-speak. Short sentences. Concrete > abstract. Contractions normal.
- **Banned words**: leverage, utilize, streamline, solutions, cutting-edge, innovative, transform, revolutionize, synergy, ecosystem, empower, game-changer, best-in-class, hyper-personalized, robust, seamless, world-class
- **No exclamation marks**
- **No emoji** in body copy, marketing, or LinkedIn posts
- **Talk like a broker, not a SaaS founder.** Specifics over abstractions. "Cut double-sells to zero" beats "improve accuracy." Reference real venues, real events, real marketplaces.
- Marketplace names are canonical: StubHub, SeatGeek, Vivid Seats, Ticketmaster, TickPick, AXS, Gametime, TicketNetwork, Tixstar, Ticombo. Don't shorten or abbreviate.
- Pricing: **don't quote numbers on the public site until beta closes.** Direct readers to the waitlist instead. Internally we're aiming at flat-rate monthly priced against the SkyBox tier the broker is on.

## Industry vocabulary (use these, don't invent synonyms)
- **Listing** (not "ticket entry")
- **Active listings** (not "open inventory")
- **Spec / speculation** (selling tickets you haven't taken delivery on)
- **Float** / **open position** (spec inventory not yet covered)
- **Footprint** (a seller's marketplace presence — multi-account brokers manage multiple footprints to spread risk)
- **Comps** (live comparable listings used for pricing)
- **AutoPrice** (the engine — capitalized as a product name)
- **Double-sell** (selling the same seat twice across marketplaces — the cardinal sin)
- **Fulfillment** (delivering the actual tickets — mobile transfer, PDF, hard ticket)
- **POS** (point-of-sale system — usually means SkyBox in this market)

## Quality bar
Every change should hold up next to `linear.app`, `vercel.com`, `stripe.com`. If it doesn't, redo it before shipping. The standard isn't "does it work" — it's "would a broker doing $5M/yr in volume look at this and trust us with their inventory."

## Before completing any task
Ask:
1. Does it match the existing design tokens (no new colors, no new radii)?
2. Have I introduced any of the 10 anti-patterns above?
3. Does the copy follow the voice rules (no banned words, no emoji, no exclamation marks, US English)?
4. Did I use the right broker vocabulary (listing not "ticket entry", spec not "pre-sale")?
5. If I changed CSS, does it respect `prefers-reduced-motion`?
6. If I changed the form, does `api/waitlist.js` still receive the field shape it expects (`name`, `email`, `company`, `s1`, `s2`, `s3`)?
7. Have I committed with a clear message?

If any answer is wrong, fix it before reporting done.

## Reference site
The design language was lifted from `innovite.io` (same founder). When in doubt about a pattern, that site is the visual reference — but never copy its **copy**, which is for a different product and audience.
