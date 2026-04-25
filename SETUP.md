# Innovite — backend setup

Step-by-step to wire the website form into a real database.
Time required: ~15 minutes.

## 1. Create a Supabase project

1. Go to https://supabase.com and sign in.
2. Click **New project**. Pick the free tier.
3. Region: London (eu-west-2) for UK GDPR + lowest latency.
4. Save the database password somewhere safe.
5. Wait ~2 minutes for the project to provision.

## 2. Create the `leads` table

In the Supabase dashboard → **SQL Editor** → **New query**, paste:

```sql
create table public.leads (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  source text not null,
  name text not null,
  email text not null,
  company text,
  phone text,
  industry text,
  deal_value text,
  current_method text,
  target_clients text,
  raw_data jsonb,
  user_agent text,
  ip text,
  -- pipeline status (used later by the CRM dashboard)
  status text not null default 'new',
  notes text
);

create index leads_created_at_idx on public.leads (created_at desc);
create index leads_status_idx on public.leads (status);

-- Lock the table down — only the service role can read/write it.
-- The website form uses the service role via the Vercel function;
-- no client-side access ever.
alter table public.leads enable row level security;
```

Hit **Run**. You should see "Success. No rows returned."

## 3. Grab your credentials

Supabase dashboard → **Settings** → **API**:

- **Project URL** → copy the `https://xxxxx.supabase.co` value
- **service_role secret** → click "Reveal" and copy it

> ⚠️ The service role key bypasses RLS. Treat it like a database password.
> Only put it in Vercel env vars, never in the repo or client code.

## 4. Add env vars to Vercel

1. Go to your Vercel project → **Settings** → **Environment Variables**.
2. Add two variables (Production + Preview + Development scopes):
   - `SUPABASE_URL` → the Project URL from step 3
   - `SUPABASE_SERVICE_ROLE_KEY` → the service_role secret from step 3
3. Click **Save**.
4. Trigger a redeploy (Deployments → ⋯ → Redeploy) so the env vars take effect.

## 5. Test it

Open your live site, click **Book a strategy call**, complete the 5 steps and submit.

In Supabase → **Table editor** → `leads`, you should see the new row with all the form data, including the raw payload.

If submit shows "Something went wrong":
- Vercel project → **Logs** → look for `Submit handler error` or `Supabase insert failed`.
- Check the env vars are set on the same environment Vercel is serving (preview vs production).

## What this does NOT do (yet)

- ❌ AI auto-response email to the lead
- ❌ Slack notification to Sammy
- ❌ Founder notification email
- ❌ CRM dashboard to view/manage leads

These are Items #2 and #5 in the build plan and need their own session each.
For now leads land in the database — you can view them in the Supabase Table editor and manually email people back.
