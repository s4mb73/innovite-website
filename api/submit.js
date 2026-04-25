// POST /api/submit
// Receives the 5-step form payload (+ optional qualifier answers), validates,
// saves to Supabase `leads`, then in parallel:
//   - computes Hot/Warm/Cold tier from qualifier
//   - posts a Slack notification
//   - emails the founder a notification
//   - generates an AI-personalised auto-response and emails it to the lead
//
// Each integration is independently optional — missing env vars no-op that
// piece without breaking the others. The form always returns 200 once the
// lead is in the database; side-effect failures are logged, never fatal.
//
// Required env (form persistence will fail without these):
//   SUPABASE_URL
//   SUPABASE_SERVICE_ROLE_KEY
//
// Optional env (each unlocks a piece of Item #2):
//   SLACK_WEBHOOK_URL          — Slack notification on every new lead
//   RESEND_API_KEY             — sending email
//   FROM_EMAIL                 — e.g. "Sammy from Innovite <hello@innoviteai.com>"
//   NOTIFY_EMAIL               — founder-facing notification recipient
//   ANTHROPIC_API_KEY          — AI-personalised auto-response copy
//   CALENDLY_URL               — link surfaced in the auto-response

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const supaUrl = process.env.SUPABASE_URL;
  const supaKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!supaUrl || !supaKey) {
    console.error('Missing Supabase env vars');
    return res.status(500).json({ error: 'Server not configured' });
  }

  let body = req.body;
  if (typeof body === 'string') {
    try { body = JSON.parse(body); } catch { return res.status(400).json({ error: 'Invalid JSON' }); }
  }
  if (!body || typeof body !== 'object') return res.status(400).json({ error: 'Missing body' });

  // ── Validate + normalise -----------------------------------------------
  const trim = (v, max = 200) => String(v || '').trim().slice(0, max);
  const name           = trim(body.name);
  const email          = trim(body.email).toLowerCase();
  const company        = trim(body.company);
  const phone          = trim(body.phone, 50);
  const industry       = trim(body.s1);
  const dealValue      = trim(body.s2, 50);
  const currentMethod  = trim(body.s3);
  const targetClients  = trim(body.s4, 50);

  if (!name) return res.status(400).json({ error: 'Name is required' });
  if (!EMAIL_RE.test(email)) return res.status(400).json({ error: 'Valid email is required' });

  // ── Score qualifier (mirror of qScore() in app.js so server is the authority)
  const qa = body.qualifier && typeof body.qualifier === 'object' ? body.qualifier : null;
  const { tier, score } = scoreQualifier(qa);

  // ── Insert lead ---------------------------------------------------------
  const lead = {
    source: 'website-form',
    name, email,
    company: company || null,
    phone: phone || null,
    industry: industry || null,
    deal_value: dealValue || null,
    current_method: currentMethod || null,
    target_clients: targetClients || null,
    qualifier_q1: qa?.q1 || null,
    qualifier_q2: qa?.q2 || null,
    qualifier_q3: qa?.q3 || null,
    qualifier_q4: qa?.q4 || null,
    qualifier_score: score,
    tier,
    raw_data: body,
    user_agent: (req.headers['user-agent'] || '').slice(0, 500),
    ip: (req.headers['x-forwarded-for'] || '').split(',')[0].trim().slice(0, 64) || null
  };

  let savedId;
  try {
    const dbRes = await fetch(`${supaUrl}/rest/v1/leads`, {
      method: 'POST',
      headers: {
        'apikey': supaKey,
        'Authorization': `Bearer ${supaKey}`,
        'Content-Type': 'application/json',
        'Prefer': 'return=representation'
      },
      body: JSON.stringify(lead)
    });
    if (!dbRes.ok) {
      console.error('Supabase insert failed', dbRes.status, await dbRes.text());
      return res.status(502).json({ error: 'Could not save lead' });
    }
    const [saved] = await dbRes.json();
    savedId = saved?.id;
  } catch (err) {
    console.error('Submit handler DB error', err);
    return res.status(500).json({ error: 'Unexpected server error' });
  }

  // ── Side effects (all best-effort, parallel) ---------------------------
  const enriched = { ...lead, id: savedId };
  const sideEffects = await Promise.allSettled([
    notifySlack(enriched),
    notifyFounder(enriched),
    sendAutoResponse(enriched)
  ]);
  sideEffects.forEach((r, i) => {
    if (r.status === 'rejected') {
      console.error(`[side-effect ${['slack','founder','auto-response'][i]}]`, r.reason);
    }
  });

  return res.status(200).json({ ok: true, id: savedId, tier });
}

