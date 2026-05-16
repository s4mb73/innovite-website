# Innovite CRM — Scraper

Custom website scraper. Pure HTTP via [wreq-python](https://github.com/0x676e67/wreq-python)
(TLS/HTTP2 fingerprint impersonation) + UK ISP proxies. No browser
automation, no Playwright, no CAPTCHA solving.

## Architecture

```
pipeline/runner.py
    ↓ after Apollo, before scoring
scraper/enricher.py        — entry point: enrich(business)
    ↓
scraper/extractor.py       — fetch homepage + sub-page, Haiku-extract signals
    ↓
scraper/client.py          — wreq wrapper, proxy rotation, retry
    ↓
scraper/proxy_pool.py      — load + round-robin /etc/innovite/proxies.list
```

Output of one successful scrape lands in three columns on `crm.leads`:
- `website_signals` (jsonb) — `{summary, services, team_size_hint,
  recency_hint, contact_form, pricing_visible, client_logos}`
- `website_scraped_at` (timestamptz)
- `website_scrape_status` ('ok' / 'blocked' / 'timeout' / 'no_website'
  / 'parse_failed' / 'disabled')

## Operational rules

1. **Proxies file lives at `/etc/innovite/proxies.list`** on the VPS,
   chmod 600, owner `deploy`. Never committed.
2. **Proxy egress only.** All scraper requests go through a proxy. The
   VPS's own IP never touches a scraping target — scraper bans can't
   affect the CRM's mail or app reputation.
3. **One proxy per request** (round-robin). Per-host rate limit is 1
   request per 3 seconds across the pool, so we never hammer a target
   regardless of IP spread.
4. **Graceful degradation.** Missing wreq, empty proxy list, blocked
   target, parse failure → scraper marks the lead with the appropriate
   status and the pipeline continues. The lead is just less enriched.

## Setting up the proxies file

On the VPS (NOT in this repo):

```
sudo install -o deploy -g deploy -m 0600 /dev/null /etc/innovite/proxies.list
sudo -u deploy nano /etc/innovite/proxies.list
```

Format — one proxy per line:

```
# Comments allowed (lines starting with #)
IP:PORT:USERNAME:PASSWORD
IP:PORT:USERNAME:PASSWORD
...
```

After editing, reload the worker's pool without restart:

```
sudo systemctl kill -s HUP crm-worker
```

## Verifying the pool

After deploy, look in journalctl for the line:
```
crm.scraper.proxy_pool — ProxyPool loaded: N proxies from /etc/innovite/proxies.list
```

If N=0, the file is missing, malformed, or unreadable. The pipeline
will continue but every lead will get `website_scrape_status='disabled'`.

## Cost

Per lead with a successful scrape:
- 1-2 proxy HTTP requests (typically <30KB each)
- 1 Anthropic Haiku call (~£0.001)

Per 200-lead pipeline run: ~£0.40 in Anthropic spend on top of the
existing ~£3.50. Proxy bandwidth is negligible — under 10MB per run.

## Tuning

Constants in `client.py`:
- `TIMEOUT_S` — per-request timeout (12s default)
- `MAX_RETRIES` — retry with fresh proxy on failure (2 default)
- `_MIN_INTERVAL_PER_HOST_S` — per-host rate limit (3s default)

Constants in `proxy_pool.py`:
- `BENCH_AFTER_CONSECUTIVE_FAILURES` — bench threshold (3 default)
- `BENCH_DURATION_SECONDS` — bench duration (15 min default)

## What the scraper does NOT do

- No browser automation (Playwright, Selenium, Puppeteer)
- No CAPTCHA solving — if a target serves a CAPTCHA, that proxy gets
  benched and we try another. If every proxy gets challenged, the
  lead lands with status='blocked' and the pipeline continues
- No login/authenticated scraping
- No LinkedIn / Facebook / Yell / Yelp (skipped at the enricher level)
- No persistent cookie jars (each request is independent)
- No JavaScript execution — we read raw HTML only. Sites that render
  content entirely client-side (some Squarespace / SPA-heavy sites)
  will return mostly-empty content; Haiku reports "no signal" rather
  than guessing
