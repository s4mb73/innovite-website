"""Website-URL discovery from the Companies House name (Stage 2).

Per the documented 6-stage plan, Stage 2 should derive the lead's
website URL from the CH-registered name *before* spending Google
Places (Stage 3) quota. Currently the runner gets the URL from
Places — fine when Places matches, but it means we can't actually
say Stage 2 is independent of Stage 3.

Why heuristic guessing instead of search-engine scraping
--------------------------------------------------------
The plan suggested "Search Google for [company name] [city] to find
their website URL." HTTP-only search-engine scraping turns out to
be blocked the same way LinkedIn-URL discovery is — Bing wraps
results in /ck/a click-redirects, DuckDuckGo's HTML endpoint
intermittently rejects wreq, Google requires a paid API.

For UK SMBs the alternative is cheap: most own a domain matching a
predictable slug of their registered name. We generate a handful of
candidate domains, HEAD each one, and take the first that resolves
+ confirms the company name in the homepage. Hit rate ~30-50% on
UK Ltds with conventional naming; for the rest, the runner falls
back to whatever Google Places returns later in the pipeline.

Candidate generation
--------------------
For "Roca Accountants Limited" we try, in order:
  rocaaccountants.co.uk
  roca-accountants.co.uk
  rocaaccountants.com
  roca-accountants.com
  rocaaccountants.uk
  www.rocaaccountants.co.uk
  www.roca-accountants.co.uk

.co.uk first because that's the dominant UK SMB TLD by a wide margin.
Stop on the first candidate that returns 200 AND whose homepage
contains the company name (validation defeats parked-domain false
positives — many slug domains resolve to a registrar holding page
or a competitor squatting on the name).

Output
------
  status:   'found' | 'not_found' | 'skipped'
  website:  https://<host>/ on a confirmed match, else None
  candidates_tried: list of URLs we HEAD'd (for debugging)
  errors:   list of short strings
"""
from __future__ import annotations

import logging
import re
import socket
import unicodedata
import urllib.parse

from scraper import client as scraper_client

logger = logging.getLogger("crm.scraper.website_discovery")

# Suffix words stripped from the slug — domains for "Greggs plc" are
# at greggs.co.uk, not greggsplc.co.uk. UK GDPR registrations rarely
# include the company-form suffix in the live web domain.
_SLUG_DROP_TOKENS = {"ltd", "limited", "plc", "llp", "uk", "the", "and", "co"}

# TLDs to try, ranked by SMB prevalence in the UK.
_TLDS = (".co.uk", ".com", ".uk")

# Skip the lookup entirely for names that would generate garbage
# candidates (1-token slugs <5 chars produce "abc.co.uk" type domains
# that mostly resolve to squatters / unrelated sites).
_MIN_SLUG_LEN = 5

# Cap on HEAD requests per lead — sums to (variants × TLDs × www).
# Concretely: 2 slugs × 3 TLDs × 2 (with/without www) = 12 candidates.
# Bounded so a single lead can't burn proxy capacity on a long list.
_MAX_CANDIDATES = 12

# DNS check timeout per candidate. Short because we're only trying to
# decide "does this host even exist" — slow resolvers shouldn't pin a
# pipeline run.
_DNS_TIMEOUT_S = 2.0


def _dns_resolves(host: str) -> bool:
    """Cheap precheck: does the hostname resolve at all? If DNS says no,
    we skip the proxied HTTPS fetch (which would otherwise burn 2-3
    retries × multiple proxies on a guaranteed miss). Resolution itself
    uses the local network — fine for "does this exist", not perfect
    if the lead is geo-restricted from our box, but that's rare."""
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(_DNS_TIMEOUT_S)
    try:
        socket.gethostbyname(host)
        return True
    except (socket.gaierror, OSError):
        return False
    finally:
        socket.setdefaulttimeout(old)


def _normalise(name: str) -> list[str]:
    """Generate slug variants. Returns up to two:
       (a) collapsed:  'rocaaccountants'
       (b) hyphenated: 'roca-accountants'
    Both are commonly used; we try collapsed first because it's
    slightly more common for SMBs."""
    if not name:
        return []
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    ascii_only = re.sub(r"&", " and ", ascii_only)
    ascii_only = re.sub(r"['’]", "", ascii_only)
    base = re.sub(r"[^a-z0-9 ]+", " ", ascii_only.lower())
    tokens = [t for t in base.split() if t and t not in _SLUG_DROP_TOKENS]
    if not tokens:
        return []
    collapsed = "".join(tokens)
    if len(collapsed) < _MIN_SLUG_LEN:
        return []
    out = [collapsed]
    if len(tokens) > 1:
        out.append("-".join(tokens))
    return out