// ── Qualifier scoring (mirrors app.js qScore) ----------------------------
function scoreQualifier(qa) {
  if (!qa) return { tier: null, score: null };
  let score = 0, hardNo = false;

  if (qa.q1 === 'b2c') hardNo = true;
  else if (qa.q1 === 'mix') score += 15;
  else if (qa.q1 === 'b2b') score += 30;

  if (qa.q2 === 'sub2k') hardNo = true;
  else if (qa.q2 === '2to5k') score += 8;
  else if (qa.q2 === '5to15k') score += 22;
  else if (qa.q2 === '15kplus') score += 30;

  if (qa.q3 === 'yes') score += 20;
  else if (qa.q3 === 'partner') score += 14;
  else if (qa.q3 === 'no') score += 5;

  if (qa.q4 === 'now') score += 20;
  else if (qa.q4 === 'soon') score += 14;
  else if (qa.q4 === 'exploring') score += 5;

  let tier;
  if (hardNo) tier = 'no-fit';
  else if (score >= 70) tier = 'hot';
  else if (score >= 45) tier = 'warm';
  else tier = 'cold';

  return { tier, score };
}

// ── Slack notification ---------------------------------------------------
async function notifySlack(lead) {
  const url = process.env.SLACK_WEBHOOK_URL;
  if (!url) return;

  const tierEmoji = { hot:'🔥', warm:'🟡', cold:'❄️', 'no-fit':'⛔' }[lead.tier] || '🆕';
  const tierLine = lead.tier ? `${tierEmoji} *${lead.tier.toUpperCase()}* (score ${lead.qualifier_score ?? '—'})` : '🆕 No qualifier';

  const text = `New lead — ${lead.name}${lead.company ? ` (${lead.company})` : ''}`;
  const blocks = [
    { type:'header', text:{ type:'plain_text', text:`New lead — ${lead.name}` } },
    { type:'section', text:{ type:'mrkdwn', text: tierLine } },
    { type:'section', fields: [
      { type:'mrkdwn', text:`*Email*\n${lead.email}` },
      { type:'mrkdwn', text:`*Company*\n${lead.company || '—'}` },
      { type:'mrkdwn', text:`*Phone*\n${lead.phone || '—'}` },
      { type:'mrkdwn', text:`*Industry*\n${lead.industry || '—'}` },
      { type:'mrkdwn', text:`*Deal value*\n${lead.deal_value || '—'}` },
      { type:'mrkdwn', text:`*Current method*\n${lead.current_method || '—'}` },
      { type:'mrkdwn', text:`*Target/month*\n${lead.target_clients || '—'}` },
      { type:'mrkdwn', text:`*Lead ID*\n${lead.id || '—'}` }
    ] }
  ];

  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, blocks })
  });
  if (!r.ok) throw new Error(`Slack ${r.status}: ${await r.text()}`);
}

// ── Founder notification email ------------------------------------------
async function notifyFounder(lead) {
  const apiKey  = process.env.RESEND_API_KEY;
  const from    = process.env.FROM_EMAIL;
  const to      = process.env.NOTIFY_EMAIL;
  if (!apiKey || !from || !to) return;

  const tierLine = lead.tier ? `${lead.tier.toUpperCase()} (score ${lead.qualifier_score ?? '—'})` : 'No qualifier';
  const subject = `New lead: ${lead.name}${lead.company ? ` — ${lead.company}` : ''}`;
  const html = `
    <div style="font-family:system-ui,sans-serif;font-size:14px;color:#222;line-height:1.5;max-width:560px">
      <h2 style="margin:0 0 12px;font-size:18px">New website lead</h2>
      <p style="margin:0 0 16px"><strong>Tier:</strong> ${tierLine}</p>
      <table cellpadding="6" style="border-collapse:collapse;width:100%;background:#fafafa;border-radius:6px">
        <tr><td><strong>Name</strong></td><td>${esc(lead.name)}</td></tr>
        <tr><td><strong>Email</strong></td><td><a href="mailto:${esc(lead.email)}">${esc(lead.email)}</a></td></tr>
        <tr><td><strong>Company</strong></td><td>${esc(lead.company) || '—'}</td></tr>
        <tr><td><strong>Phone</strong></td><td>${esc(lead.phone) || '—'}</td></tr>
        <tr><td><strong>Industry</strong></td><td>${esc(lead.industry) || '—'}</td></tr>
        <tr><td><strong>Deal value</strong></td><td>${esc(lead.deal_value) || '—'}</td></tr>
        <tr><td><strong>Current method</strong></td><td>${esc(lead.current_method) || '—'}</td></tr>
        <tr><td><strong>Target / month</strong></td><td>${esc(lead.target_clients) || '—'}</td></tr>
        <tr><td><strong>Lead ID</strong></td><td><code>${esc(lead.id) || '—'}</code></td></tr>
      </table>
      <p style="margin:16px 0 0;color:#888;font-size:12px">Reply directly to email this lead.</p>
    </div>`;

  const r = await fetch('https://api.resend.com/emails', {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ from, to, subject, html, reply_to: lead.email })
  });
  if (!r.ok) throw new Error(`Resend founder ${r.status}: ${await r.text()}`);
}

