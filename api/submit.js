// POST /api/submit
// Receives the 5-step form payload, validates, saves to Supabase `leads` table.
// Item #2 (AI auto-response, Slack notification, founder email) hooks in here later.
//
// Required env vars (Vercel project → Settings → Environment Variables):
//   SUPABASE_URL                 — e.g. https://xxxxx.supabase.co
//   SUPABASE_SERVICE_ROLE_KEY    — service role key (server-only, NEVER expose to client)
//
// Native fetch is available on Vercel's Node 18+ runtime, no deps needed.

const REQUIRED_ENV = ['SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY'];
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const missingEnv = REQUIRED_ENV.filter(k => !process.env[k]);
  if (missingEnv.length) {
    console.error('Missing env vars:', missingEnv.join(', '));
    return res.status(500).json({ error: 'Server not configured' });
  }

  let body = req.body;
  if (typeof body === 'string') {
    try { body = JSON.parse(body); } catch { return res.status(400).json({ error: 'Invalid JSON' }); }
  }
  if (!body || typeof body !== 'object') {
    return res.status(400).json({ error: 'Missing body' });
  }

  const name = String(body.name || '').trim().slice(0, 200);
  const email = String(body.email || '').trim().toLowerCase().slice(0, 200);
  const company = String(body.company || '').trim().slice(0, 200);
  const phone = String(body.phone || '').trim().slice(0, 50);

  // Step answers from the 5-step form (keys s1..s4 are set by sO() in app.js)
  const industry = String(body.s1 || '').trim().slice(0, 200);
  const dealValue = String(body.s2 || '').trim().slice(0, 50);
  const currentMethod = String(body.s3 || '').trim().slice(0, 200);
  const targetClients = String(body.s4 || '').trim().slice(0, 50);

  if (!name) return res.status(400).json({ error: 'Name is required' });
  if (!EMAIL_RE.test(email)) return res.status(400).json({ error: 'Valid email is required' });

  const lead = {
    source: 'website-form',
    name,
    email,
    company: company || null,
    phone: phone || null,
    industry: industry || null,
    deal_value: dealValue || null,
    current_method: currentMethod || null,
    target_clients: targetClients || null,
    raw_data: body,
    user_agent: (req.headers['user-agent'] || '').slice(0, 500),
    ip: (req.headers['x-forwarded-for'] || '').split(',')[0].trim().slice(0, 64) || null
  };

  try {
    const dbRes = await fetch(`${process.env.SUPABASE_URL}/rest/v1/leads`, {
      method: 'POST',
      headers: {
        'apikey': process.env.SUPABASE_SERVICE_ROLE_KEY,
        'Authorization': `Bearer ${process.env.SUPABASE_SERVICE_ROLE_KEY}`,
        'Content-Type': 'application/json',
        'Prefer': 'return=representation'
      },
      body: JSON.stringify(lead)
    });

    if (!dbRes.ok) {
      const text = await dbRes.text();
      console.error('Supabase insert failed', dbRes.status, text);
      return res.status(502).json({ error: 'Could not save lead' });
    }

    const [saved] = await dbRes.json();
    return res.status(200).json({ ok: true, id: saved?.id });
  } catch (err) {
    console.error('Submit handler error', err);
    return res.status(500).json({ error: 'Unexpected server error' });
  }
}
