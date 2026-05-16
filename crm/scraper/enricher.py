"""Website-scraper enrichment source.

Implements the EnrichmentSource protocol from crm/pipeline/sources/.
Slots into the pipeline runner between Apollo and scoring. Sets
business['website_signals'], business['website_scraped_at'], and
business['website_scrape_status'] so the SQL insert in
pipeline/runner._insert_lead can persist them.

Skip rules
----------
- No website on the business → status='no_website', no fetch attempted
- Scraper disabled (wreq missing or pool empty) → status='disabled'
- Domain in SKIP_DOMAINS (e.g. Facebook business pages) → status='no_website'

Cost
----
Best case ~£0.001 per lead (1 page + 1 LLM call). Worst case ~£0.003
(homepage + 1 subpage + LLM). At 200 leads per run that's ~£0.40-£0.60
in scraper cost on top of the existing ~£3.50 per run.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from scraper import extractor

logger = logging.getLogger("crm.scraper.enricher")

name = "website_scraper"

# Domains we deliberately don't scrape because they're not the lead's
# actual website (they're a third-party placeholder) and they have
# tight anti-bot regimes.
SKIP_DOMAINS = (
    "facebook.com", "fb.com", "instagram.com", "linkedin.com",
    "yell.com", "yelp.com", "google.com", "goo.gl",
)


def _should_skip(website: str) -> bool:
    site = (website or "").lower()
    return any(d in site for d in SKIP_DOMAINS)


def enrich(business: dict) -> dict:
    """Fetch the lead's homepage + sub-page, extract signals via Haiku,
    annotate the business dict. Never raises — failures land as
    status='blocked'/'timeout'/'parse_failed' and the runner continues."""
    website = (business.get("website") or "").strip()
    if not website:
        business["website_signals"] = None
        business["website_scrape_status"] = "no_website"
        return business

    if _should_skip(website):
        business["website_signals"] = None
        business["website_scrape_status"] = "no_website"
        business.setdefault("source_errors", {})["website_scraper"] = (
            f"skipped third-party domain: {website[:80]}"
        )
        return business

    try:
        result = extractor.extract(website)
    except Exception as e:
        logger.exception("website extract failed for %s", website[:80])
        business.setdefault("source_errors", {})["website_scraper"] = (
            f"unexpected: {str(e)[:120]}"
        )
        business["website_signals"] = None
        business["website_scrape_status"] = "blocked"
        return business

    business["website_scraped_at"] = datetime.now(timezone.utc)
    business["website_scrape_status"] = result.get("status") or "ok"
    business["website_signals"] = result.get("signals") or None

    # Surface a short error trail on the lead so the operator can see
    # *why* a website wasn't scraped without diving into worker logs.
    if result.get("errors"):
        business.setdefault("source_errors", {})["website_scraper"] = (
            " | ".join(result["errors"])[:240]
        )

    return business
