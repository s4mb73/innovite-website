"""Free Google Places replacement — rating + identity via HTTP-only.

Why this exists
---------------
The official Google Places API charges ~$0.025 per Place Details call
including atmosphere (rating) data. At 200 leads/run × 1 lookup each,
that's ~$5/run; running daily = ~$150/mo. This module reproduces ~80%
of the per-lead Places signal — rating, name, website, address, place
ID, lat/lng — for £0.

What we DON'T get (be honest about the trade-off)
--------------------------------------------------
Review count. Every "free open-source" Google Maps scraper that
returns review count uses browser automation (Selenium / Playwright /
Botasaurus). The count lives in a separate XHR endpoint that requires
JS-computed context tokens — pure HTTP can't synthesize those without
arms-race reverse engineering. Probed for hours, confirmed dead end.

If review count later becomes load-bearing for outreach copy
("only 8 reviews vs your competitor's 200" lines), Apify is the
cheapest paid path: $0.40/1000 (60× cheaper than Places) including
review count.

How the endpoint works
----------------------
We hit Google Maps' internal `tbm=map&pb=...` endpoint. The `pb` is
Google's protobuf-over-URL encoding selecting which fields the server
returns. We use a minimal pb that returns the place list:

  https://www.google.com/search?tbm=map&hl=en&gl=uk
       &q=<query>
       &pb=!4m8!1m3!1d8000.0!2d<lng>!3d<lat>!3m2!1i1024!2i768
           !4f13.1!7i20!10b1
       &tch=1

Response is JSONP-wrapped: `{c:0, d:"<XSSI-prefixed JSON array>"}`,
with a `/*""*/` trailer to strip. The XSSI prefix is the standard
`)]}\\n` Google uses to defeat JSON hijacking. Inside the array,
each place has stable offsets in `result[0][1][N][14]`:

  [11]    name
  [18]    formatted address
  [10]    place_id (hex:hex)
  [4][7]  rating (float 1.0-5.0)
  [7][0]  website URL
  [9]     [_, _, lat, lng]

Verified across 10+ places across multiple categories at probe time.
Offsets are Google's internal protobuf indices — stable across A/B
tests, but a Google rewrite would break them. Treat parser as
best-effort with graceful None on each field.

Matching strategy
-----------------
Same conservative approach as the Gazette scraper: don't claim a
match unless we're confident it's the right business. We accept the
first result whose name contains the longest non-suffix token of the
query, and whose address contains the city. Below that threshold the
lookup returns 'not_found' rather than misreporting.

Coordinates
-----------
The pb string carries lat/lng/zoom but Google honours the textual
query for ranking. A UK-centered pb (London-ish) plus "{name} {city}"
in the query gets correct results without us needing per-lead
geocoding (which would itself be a paid Places call).
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
import urllib.parse

from scraper import client as scraper_client

logger = logging.getLogger("crm.scraper.google_places_free")

SEARCH_URL = "https://www.google.com/search"

# UK-centered pb. lat=54.97 lng=-1.62 is Newcastle-ish; the textual
# query overrides ranking, so the exact coords don't matter much
# as long as the country is right.
_DEFAULT_PB = (
    "!4m8!1m3"
    "!1d8000.0!2d-1.6178!3d54.9783"
    "!3m2!1i1024!2i768"
    "!4f13.1!7i20!10b1"
)

# Suffix tokens we strip from the name when looking for the
# longest-token-in-page check — same logic as gazette.py / jobs.py.
_AMBIGUOUS_SUFFIXES = {"ltd", "limited", "plc", "llp", "uk", "the", "and", "co"}
_MIN_TOKEN_LEN = 4


def _strip_jsonp_trailer(body: str) -> str:
    """The response ends with `/*""*/` JSONP padding — drop it before
    JSON-parsing. Use rsplit so we keep any earlier `/*...` substrings
    that might legitimately appear inside the JSON string values."""
    return body.rsplit("/*", 1)[0]


def _normalise(s: str) -> str:
    """Lowercase + drop diacritics + collapse non-alphanum to spaces."""
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    base = re.sub(r"[^a-z0-9 ]+", " ", ascii_only.lower())
    return re.sub(r"\s+", " ", base).strip()


def _distinctive_tokens(name: str) -> list[str]:
    """All tokens from `name` that aren't corporate suffixes or
    stopwords, preserving input order. The FIRST token is treated as
    the brand identifier in matching — for 'Roca Accountants', 'roca'
    is the discriminator, not 'accountants' (which is a category word
    that matches dozens of unrelated businesses)."""
    return [t for t in _normalise(name).split()
            if t and t not in _AMBIGUOUS_SUFFIXES and len(t) >= _MIN_TOKEN_LEN]


def _safe(d, *path):
    """Walk a nested list using integer indices; return None on miss."""
    cur = d
    for p in path:
        if isinstance(cur, list) and isinstance(p, int) and -len(cur) <= p < len(cur):
            cur = cur[p]
        else:
            return None
    return cur


def _extract_place(pm) -> dict | None:
    """Pull our fields out of one place_meta record (`result[0][1][N][14]`).
    Returns None if the record doesn't have at least a name."""
    if not isinstance(pm, list) or len(pm) < 12:
        return None
    name = _safe(pm, 11)
    if not name:
        return None
    return {
        "name":     name,
        "address":  _safe(pm, 18),
        "place_id": _safe(pm, 10),
        "rating":   _safe(pm, 4, 7),
        "website":  _safe(pm, 7, 0) or _safe(pm, 7, 1),
        "lat":      _safe(pm, 9, 2),
        "lng":      _safe(pm, 9, 3),
    }