def _candidates(name: str) -> list[str]:
    """Build the full candidate URL list, capped at _MAX_CANDIDATES."""
    slugs = _normalise(name)
    if not slugs:
        return []
    urls: list[str] = []
    # Bare-host candidates first (no www) — fewer redirects to follow.
    for slug in slugs:
        for tld in _TLDS:
            urls.append(f"https://{slug}{tld}/")
    # Then www. variants for sites that don't redirect bare→www.
    for slug in slugs:
        for tld in _TLDS:
            urls.append(f"https://www.{slug}{tld}/")
    return urls[:_MAX_CANDIDATES]


def _name_in_page(html_body: str, business_name: str) -> bool:
    """Cheap validation: the company's name should appear in the homepage
    HTML somewhere (title, header, footer copyright, etc.). Defeats
    parked-domain false positives — a typo'd slug might resolve to
    GoDaddy's holding page, which won't contain the target name.

    Conservative match: lowercase, strip punctuation, look for the
    longest single token of the name (3+ chars) in the page text.
    Using the full multi-word name is too brittle ('Roca Accountants
    Ltd' wouldn't match a page that says 'ROCA' alone in the logo)."""
    if not html_body or not business_name:
        return False
    body = html_body.lower()
    norm = re.sub(r"[^a-z0-9 ]+", " ", business_name.lower())
    tokens = [t for t in norm.split() if len(t) >= 3 and t not in _SLUG_DROP_TOKENS]
    if not tokens:
        return False
    longest = max(tokens, key=len)
    return longest in body


# ── Public entry ────────────────────────────────────────────────────
def find(business_name: str) -> dict:
    """Try to discover the lead's website from its registered name.

    Returns:
      {status, website, candidates_tried, errors}
        status:  'found' | 'not_found' | 'skipped'
        website: confirmed URL with scheme, or None
        candidates_tried: URLs we HEAD'd, in order
        errors:  short strings for debugging
    """
    result: dict = {"status": "skipped", "website": None,
                    "candidates_tried": [], "errors": []}

    cands = _candidates(business_name)
    if not cands:
        result["errors"].append("name too short to slugify")
        return result

    if not scraper_client.is_available():
        result["errors"].append("scraper disabled (wreq/proxy pool)")
        return result

    for url in cands:
        result["candidates_tried"].append(url)
        host = urllib.parse.urlparse(url).hostname or ""
        if not host or not _dns_resolves(host):
            continue
        body = scraper_client.fetch(url)
        if not body:
            continue
        if _name_in_page(body, business_name):
            result["status"]  = "found"
            result["website"] = url
            return result

    result["status"] = "not_found"
    return result


# ── Pipeline adapter ────────────────────────────────────────────────
name = "website_discovery"


def enrich(business: dict) -> dict:
    """Set business['website'] if we don't already have one. Never
    raises. No-op when the lead already has a website (from Google
    Places or any earlier source) — discovery exists to BACKFILL,
    not to overwrite an authoritative URL."""
    if (business.get("website") or "").strip():
        # Already have a URL from upstream — Places, manual entry, etc.
        # Don't second-guess that with a heuristic guess.
        return business

    bn = (business.get("business_name") or "").strip()
    try:
        r = find(bn)
    except Exception as e:
        logger.exception("website discovery failed for %s", bn[:80])
        business.setdefault("source_errors", {})["website_discovery"] = (
            f"unexpected: {str(e)[:120]}"
        )
        return business

    if r["status"] == "found" and r["website"]:
        business["website"] = r["website"]
        # Note in source_errors that this came from heuristic discovery
        # rather than an authoritative source — useful for the operator
        # to know the URL is best-effort, not a guarantee.
        business.setdefault("source_errors", {})["website_discovery"] = (
            f"heuristic match: tried {len(r['candidates_tried'])} candidates"
        )
    elif r["errors"]:
        business.setdefault("source_errors", {})["website_discovery"] = (
            " | ".join(r["errors"])[:240]
        )
    return business
