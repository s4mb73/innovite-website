// POST /api/waitlist
// Receives the 4-step waitlist form, validates, posts to Slack and/or Resend if
// configured. Each integration is optional — missing env vars no-op that piece
// without failing the request. The form returns 200 as long as input validates.
//
// Optional env:
//   SLACK_WEBHOOK_URL   — Slack notification on every new signup
//   RESEND_API_KEY      — required for any email
//   FROM_EMAIL          — e.g. "Stockwise <hello@stockwise.app>"
//   NOTIFY_EMAIL        — founder-facing notification recipient

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  let body = req.body;
  if (typeof body === 'string') {
    try { body = JSON.parse(body); } catch { return res.status(400).json({ error: 'Invalid JSON' }); }
  }
  if (!body || typeof body !== 'object') return res.status(400).json({ error: 'Missing body' });

  const trim = (v, max = 200) => String(v || '').trim().slice(0, max);
  const name    = trim(body.name);
  const email   = trim(body.email).toLowerCase();
  const company = trim(body.company);
  const volume  = trim(body.s1, 80);
  const tool    = trim(body.s2, 80);
  const market  = trim(body.s3, 80);

  if (!name) return res.status(400).json({ error: 'Name is required' });
  if (!EMAIL_RE.test(email)) return res.status(400).json({ error: 'Valid email is required' });

  const lead = {
    source: 'stockwise-waitlist',
    name, email, company,
    volume, tool, market,
    user_agent: (req.headers['user-agent'] || '').slice(0, 500),
    ip: (req.headers['x-forwarded-for'] || '').split(',')[0].trim().slice(0, 64) || null,
    at: new Date().toISOString()
  };

  console.log('[waitlist] new signup', lead);

  const sideEffects = await Promise.allSettled([
    notifySlack(lead),
    notifyFounder(lead)
  ]);
  sideEffects.forEach((r, i) => {
    if (r.status === 'rejected') {
      console.error(`[side-effect ${['slack','founder'][i]}]`, r.reason);
    }
  });

  return res.status(200).json({ ok: true });
}

async function notifySlack(lead) {
  const url = process.env.SLACK_WEBHOOK_URL;
  if (!url) return;

  const text = `New Stockwise waitlist signup — ${lead.name}${lead.company ? ` (${lead.company})` : ''}`;
  const blocks = [
    { type:'header', text:{ type:'plain_text', text } },
    { type:'section', fields: [
      { type:'mrkdwn', text:`*Email*\n${lead.email}` },
      { type:'mrkdwn', text:`*Company*\n${lead.company || '—'}` },
      { type:'mrkdwn', text:`*Volume*\n${lead.volume || '—'}` },
      { type:'mrkdwn', text:`*Current tool*\n${lead.tool || '—'}` },
      { type:'mrkdwn', text:`*Main marketplace*\n${lead.market || '—'}` }
    ] }
  ];

  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, blocks })
  });
  if (!r.ok) throw new Error(`Slack ${r.status}: ${await r.text()}`);
}

async function notifyFounder(lead) {
  const apiKey = process.env.RESEND_API_KEY;
  const from   = process.env.FROM_EMAIL;
  const to     = process.env.NOTIFY_EMAIL;
  if (!apiKey || !from || !to) return;

  const subject = `New waitlist signup: ${lead.name}${lead.company ? ` — ${lead.company}` : ''}`;
  const html = `
    <div style="font-family:system-ui,sans-serif;font-size:14px;color:#222;line-height:1.5;max-width:560px">
      <h2 style="margin:0 0 12px;font-size:18px">New Stockwise waitlist signup</h2>
      <table cellpadding="6" style="border-collapse:collapse;width:100%;background:#fafafa;border-radius:6px">
        <tr><td><strong>Name</strong></td><td>${esc(lead.name)}</td></tr>
        <tr><td><strong>Email</strong></td><td><a href="mailto:${esc(lead.email)}">${esc(lead.email)}</a></td></tr>
        <tr><td><strong>Company</strong></td><td>${esc(lead.company) || '—'}</td></tr>
        <tr><td><strong>Volume</strong></td><td>${esc(lead.volume) || '—'}</td></tr>
        <tr><td><strong>Current tool</strong></td><td>${esc(lead.tool) || '—'}</td></tr>
        <tr><td><strong>Main marketplace</strong></td><td>${esc(lead.market) || '—'}</td></tr>
      </table>
    </div>`;

  const r = await fetch('https://api.resend.com/emails', {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ from, to, subject, html, reply_to: lead.email })
  });
  if (!r.ok) throw new Error(`Resend ${r.status}: ${await r.text()}`);
}

function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, c =>
    ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));
}
