"""Reed.co.uk — open-jobs growth-signal lookup (Stage 5).

Why Reed instead of Indeed
--------------------------
The plan called for Indeed, but Indeed serves a client-rendered
Next.js SPA — wreq retrieves the HTML shell but the job list is
hydrated by JS from a private API, so HTTP-only scraping returns
nothing useful. Browser automation would solve it but the user
explicitly chose HTTP-only.

Reed is UK-focused, server-renders job pages, and has a stable
canonical URL pattern at /jobs/jobs-at-<slug> that returns:
  "N Jobs At {Company} Jobs"
…in the page header for any slug — real-N for real companies with
listings, 0 for everything else (no-such-company is functionally the
same as no-openings for our outreach purpose).

Signal classification
---------------------
Per the operator's brief, the actionable splits are:
  0       → 'none'     (not hiring; no outreach hook)
  1-4     → 'hiring'   (modest activity; soft hook)
  5+      → 'scaling'  (real growth; strong hook for an
                        advisory-services pitch)
  unknown → bad slug, fetch failure, or name too ambiguous

Free, no auth, no anti-bot detected on this endpoint at probe time.
We still go through the proxy pool for consistency with the other
scrapers and to amortise rate-limit risk across our IP pool.
"""
from __future__ import annotations

import logging
import re
import unicodedata
import urllib.parse
from datetime import datetime, timezone

from scraper import client as scraper_client

logger = logging.getLogger("crm.scraper.jobs")

REED_BASE = "https://www.reed.co.uk"

# Suffix words stripped from the slug — Reed itself indexes companies
# without these (e.g. "Greggs plc" → "/jobs/jobs-at-greggs").
_SLUG_DROP_TOKENS = {"ltd", "limited", "plc", "llp", "uk", "the", "and", "co"}

# Minimum slug length (post-cleanup) before we'll bother querying.
# Below this the slug is too generic — short tokens like "abc" would
# match common abbreviations on Reed and return cross-sell noise.
_MIN_SLUG_LEN = 4

# Headline breadcrumb Reed renders for any /jobs-at-<slug> page —
# extracted from the cleaned visible text, not the raw HTML, because
# Reed splits the breadcrumb across spans and HTML comments. The
# pattern anchors on "N Jobs At <Company> jobs" to avoid matching
# unrelated counters like "1 job hidden" or "12 jobs you may like".
_COUNT_RE = re.compile(
    r"(\d{1,5})\s+Jobs?\s+At\s+([A-Za-z0-9][A-Za-z0-9 &'\-]{1,80}?)\s+(?:Jobs|jobs|Vacancies|vacancies)\b"
)

# Cheap script/style stripper — same shape as scraper.extractor's
# but inlined to avoid the import cycle (extractor already imports
# client and this module also imports client).
_STRIP_NOISE = re.compile(
    r"<(script|style|svg|noscript|iframe)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_COLLAPSE_WS = re.compile(r"\s+")


def _visible_text(html: str) -> str:
    no_noise = _STRIP_NOISE.sub("", html)
    text = re.sub(r"<[^>]+>", " ", no_noise)
    return _COLLAPSE_WS.sub(" ", text).strip()


def _slugify(name: str) -> str | None:
    """Turn 'Greggs plc' → 'greggs', 'O'Reilly & Sons Ltd' → 'oreilly-and-sons'.
    Returns None if the cleaned slug is too short to query reliably."""
    if not name:
        return None
    # Strip diacritics so 'Café Nero' → 'cafe-nero'.
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Reed uses "and" for ampersands rather than collapsing them.
    ascii_only = re.sub(r"&", " and ", ascii_only)
    # Strip apostrophes BEFORE non-alphanum cleanup so "O'Reilly" → "oreilly".
    ascii_only = re.sub(r"['’]", "", ascii_only)
    # Lowercase + only keep [a-z0-9 ].
    base = re.sub(r"[^a-z0-9 ]+", " ", ascii_only.lower())
    tokens = [t for t in base.split() if t and t not in _SLUG_DROP_TOKENS]
    slug = "-".join(tokens)
    if len(slug.replace("-", "")) < _MIN_SLUG_LEN:
        return None
    return slug


def _classify(count: int) -> str:
    if count <= 0:
        return "none"
    if count >= 5:
        return "scaling"
    return "hiring"


# ── Public entry ────────────────────────────────────────────────────
def check(business_name: str) -> dict:
    """Look up a business's open-jobs count on Reed.

    Returns:
      {signal, count, source_url, errors}
        signal:     'none' | 'hiring' | 'scaling' | 'unknown'
        count:      int   (verified open jobs, 0 when none)
        source_url: str | None (link to the Reed company page)
        errors:     list of short strings for debugging
    """
    result: dict = {"signal": "unknown", "count": 0, "source_url": None, "errors": []}

    slug = _slugify(business_name)
    if not slug:
        result["errors"].append("name too short to slugify")
        return result

    if not scraper_client.is_available():
        result["errors"].append("scraper disabled (wreq/proxy pool)")
        return result

    url = f"{REED_BASE}/jobs/jobs-at-{urllib.parse.quote(slug)}"
    result["source_url"] = url
    body = scraper_client.fetch(url)
    if not body:
        result["errors"].append("fetch failed")
        return result

    match = _COUNT_RE.search(_visible_text(body))
    if not match:
        # Reed always renders the breadcrumb for any slug — if it's
        # missing, we hit a different page shape (maintenance, 503,
        # redirect to login). Treat as unknown rather than zero so
        # we don't misreport a fetch problem as "no hiring activity".
        result["errors"].append("count breadcrumb not found")
        return result

    count = int(match.group(1))
    result["count"]  = count
    result["signal"] = _classify(count)
    return result


# ── Pipeline adapter ────────────────────────────────────────────────
name = "jobs"


def enrich(business: dict) -> dict:
    """Annotate the business dict with jobs_open_count + jobs_signal +
    jobs_source_url + jobs_last_checked_at. Never raises."""
    bn = (business.get("business_name") or "").strip()
    try:
        r = check(bn)
    except Exception as e:
        logger.exception("jobs check failed for %s", bn[:80])
        business.setdefault("source_errors", {})["jobs"] = f"unexpected: {str(e)[:120]}"
        business["jobs_signal"] = "unknown"
        return business

    business["jobs_open_count"]     = r["count"]
    business["jobs_signal"]         = r["signal"]
    business["jobs_source_url"]     = r["source_url"]
    business["jobs_last_checked_at"] = datetime.now(timezone.utc)
    if r["errors"]:
        business.setdefault("source_errors", {})["jobs"] = " | ".join(r["errors"])[:240]
    return business
