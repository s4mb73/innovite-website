# Innovite Website

## What this is
Marketing website for Innovite, an AI lead generation agency for B2B service firms in the UK.
- Production target: `https://innoviteai.com` (custom domain, pending DNS)
- Vercel deployment: `https://innovite-website.vercel.app` (active)
- Repo: `github.com/s4mb73/innovite-website`

## Tech stack
- Static HTML/CSS/JS — no framework
- Vercel for hosting + serverless functions
- Vercel serverless function `api/submit.js` for form submissions (Node 18+, native fetch, no deps)
- Supabase Postgres for `leads` table (env vars `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`)
- Resend for transactional email — founder notification + AI auto-response (`RESEND_API_KEY`, `FROM_EMAIL`, `NOTIFY_EMAIL`)
- Anthropic Claude Haiku for AI auto-response copy (`ANTHROPIC_API_KEY`)
- Slack incoming webhook for new-lead pings (`SLACK_WEBHOOK_URL`)
- Plausible for analytics (currently `data-domain="innoviteai.com"`)
- Wistia for showcase videos (lazy-loaded via IntersectionObserver)
- Google Fonts: Outfit (300-700) + Newsreader (400, 500, italic 400)

## File layout
- `index.html` — single-page marketing site (markup only, ~520 lines)
- `styles.css` — all CSS (~360 lines)
- `app.js` — all JS (~350 lines, loaded with `defer`)
- `api/submit.js` — Vercel serverless function for form submission
- `vercel.json` — security headers + cache rules
- `robots.txt`, `sitemap.xml`
- `privacy.html`, `terms.html`
- `docs/` — call script, LinkedIn launch pack, etc.
- `SETUP.md` — step-by-step backend wiring for Supabase + Resend + Slack + Anthropic
- `.env.example` — env-var template

## Design system (actual values from styles.css — do not invent)

### Colours
- `--bg: #0b0c11` — page background
- `--s1: #14151d` — surface 1 (cards on background)
- `--s2: #1a1c25` — surface 2 (cards inside surface 1, or hover state)
- `--s3: #242732` — surface 3 (input backgrounds, etc.)
- `--border: rgba(255,255,255,.05)` — primary border
- `--b2: rgba(255,255,255,.09)` — secondary border (more visible)
- `--t1: #ecedf3` — text primary (bright)
- `--t2: #9a9da8` — text secondary (passes WCAG AA)
- `--t3: #5e6069` — text tertiary (eyebrow labels, captions)
- `--accent: #3d7cf5` — single accent (blue)
- `--accent-h: #5a94ff` — accent hover
- `--accent-bg: rgba(61,124,245,.08)` — accent tint
- `--green: #3ecf8e` — success/positive
- `--orange: #e8a43a` — warning (used sparingly)

### Type scale
- Hero `<h1>`: clamp(38px, 5vw, 58px) Newsreader 400, line-height 1.1
- Section `<h2>` (`.sh`): clamp(26px, 3.5vw, 40px) Newsreader 400
- CTA `<h2>`: clamp(28px, 4vw, 42px) Newsreader 400
- Body: 15-16px Outfit 300-400, line-height 1.65
- Eyebrow labels: 11.5px uppercase, letter-spacing 1px
- Min font size on the page: **11.5px** (do not go below this)

### Radius tokens
- `--r: 10px` — small (buttons, inputs)
- `--rlg: 16px` — cards, modals
- `--rxl: 24px` — CTA block

### Easing
- `--ease: cubic-bezier(.2,.6,.2,1)` — quiet, no overshoot

## Anti-patterns — never ship these
1. **Purple gradients** anywhere
2. **Cards inside cards** — surfaces are flat, never nested
3. **Bounce / elastic CSS animations** — use `var(--ease)` only
4. **Film grain or noise overlays**
5. **Pure grey** — all neutrals are blue-tinted (see palette)
6. **Inter or system fonts** — Outfit + Newsreader only
7. **Body text below 12px** (eyebrow labels can be 11.5px)
8. **Generic unicode dingbats as icons** — use inline SVGs (see Services section for the pattern)
9. **Multiple sections with identical layouts** — Services is a card grid, Process is a timeline (`.tline`/`.tstep`), Who is a directory list (`.wlist`/`.wrow`). Don't collapse these back into card grids.
10. **Fake dashboard cards with stylised numbers** — the hero `.hc` card uses real ROCA Week 3 data with explicit "Real data from a live client engagement" caption.

## Copy rules
- UK English (`<html lang="en-GB">`). Use British spellings: organise, personalise, optimise, analyse.
- Voice: confident founder, direct, anti-fluff. Short sentences. Concrete > abstract.
- **Banned words**: leverage, utilise, streamline, solutions, cutting-edge, innovative, transform, revolutionise, synergy, ecosystem, empower, game-changer, best-in-class, hyper-personalised
- **No exclamation marks**
- **No emoji** in body copy or LinkedIn posts
- Service pillars are canonical: **AI Outbound / Content / Paid ads** (don't drift to "Outbound Marketing" / "Content Production" / "Paid Advertising")
- Deal-value range is **£2k floor to £15k+ ceiling** with hero copy framing it as a range, not a badge
- Pricing tiers: **£1,500 / £2,500 / £3,500** retainers + **£750** Audit Day
- Reply rate stat: **12.4%** (peak, across 6 client campaigns) — never round to 12%

## Clients in showcase (in order)
1. **CIC Event** — Corporate events, London — content-led
2. **ROCA Accountants** — Accountancy, Manchester — full system, headline case study
3. **Evinco** — Strategy consulting — outbound-led
4. **Advantedge** — Professional services, London — full system

ROCA is the only one with concrete metrics on the public site (3x pipeline, 60 days, 0 hrs prospecting).

## Key people
- **Sammy Bimpson** — founder, runs Vidora Media (the content production company that films KSI + Premier League footballers)
- **Louis** — outbound operations (mentioned in internal docs but not on the public site)

## Quality bar
Every change should hold up next to `linear.app`, `vercel.com`, `stripe.com`. If it doesn't, redo it before shipping. The standard isn't "does it work" — it's "would a paying B2B client at £3,500/mo be impressed."

## Before completing any task
Ask:
1. Does it match the existing design system tokens (no new colours, no new radii)?
2. Have I introduced any of the 10 anti-patterns above?
3. Does the copy follow the voice rules (no banned words, no emoji, no exclamation marks)?
4. If I changed CSS, does it respect `prefers-reduced-motion`?
5. If I changed markup, does the lazy-loader still pick up `.showcase-video` cards?
6. If I changed the form, does `api/submit.js` still receive the field shape it expects?
7. Have I committed and pushed with a clear message?

If any answer is wrong, fix it before reporting done.
