# Innovite — backend setup

Step-by-step to wire the website form into a real database, plus the
optional integrations (Slack, founder email, AI auto-response).

Each integration is independent — pick what to wire up tomorrow.

---

## Item #1 — Form persists to Supabase (REQUIRED)

### 1. Create the table

Supabase dashboard → **SQL Editor** → **New query**, paste:

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
  qualifier_q1 text,
  qualifier_q2 text,
  qualifier_q3 text,
  qualifier_q4 text,
  qualifier_score int,
  tier text,           -- 'hot' | 'warm' | 'cold' | 'no-fit'
  raw_data jsonb,
  user_agent text,
  ip text,
  status text not null default 'new',
  notes text
);

create index leads_created_at_idx on public.leads (created_at desc);
create index leads_status_idx on public.leads (status);
create index leads_tier_idx on public.leads (tier);

-- Lock the table down — only the service role can read/write it.
alter table public.leads enable row level security;
```

> Already ran the v1 SQL? Apply this migration instead:
> ```sql
> alter table public.leads
>   add column if not exists qualifier_q1 text,
>   add column if not exists qualifier_q2 text,
>   add column if not exists qualifier_q3 text,
>   add column if not exists qualifier_q4 text,
>   add column if not exists qualifier_score int,
>   add column if not exists tier text;
> create index if not exists leads_tier_idx on public.leads (tier);
> ```

### 2. Vercel env vars

Vercel project → **Settings → Environment Variables**, add to Production + Preview + Development:

| Key | Value |
|---|---|
| `SUPABASE_URL` | Supabase Settings → API → Project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase Settings → API → `service_role` secret (click "Reveal") |

> ⚠️ The service role key bypasses RLS. Treat it like a database password — Vercel env vars only, never in the repo or client.

Trigger a redeploy after saving (Deployments → ⋯ → Redeploy) so the new env reaches the function.

### 3. Test

Submit the form on the live site → check `leads` table in Supabase. The new row should have all four step answers, name/email/company/phone, `source='website-form'`, `status='new'`.

---

## Item #2a — Slack notifications

Pings a rich block in Slack on every new lead, colour-coded by tier.

1. Slack workspace → **Apps → Custom Integrations → Incoming Webhooks** → **Add to Slack** → pick a channel (`#innovite-leads`).
2. Copy the **Webhook URL**.
3. Add to Vercel env: `SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../xxx`
4. Redeploy → submit a test → check Slack.

If you use **Discord** instead, the same payload works against a Discord webhook URL ending in `/slack` — see https://discord.com/developers/docs/resources/webhook#execute-slackcompatible-webhook.

---

## Item #2b — Founder notification email + AI auto-response (Resend)

One Resend account powers both:
- An email to **you** (full lead data, ready to triage)
- An AI-personalised email to **the lead** (within seconds, with Calendly link)

### 1. Create a Resend account

1. https://resend.com → sign up (free tier covers 100 emails/day, 3,000/month).
2. **Domains → Add Domain** → `innoviteai.com` → follow the DNS steps (3 records on your registrar).
3. Wait for verification (5–30 min).

> If DNS verification feels heavy for day-one, skip step 2 and use Resend's test sender: `FROM_EMAIL="Innovite <onboarding@resend.dev>"`. Auto-responses will land but may go to spam — fine for testing, not for production.

### 2. Vercel env vars

| Key | Example value |
|---|---|
| `RESEND_API_KEY` | `re_xxx...` (Resend dashboard → API Keys) |
| `FROM_EMAIL` | `Sammy from Innovite <hello@innoviteai.com>` |
| `NOTIFY_EMAIL` | `sammy@innoviteai.com` |

### 3. Test

Submit the form with a real email you can check. Within ~10 seconds you should get **two** emails:
- Founder notification at `NOTIFY_EMAIL` with all form data
- Auto-response at the lead's email with the Calendly link

---

## Item #2c — AI-personalised copy (Anthropic)

Without this, leads still get a clean templated email. With it, every email is tailored to their industry and deal value.

1. https://console.anthropic.com → **API Keys** → **Create Key**.
2. Add to Vercel env: `ANTHROPIC_API_KEY=sk-ant-...`
3. Redeploy. Cost: ~£0.001 per lead with Claude Haiku.

---

## Item #2d — Calendly link

The auto-response includes a Calendly URL. Default placeholder is `https://calendly.com/innovite/strategy-call`. Override it once you've created the real link:

| Key | Value |
|---|---|
| `CALENDLY_URL` | Your real Calendly event link |

---

## Debugging

If submit shows "Something went wrong":
- Vercel project → **Logs** (or **Functions → /api/submit → Logs**)
- Watch for: `Missing Supabase env vars`, `Supabase insert failed 4xx`, `Anthropic ...`, `Resend lead/founder ...`

Side-effect failures (Slack/email/AI) are logged but do **not** fail the submission. The lead always lands in Supabase as long as the DB write succeeded.

---

## Not yet built

- ❌ CRM dashboard to view/manage leads (Item #5)
- ❌ Automated weekly client reports (Item #6)
- ❌ Pricing page or proposal template (Item #7)
- ❌ Referral programme (Item #8)