// ── AI auto-response to the lead ----------------------------------------
async function sendAutoResponse(lead) {
  const resendKey = process.env.RESEND_API_KEY;
  const from      = process.env.FROM_EMAIL;
  if (!resendKey || !from) return;

  const calendly = process.env.CALENDLY_URL || 'https://calendly.com/innovite/strategy-call';
  const firstName = lead.name.split(/\s+/)[0];

  let subject, body;
  const ai = await generateCopyWithClaude(lead, calendly);
  if (ai) {
    subject = ai.subject;
    body = ai.body;
  } else {
    // Fallback templated copy if Anthropic key missing or call failed.
    subject = `Got it, ${firstName} — here's the next step`;
    body =
`Hi ${firstName},

Got your details. ${lead.industry ? `Have worked with a few ${lead.industry.toLowerCase()} firms before, so we'll have a useful conversation either way.` : ''}

Quick next step: a 20-minute call. We'll walk through your current pipeline, find the gaps, and tell you exactly what we'd do differently — whether you hire us or not.

Pick a slot that suits you: ${calendly}

— Sammy
Founder, Innovite
sammy@innoviteai.com`;
  }

  const html = body
    .split(/\n{2,}/)
    .map(p => `<p style="margin:0 0 14px">${esc(p).replace(/\n/g,'<br>').replace(/(https?:\/\/\S+)/g,'<a href="$1" style="color:#3d7cf5">$1</a>')}</p>`)
    .join('');

  const wrapped = `<div style="font-family:system-ui,sans-serif;font-size:15px;color:#222;line-height:1.55;max-width:560px">${html}</div>`;

  const r = await fetch('https://api.resend.com/emails', {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${resendKey}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      from,
      to: lead.email,
      subject,
      html: wrapped,
      reply_to: process.env.NOTIFY_EMAIL || undefined
    })
  });
  if (!r.ok) throw new Error(`Resend lead ${r.status}: ${await r.text()}`);
}

async function generateCopyWithClaude(lead, calendly) {
  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) return null;

  const system =
`You are Sammy Bimpson, founder of Innovite, an AI lead-generation agency for B2B service firms. Write the first email a lead receives after submitting the qualifying form on innoviteai.com.

Voice: direct, UK English, no agency-speak. Short sentences. Concrete > abstract. Contractions normal. No exclamation marks. No "we're excited" or "thanks for reaching out".

Constraints:
- 90-130 words
- Greet by first name only
- Reference the lead's industry by name in one sentence
- Briefly acknowledge their deal-value tier or the channel they currently use
- Tell them what happens on the call: 20 minutes, walk through pipeline, find the gaps, leave with a plan either way
- Include the Calendly URL exactly as given, on its own line
- Sign off "— Sammy" then on a new line "Founder, Innovite"

Output JSON only with this exact shape:
{"subject":"...","body":"..."}
The subject line should be punchy, lowercase-friendly, under 60 chars, and reference their first name.`;

  const userMsg =
`Lead:
- Name: ${lead.name}
- Company: ${lead.company || 'unknown'}
- Industry: ${lead.industry || 'not stated'}
- Deal value: ${lead.deal_value || 'not stated'}
- How they find clients now: ${lead.current_method || 'not stated'}
- Clients/month they want: ${lead.target_clients || 'not stated'}
${lead.tier ? `- Qualifier tier: ${lead.tier} (score ${lead.qualifier_score})` : ''}

Calendly URL to include: ${calendly}

Write the email. Return JSON only.`;

  try {
    const ctrl = new AbortController();
    const timeout = setTimeout(() => ctrl.abort(), 8000);
    const r = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: {
        'x-api-key': apiKey,
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json'
      },
      body: JSON.stringify({
        model: 'claude-haiku-4-5-20251001',
        max_tokens: 600,
        temperature: 0.5,
        system,
        messages: [
          { role: 'user', content: userMsg },
          { role: 'assistant', content: '{' }
        ]
      }),
      signal: ctrl.signal
    });
    clearTimeout(timeout);
    if (!r.ok) {
      console.error('Anthropic', r.status, await r.text());
      return null;
    }
    const data = await r.json();
    const text = '{' + (data.content?.[0]?.text || '');
    const parsed = JSON.parse(text);
    if (!parsed.subject || !parsed.body) return null;
    return parsed;
  } catch (err) {
    console.error('Anthropic generation failed', err);
    return null;
  }
}

// ── HTML escape for safe interpolation ----------------------------------
function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, c =>
    ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));
}
