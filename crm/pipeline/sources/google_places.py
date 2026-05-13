"""Google Places API — UK business discovery.

Two-step: Nearby Search to get candidate place_ids, then Place Details
for the fields we care about (phone, website, full address). We only
pay for Details on rows that survive the basic-quality filter, which
shaves ~30% off the per-run cost.

Auth: GOOGLE_PLACES_API_KEY env var. Restrict the key to the VPS IP
in the Google Cloud Console — the key has spend authority.

Rate limit: Google quotes 100 QPS, $200/mo free credit ≈ 6000 Place
Details calls. We add a 50ms sleep between calls so a single client's
run never burns through quota.

Quota errors return partial results with a source_error rather than
raising. The runner marks the run 'partial' and the operator sees the
leads we *did* find plus a clear banner.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Iterable

from pipeline.sources import Business

PLACES_NEARBY_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
PLACES_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

# Fields we ask Place Details for. Each field bills separately; keep this lean.
DETAILS_FIELDS = "name,formatted_address,address_components,formatted_phone_number,website,rating,user_ratings_total,url,place_id,business_status"

# Per-call sleep to stay well clear of the 100 QPS ceiling.
PER_CALL_SLEEP_S = 0.05


class QuotaExhausted(Exception):
    pass


def _http_get(url: str, params: dict) -> dict:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{qs}", headers={"User-Agent": "InnoviteCRM/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _api_key() -> str:
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not key:
        raise RuntimeError(
            "GOOGLE_PLACES_API_KEY not set. Add to /etc/innovite/crm-worker.env "
            "and restrict the key to the VPS IP in Google Cloud Console."
        )
    return key


def _extract_postcode(address_components: list[dict] | None) -> str | None:
    if not address_components:
        return None
    for c in address_components:
        if "postal_code" in c.get("types", []):
            return c.get("long_name") or c.get("short_name")
    return None


def _extract_city(address_components: list[dict] | None) -> str | None:
    if not address_components:
        return None
    for c in address_components:
        types = c.get("types", [])
        if "postal_town" in types or "locality" in types:
            return c.get("long_name")
    return None


def _geocode(location: str) -> tuple[float, float] | None:
    """Resolve a UK town/region name to lat/lng for Nearby Search."""
    data = _http_get(GEOCODE_URL, {
        "address": f"{location}, UK",
        "region": "uk",
        "key": _api_key(),
    })
    status = data.get("status")
    if status == "OVER_QUERY_LIMIT":
        raise QuotaExhausted("Geocode quota exhausted")
    if status != "OK" or not data.get("results"):
        return None
    loc = data["results"][0]["geometry"]["location"]
    return float(loc["lat"]), float(loc["lng"])


def _nearby_search(lat: float, lng: float, industry: str, radius_m: int = 15000) -> Iterable[dict]:
    """Yield raw Place results across all pages (up to 60 per query)."""
    next_token = None
    pages = 0
    while pages < 3:  # Google caps at 3 pages = 60 results
        params = {
            "location": f"{lat},{lng}",
            "radius": radius_m,
            "keyword": industry,
            "key": _api_key(),
        }
        if next_token:
            params["pagetoken"] = next_token
            time.sleep(2)  # Google requires a delay before pagetoken is valid

        data = _http_get(PLACES_NEARBY_URL, params)
        status = data.get("status")
        if status == "OVER_QUERY_LIMIT":
            raise QuotaExhausted(f"Nearby search quota exhausted for {industry}/{lat},{lng}")
        if status not in ("OK", "ZERO_RESULTS"):
            return

        for r in data.get("results", []):
            yield r

        next_token = data.get("next_page_token")
        if not next_token:
            return
        pages += 1


def _place_details(place_id: str) -> dict | None:
    time.sleep(PER_CALL_SLEEP_S)
    data = _http_get(PLACES_DETAILS_URL, {
        "place_id": place_id,
        "fields": DETAILS_FIELDS,
        "key": _api_key(),
    })
    status = data.get("status")
    if status == "OVER_QUERY_LIMIT":
        raise QuotaExhausted(f"Place details quota exhausted on {place_id}")
    if status != "OK":
        return None
    return data.get("result")


def _looks_like_real_business(nearby_row: dict) -> bool:
    """Cheap filter before we pay for Place Details on every row.

    Drops permanently-closed pins and pins with no name. Keeps rating-less
    rows since plenty of viable B2B targets have <5 reviews.
    """
    if nearby_row.get("business_status") not in (None, "OPERATIONAL"):
        return False
    if not nearby_row.get("name"):
        return False
    return True


def discover(industry: str, location: str, limit: int = 60) -> list[Business]:
    """Discover up to `limit` businesses matching (industry x location).

    Returns Business dicts with identity + Google fields filled.
    Drops permanently-closed / nameless pins before paying for Details.
    """
    try:
        coords = _geocode(location)
    except QuotaExhausted as e:
        return [{"source_errors": {"google_places": str(e)}, "business_name": "(quota-exhausted)"}]
    if not coords:
        return []

    lat, lng = coords
    results: list[Business] = []
    seen_place_ids: set[str] = set()

    try:
        for nearby in _nearby_search(lat, lng, industry):
            if len(results) >= limit:
                break
            place_id = nearby.get("place_id")
            if not place_id or place_id in seen_place_ids:
                continue
            seen_place_ids.add(place_id)

            if not _looks_like_real_business(nearby):
                continue

            details = _place_details(place_id)
            if not details:
                continue

            postcode = _extract_postcode(details.get("address_components"))
            city = _extract_city(details.get("address_components"))

            results.append({
                "business_name":        details.get("name", ""),
                "address":              details.get("formatted_address", ""),
                "postcode":             postcode or "",
                "city":                 city or "",
                "phone":                details.get("formatted_phone_number", ""),
                "website":              details.get("website", ""),
                "google_rating":        float(details.get("rating") or 0) or None,
                "google_review_count":  int(details.get("user_ratings_total") or 0),
                "google_maps_url":      details.get("url", ""),
                "google_place_id":      place_id,
            })
    except QuotaExhausted as e:
        # Partial results — return what we have and let the runner mark
        # the run 'partial'. The operator sees real leads + a banner.
        if results:
            results[-1].setdefault("source_errors", {})["google_places"] = str(e)
        else:
            results.append({"source_errors": {"google_places": str(e)}, "business_name": "(quota-exhausted)"})

    return results
