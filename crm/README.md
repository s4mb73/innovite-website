# Innovite CRM

Internal lead-management + outreach platform for the Innovite agency.
Lives inside the marketing-site repo for convenience; deploys separately.

## What's here today

```
crm/
  migrations/
    0001_init.sql   ← schema for the `crm` schema in Supabase (Step 1)
  README.md
```

Flask app, templates, static files arrive in Step 2.

## Stack

- **Database**: Supabase Postgres, schema `crm`. Same project as the marketing site (`api/submit.js` → `public.leads`).
- **Backend** (TBD): Python 3.11 + Flask, served on port 8080.
- **Frontend**: server-rendered Jinja templates + small JS islands. Chart.js via CDN for the dashboard.
- **AI**: Claude API for reply sentiment + email drafting.
- **Email**: Zoho SMTP (send) + IMAP (poll for replies).

## Applying migrations

Migrations are plain `.sql` files, applied in order via the Supabase SQL editor.

1. Open Supabase dashboard → SQL Editor → New query.
2. Paste the contents of the next un-applied migration.
3. Run.
4. After `0001_init.sql`, go to Project Settings → API → **Exposed schemas** and add `crm` to the list. Save.

Each migration is idempotent — re-running it on an up-to-date database is a no-op.

## Schema overview

```
crm.clients          (active client roster — Vidora, ROCA, future)
   ↓ 1:many
crm.leads            (deeply enriched outbound targets per client)
   ↓ 1:many
crm.emails           (scheduled / sent outbound messages, Day 1/3/7)
   ↓ 1:many
crm.replies          (inbound responses to our outreach)

crm.inbound_leads    (innovite.io form submissions — see note in 0001)
crm.activity_log     (audit trail across the whole system)
```

## Open items / known overlaps

- **`crm.inbound_leads` vs `public.leads`** — the marketing site's `api/submit.js` already writes form submissions to `public.leads`. The spec defines a parallel table in the CRM schema. For now both exist; the Inbound page will read from `public.leads` until we consolidate. To resolve: either repoint `api/submit.js` to `crm.inbound_leads`, or add a Postgres trigger that mirrors inserts.
- **Auth**: POC = single user (Sammy), service_role key in the Flask env. Multi-user via Supabase Auth comes later.
- **Hosting**: TBD. Spec says Windows VPS port 8080.
- **Scheduler / email / PDF / reply-engine**: deferred.
