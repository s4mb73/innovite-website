"""Innovite CRM — custom website scraper.

Pure HTTP via wreq (no browser automation). Egress is always through a
UK ISP proxy from the pool — the VPS's own IP never touches a scraping
target, so scraper bans can't affect the CRM's mail or app reputation.

Public entry point: scraper.enricher.enrich(business) — fetches the
lead's homepage + about/services pages, extracts structured signals
via Anthropic Haiku, returns the business dict with `website_signals`,
`website_scraped_at`, and `website_scrape_status` populated.

Modules:
  proxy_pool — load + round-robin the IP:PORT:USER:PASS list from
               /etc/innovite/proxies.list, track per-IP health
  client     — wreq wrapper with random browser-emulation per request,
               retry on transient failures with a fresh proxy
  extractor  — fetch homepage + likely sub-pages, hand HTML to Haiku,
               return structured signals dict
  enricher   — the EnrichmentSource shape the runner already speaks

Graceful degradation: if wreq isn't installed or the proxy list is
empty, enrich() returns the business unchanged with
website_scrape_status='disabled'. The pipeline runner continues; the
lead is just slightly less enriched.
"""