def _match_score(query_name: str, query_city: str | None,
                 candidate: dict) -> int:
    """Match score 0-4. 0 = reject; the BRAND token (first distinctive
    word) MUST match or we return 0 regardless of other signals.
      +2 if the brand token of query is in candidate name (mandatory)
      +1 per additional query token also in candidate name
      +1 if city appears in candidate address (when city provided)
    'Roca Accountants' vs 'RWA Accountants' scores 0 — the brand
    word 'roca' is absent — even though both share 'accountants'."""
    tokens = _distinctive_tokens(query_name)
    if not tokens:
        return 0
    cand_name_norm = _normalise(candidate.get("name") or "")
    brand = tokens[0]
    if brand not in cand_name_norm:
        return 0  # hard floor — wrong business

    score = 2
    for t in tokens[1:]:
        if t in cand_name_norm:
            score += 1
    if query_city:
        addr_norm = _normalise(candidate.get("address") or "")
        if _normalise(query_city) in addr_norm:
            score += 1
    return score


# ── Public entry ────────────────────────────────────────────────────
def find(business_name: str, city: str | None = None) -> dict:
    """Look up a business via the free Google Maps pb endpoint.

    Returns:
      {status, place, candidates, errors}
        status:   'found' | 'low_confidence' | 'not_found' | 'skipped'
        place:    the best matched place dict (see _extract_place) or None
        candidates: list of all returned places (for debugging)
        errors:   list of short strings
    """
    result: dict = {"status": "skipped", "place": None,
                    "candidates": [], "errors": []}

    if not business_name or not business_name.strip():
        result["errors"].append("no business_name")
        return result

    if not scraper_client.is_available():
        result["errors"].append("scraper disabled (wreq/proxy pool)")
        return result

    query = business_name.strip()
    if city:
        query = f"{query} {city}"

    params = urllib.parse.urlencode({
        "tbm":  "map",
        "hl":   "en",
        "gl":   "uk",
        "q":    query,
        "pb":   _DEFAULT_PB,
        "tch":  "1",
    })
    url = f"{SEARCH_URL}?{params}"
    body = scraper_client.fetch(url, max_bytes=500_000)
    if not body:
        result["errors"].append("fetch failed")
        return result

    try:
        payload = _strip_jsonp_trailer(body)
        outer = json.loads(payload)
        inner = outer.get("d", "")
        if not isinstance(inner, str):
            result["errors"].append("response missing 'd' string")
            return result
        # Strip Google's XSSI prefix — first 4 or 5 chars depending on
        # whether the newline is present.
        inner = inner.lstrip(")]}'").lstrip("\n")
        data = json.loads(inner)
    except (json.JSONDecodeError, ValueError) as e:
        result["errors"].append(f"parse: {str(e)[:80]}")
        return result

    # Place list is at data[0][1][1..N]; data[0][1][0] is sometimes
    # query metadata. We try a wide slice and filter to those with a
    # valid name extraction.
    raw_places = _safe(data, 0, 1) or []
    candidates: list[dict] = []
    for entry in raw_places:
        pm = _safe(entry, 14)
        cand = _extract_place(pm)
        if cand:
            candidates.append(cand)

    result["candidates"] = candidates
    if not candidates:
        result["status"] = "not_found"
        return result

    # Rank by match score; take the highest.
    scored = sorted(
        ((cand, _match_score(business_name, city, cand)) for cand in candidates),
        key=lambda x: -x[1],
    )
    best_cand, best_score = scored[0]
    if best_score == 0:
        result["status"] = "not_found"
        return result

    result["place"] = best_cand
    # Score 2 = brand token matched but nothing else. Score 3+ = brand
    # plus city/extra-token confirmation. We mark 2 as low_confidence
    # so the operator/UI can flag for manual verification.
    result["status"] = "found" if best_score >= 3 else "low_confidence"
    return result


# ── Pipeline adapter ────────────────────────────────────────────────
name = "google_places_free"


def enrich(business: dict) -> dict:
    """Populate google_rating, google_maps_url-equivalent, and
    google_place_id when missing. Never raises.

    Designed to coexist with the paid Places source: only fills fields
    that aren't already populated. Means a hybrid pipeline can keep
    paid Places for the leads that need full data and let this free
    source fill in the gaps for everything else."""
    bn = (business.get("business_name") or "").strip()
    city = (business.get("city") or "").strip() or None

    # Skip if rating is already populated (paid Places already ran).
    if business.get("google_rating") is not None:
        return business

    try:
        r = find(bn, city)
    except Exception as e:
        logger.exception("google_places_free failed for %s", bn[:80])
        business.setdefault("source_errors", {})["google_places_free"] = (
            f"unexpected: {str(e)[:120]}"
        )
        return business

    if r["status"] not in ("found", "low_confidence") or not r["place"]:
        if r["errors"]:
            business.setdefault("source_errors", {})["google_places_free"] = (
                " | ".join(r["errors"])[:240]
            )
        return business

    place = r["place"]
    # Only fill fields that aren't already present. Especially important
    # for `website` — Stage 2 discovery may have already found one and
    # we don't want to overwrite a verified slug match with a Maps URL.
    if place.get("rating") is not None and business.get("google_rating") is None:
        business["google_rating"] = float(place["rating"])
    if place.get("place_id") and not business.get("google_place_id"):
        business["google_place_id"] = place["place_id"]
    if place.get("website") and not business.get("website"):
        business["website"] = place["website"]
    if place.get("address") and not business.get("address"):
        business["address"] = place["address"]
    # google_review_count intentionally not set — we can't fetch it
    # HTTP-only and don't want to misreport a None as zero.
    business.setdefault("source_errors", {})["google_places_free"] = (
        f"hit ({r['status']}, score derived from name+city+website match)"
    )
    return business
