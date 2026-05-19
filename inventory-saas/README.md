# Stockwise — marketing site

Inventory OS for ticket brokers. Single-page static marketing site with a working waitlist form.

## Stack

- Static HTML / CSS / JS (no framework)
- Vercel for hosting + the `/api/waitlist` serverless function (Node 18+, native `fetch`, zero deps)
- Optional Slack + Resend for signup notifications

## Layout

- `index.html` — full single-page marketing site
- `styles.css` — design tokens + every component
- `app.js` — nav, reveal observer, FAQ accordion, 4-step waitlist modal
- `api/waitlist.js` — POST endpoint that validates and forwards to Slack / Resend
- `vercel.json` — security headers + cache rules
- `.env.example` — env-var template

## Local dev

It's pure static HTML — open `index.html` directly, or:

```bash
npx serve .
```

The `/api/waitlist` endpoint only runs under Vercel (`vercel dev`).

## Deploy

1. Push this folder to a new GitHub repo (`s4mb73/stockwise`).
2. Import the repo on Vercel.
3. Add the env vars from `.env.example` in Vercel project settings (all optional — the form returns 200 either way).
4. Done.

## Splitting from this repo

This was built inside `innovite-website` while access to a fresh repo was unavailable. To extract the folder cleanly into its own repo with history preserved:

```bash
git subtree split --prefix=inventory-saas -b stockwise
# In a fresh clone of the new repo:
git pull /path/to/innovite-website stockwise
```

Or just copy the contents of this folder into a new repo and start fresh.
