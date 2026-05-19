"""Google Places — discovery source (industry x location → businesses).

This module USED to call the paid Google Places API (Nearby Search +
Place Details + Geocode). As of the cost-cut, all three are replaced
by a single call into scraper/google_places_free.py — the HTTP-only
free Maps endpoint we already use for rating backfills.

The discover() interface is unchanged so pipeline/runner.py keeps
working without edits. The only behavioural differences vs the paid
path:
  - No google_review_count (free endpoint can't fetch it HTTP-only).
    Cards that displayed review count now show "—".
  - No structured address_components — city + postcode come from a
    regex over the formatted_address string.
  - No phone number from this stage — backfilled later by the
    website scraper when present on-site.

What's gone:
  - GOOGLE_PLACES_API_KEY env var is no longer required.
  - QuotaExhausted exception removed (no quota to exhaust).
  - Per-call sleep removed (the scraper's proxy pool handles pacing).
"""
from __future__ import annotations

import logging

from pipeline.sources import Business
from scraper import google_places_free

logger = logging.getLogger("crm.pipeline.sources.google_places")


def discover(industry: str, location: str, limit: int = 60) -> list[Business]:
    """Discover up to `limit` businesses matching (industry x location)
    via the free Google Maps endpoint. Drop-in replacement for the
    previous paid-API implementation.

    Returns a list of Business dicts. On scraper unavailability or
    fetch failure, returns a single-element list with source_errors
    populated — mirrors the paid module's error-stub shape so the
    runner's quota-error path still triggers correctly.
    """
    try:
        return google_places_free.discover(industry, location, limit=limit)
    except Exception as e:
        logger.exception("free Places discover failed for %s / %s", industry, location)
        return [{
            "source_errors": {"google_places": f"unexpected: {str(e)[:120]}"},
            "business_name": "(discover failed)",
        }]
