"""LinkedIn — decision-maker enrichment via public-profile JSON-LD.

What changed (and why)
----------------------
The first version of this module parsed only og:title + og:description
meta tags, which gave us name + current headline and not much else.
After studying the source of working open-source LinkedIn scrapers
(joeyism/linkedin_scraper, nsandman/linkedin-api, hasdata's blog
write-up) and reading the actual HTML LinkedIn serves to logged-out
visitors, we found a much richer data source: LinkedIn embeds a
schema.org Person entry as JSON-LD in every public /in/<slug> page,
even without authentication.

From that single block we get the data the original plan called for:
  - name
  - jobTitle (current titles, often multiple)
  - worksFor (current employer with start date and location)
  - alumniOf (full work history with start/end dates)
  - address.addressLocality (city/region)
  - interactionStatistic (follower count)
  - description (long-form bio)
  - disambiguatingDescription ('Creator, Top Voice' etc.)
  - image.contentUrl (profile photo)
  - sibling Article entries (recent Pulse posts → activity signal)

This is HTTP-only — no Voyager auth, no Multilogin, no browser
automation. Same fetch path as before, just richer parsing.

Hit-rate caveat
---------------
The fetch step still fails ~30-40% of the time with a LinkedIn 999
'Request Denied' on profiles that aren't cached at our exit IP. That's
the underlying anti-bot at the network layer, independent of how we
PARSE successful responses. Improvements to hit rate (proxy quality,
cookie persistence, per-host slower rate limit) are out of scope for
this commit — focus here is extracting maximum value from the responses
that DO come through.

Statuses
--------
  verified   — fetched OK, JSON-LD Person parsed
  blocked    — fetch failed (999 / proxy reject)
  not_found  — fetched but no Person JSON-LD (deleted profile)
  no_url     — lead has no /in/<slug> URL
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import urllib.parse
from datetime import date, datetime, timezone

from scraper import client as scraper_client


def _enrich_enabled() -> bool:
    """Feature flag — LINKEDIN_ENRICH_ENABLED=true in worker env unlocks
    the cookie-based fetch path. Default off so a freshly-cloned worker
    can't accidentally hit LinkedIn before warmup is finished."""
    val = (os.environ.get("LINKEDIN_ENRICH_ENABLED") or "").strip().lower()
    return val in ("1", "true", "yes", "on")

logger = logging.getLogger("crm.scraper.linkedin")

_VALID_IN_PATH      = re.compile(r"^/in/[A-Za-z0-9_\-%.]+/?$")
_VALID_COMPANY_PATH = re.compile(r"^/company/[A-Za-z0-9_\-%.]+/?$")
_OG_TITLE_RE   = re.compile(r'property="og:title"\s+content="([^"]+)"')
_OG_DESC_RE    = re.compile(r'property="og:description"\s+content="([^"]+)"')
_JSONLD_RE     = re.compile(
    r'<script type="application/ld\+json">(.*?)</script>',
    re.DOTALL,
)
_LI_SUFFIX_RE  = re.compile(r"\s*\|\s*LinkedIn\s*$")


def _normalise_url(url: str | None) -> str | None:
    """Accept variants like 'linkedin.com/in/foo', '/in/foo', or full
    https URLs with query strings. Returns canonical
    https://www.linkedin.com/in/<slug> or None if not /in/<slug>."""
    if not url:
        return None
    u = url.strip()
    if not u:
        return None
    if not u.startswith(("http://", "https://")):
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("/in/"):
            u = "https://www.linkedin.com" + u
        else:
            u = "https://" + u
    try:
        p = urllib.parse.urlparse(u)
    except Exception:
        return None
    if "linkedin.com" not in (p.netloc or "").lower():
        return None
    path = (p.path or "/").rstrip("/")
    if not _VALID_IN_PATH.match(path + "/"):
        return None
    return f"https://www.linkedin.com{path}"


_COMPANY_SLUG_RE = re.compile(r"^/company/([A-Za-z0-9_\-%.]+)")


def _normalise_company_url(url: str | None) -> str | None:
    """Same shape as `_normalise_url` but for /company/<slug> URLs.

    Tolerant of sub-paths — `/company/acme/about`, `/company/acme/posts`
    all canonicalise to `https://www.linkedin.com/company/acme`. The
    company main page is always what we want to fetch.

    Returns canonical URL or None when the input isn't a company-page.
    """
    if not url:
        return None
    u = url.strip()
    if not u:
        return None
    if not u.startswith(("http://", "https://")):
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("/company/"):
            u = "https://www.linkedin.com" + u
        else:
            u = "https://" + u
    try:
        p = urllib.parse.urlparse(u)
    except Exception:
        return None
    if "linkedin.com" not in (p.netloc or "").lower():
        return None
    m = _COMPANY_SLUG_RE.match(p.path or "")
    if not m:
        return None
    return f"https://www.linkedin.com/company/{m.group(1)}"


_ORG_TYPES = {"Organization", "Organisation", "Corporation", "LocalBusiness"}


def _is_org_type(t) -> bool:
    """@type can be a string ('Organization'), a list (['Organization',
    'LocalBusiness']), or absent. Return True if any of those values
    looks like a company-style type."""
    if isinstance(t, str):
        return t in _ORG_TYPES
    if isinstance(t, list):
        return any(isinstance(x, str) and x in _ORG_TYPES for x in t)
    return False


def _find_substantive_organisation(ld: dict | list) -> dict | None:
    """Find the company's Organization JSON-LD entry. LinkedIn embeds
    multiple Organization refs on company pages (the company itself,
    related companies, parent/subsidiary). The substantive one has
    `interactionStatistic` (follower count) or `numberOfEmployees`.
    Falls back to the first Organization with a `name` field if no
    entry carries the richer fields."""
    found_rich: list[dict] = []
    found_any: list[dict] = []

    def walk(d):
        if isinstance(d, dict):
            if _is_org_type(d.get("@type")) and d.get("name"):
                found_any.append(d)
                if "interactionStatistic" in d or "numberOfEmployees" in d:
                    found_rich.append(d)
            for v in d.values():
                walk(v)
        elif isinstance(d, list):
            for v in d:
                walk(v)

    walk(ld)
    return (found_rich[0] if found_rich else
            found_any[0] if found_any else None)


def _decode_entities(s: str | None) -> str | None:
    """LinkedIn double-encodes some og: values ('&amp;amp;' instead of
    '&'). Two passes of html.unescape covers both layers without
    over-decoding."""
    if not s:
        return s
    out = html.unescape(s)
    if "&amp;" in out or "&#" in out:
        out = html.unescape(out)
    return out.strip() or None


def _find_substantive_person(ld: dict | list) -> dict | None:
    """The JSON-LD often contains many skeleton Person refs (just name +
    url) and one substantive Person entry with jobTitle/worksFor. We
    want the substantive one."""
    found: list[dict] = []

    def walk(d):
        if isinstance(d, dict):
            if d.get("@type") == "Person" and ("jobTitle" in d or "worksFor" in d):
                found.append(d)
            for v in d.values():
                walk(v)
        elif isinstance(d, list):
            for v in d:
                walk(v)

    walk(ld)
    return found[0] if found else None


def _find_articles(ld: dict | list, author_url: str) -> list[dict]:
    """Pulse articles authored by this person — surfaced for activity
    signal ('posts weekly about X'). Returns most-recent-first."""
    found: list[dict] = []

    def walk(d):
        if isinstance(d, dict):
            if (d.get("@type") == "Article"
                    and isinstance(d.get("author"), dict)
                    and d["author"].get("url") == author_url):
                found.append(d)
            for v in d.values():
                walk(v)
        elif isinstance(d, list):
            for v in d:
                walk(v)

    walk(ld)
    # Sort by datePublished desc (where parseable). Bad dates go last.
    def date_key(art):
        raw = art.get("datePublished") or ""
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    return sorted(found, key=date_key, reverse=True)


def _split_og_title(og_title: str) -> tuple[str | None, str | None]:
    """og:title shape: 'Full Name - Headline... | LinkedIn'."""
    if not og_title:
        return None, None
    cleaned = _LI_SUFFIX_RE.sub("", og_title).strip()
    if " - " not in cleaned:
        return cleaned or None, None
    name, _, headline = cleaned.partition(" - ")
    return name.strip() or None, headline.strip() or None


def _parse_post_date(art: dict) -> date | None:
    raw = art.get("datePublished") or ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except Exception:
        return None


# ── Public entry ────────────────────────────────────────────────────
def check(linkedin_url: str | None) -> dict:
    """Fetch + parse a LinkedIn /in/ profile.

    Returns:
      {status, name, current_title, current_company, location,
       headline, previous_companies, follower_count, recent_post_date,
       profile_image_url, url, errors}

    All fields except status/url/errors are None if not found.
    """
    result: dict = {
        "status": "no_url", "name": None, "current_title": None,
        "current_company": None, "location": None, "headline": None,
        "previous_companies": [], "follower_count": None,
        "recent_post_date": None, "recent_post_title": None,
        "profile_image_url": None, "url": None, "errors": [],
    }

    canonical = _normalise_url(linkedin_url)
    if not canonical:
        result["errors"].append("no /in/<slug> URL on lead")
        return result
    result["url"] = canonical

    if not scraper_client.is_available():
        result["status"] = "blocked"
        result["errors"].append("scraper disabled (wreq/proxy pool)")
        return result

    body = scraper_client.fetch(canonical, max_bytes=500_000)
    if not body:
        result["status"] = "blocked"
        result["errors"].append("fetch failed (LinkedIn 999 or proxy reject)")
        return result

    # ── JSON-LD first (richer) ──
    ld_match = _JSONLD_RE.search(body)
    person = None
    articles: list[dict] = []
    if ld_match:
        try:
            ld = json.loads(ld_match.group(1))
            person = _find_substantive_person(ld)
            if person:
                articles = _find_articles(ld, person.get("url") or canonical)
        except json.JSONDecodeError as e:
            result["errors"].append(f"JSON-LD parse: {str(e)[:80]}")

    if person:
        result["name"] = person.get("name")
        # jobTitle is a list of strings; first entry is the canonical role.
        titles = person.get("jobTitle") or []
        if titles:
            result["current_title"] = titles[0]
        # worksFor is a list of Organisation dicts; first is current employer.
        works = person.get("worksFor") or []
        if works and isinstance(works[0], dict):
            result["current_company"] = works[0].get("name")
        # address.addressLocality
        addr = person.get("address") or {}
        if isinstance(addr, dict):
            result["location"] = addr.get("addressLocality")
        # alumniOf is the "previously at X" list. Skip educational
        # organizations — operators care about work history, not schools.
        result["previous_companies"] = [
            o.get("name") for o in (person.get("alumniOf") or [])
            if isinstance(o, dict)
               and o.get("@type") == "Organization"
               and o.get("name")
        ]
        # interactionStatistic at the top level is followers.
        stat = person.get("interactionStatistic") or {}
        if isinstance(stat, dict) and stat.get("name") == "Follows":
            try:
                result["follower_count"] = int(stat.get("userInteractionCount") or 0) or None
            except (TypeError, ValueError):
                pass
        # description is the bio; longer than og:description usually.
        result["headline"] = person.get("description") or person.get("disambiguatingDescription")
        img = person.get("image") or {}
        if isinstance(img, dict):
            result["profile_image_url"] = img.get("contentUrl") or img.get("url")
        # Most recent post (if any)
        if articles:
            top = articles[0]
            result["recent_post_date"] = _parse_post_date(top)
            result["recent_post_title"] = top.get("headline")
        result["status"] = "verified"
        return result

    # ── Fallback: og:title (older, less rich, but always present
    #    when LinkedIn served a real profile page) ──
    og_title_m = _OG_TITLE_RE.search(body)
    if not og_title_m:
        result["status"] = "not_found"
        result["errors"].append("no JSON-LD Person and no og:title — deleted or redirected")
        return result
    name, current_title = _split_og_title(_decode_entities(og_title_m.group(1)))
    og_desc_m = _OG_DESC_RE.search(body)
    headline = _decode_entities(og_desc_m.group(1)) if og_desc_m else None
    result["status"] = "verified"
    result["name"] = name
    result["current_title"] = current_title
    result["headline"] = headline
    result["errors"].append("fell back to og:tags (no JSON-LD)")
    return result


def check_company(linkedin_company_url: str | None) -> dict:
    """Fetch + parse a LinkedIn /company/<slug> page.

    Returns the same shape as `check()` so the enricher can treat the
    two interchangeably. Most fields are None when sourced from a
    company page (current_title, location, previous_companies — those
    are personal-profile concepts). The useful signals here are:
      - name              — company display name
      - headline          — company description / tagline
      - follower_count    — company followers (often 5-50× higher than
                            an individual partner)
      - profile_image_url — company logo
      - recent_post_date  — most recent post on the company's feed
                            (signal for an active marketing presence)

    Status emitted: 'verified_company' on success so the operator can
    distinguish company-page enrichment from personal-profile.
    """
    result: dict = {
        "status": "no_url", "name": None, "current_title": None,
        "current_company": None, "location": None, "headline": None,
        "previous_companies": [], "follower_count": None,
        "recent_post_date": None, "recent_post_title": None,
        "profile_image_url": None, "url": None, "errors": [],
    }

    canonical = _normalise_company_url(linkedin_company_url)
    if not canonical:
        result["errors"].append("no /company/<slug> URL on lead")
        return result
    result["url"] = canonical

    if not scraper_client.is_available():
        result["status"] = "blocked"
        result["errors"].append("scraper disabled (wreq/proxy pool)")
        return result

    body = scraper_client.fetch(canonical, max_bytes=500_000)
    if not body:
        result["status"] = "blocked"
        result["errors"].append("fetch failed (LinkedIn 999 or proxy reject)")
        return result

    ld_match = _JSONLD_RE.search(body)
    org = None
    articles: list[dict] = []
    if ld_match:
        try:
            ld = json.loads(ld_match.group(1))
            org = _find_substantive_organisation(ld)
            if org:
                # Re-use the personal Article-walker — author URL on a
                # company page is the company URL itself. Posts shown
                # on the company feed are typically Articles authored
                # by the org.
                articles = _find_articles(ld, org.get("url") or canonical)
        except json.JSONDecodeError as e:
            result["errors"].append(f"JSON-LD parse: {str(e)[:80]}")

    if org:
        result["name"]    = org.get("name")
        # Org descriptions are usually 1-3 sentences — close enough
        # to headline for the operator UI.
        result["headline"] = org.get("description")
        stat = org.get("interactionStatistic") or {}
        if isinstance(stat, dict) and stat.get("name") == "Follows":
            try:
                result["follower_count"] = int(stat.get("userInteractionCount") or 0) or None
            except (TypeError, ValueError):
                pass
        logo = org.get("logo") or {}
        if isinstance(logo, dict):
            result["profile_image_url"] = logo.get("contentUrl") or logo.get("url")
        elif isinstance(logo, str):
            result["profile_image_url"] = logo
        if articles:
            top = articles[0]
            result["recent_post_date"] = _parse_post_date(top)
            result["recent_post_title"] = top.get("headline")
        result["status"] = "verified_company"
        return result

    # Fallback: og:title for company pages reads like
    # "Acme Accountants | LinkedIn" — name only, no headline.
    og_title_m = _OG_TITLE_RE.search(body)
    if not og_title_m:
        result["status"] = "not_found"
        result["errors"].append("no JSON-LD Organisation and no og:title — page redirected?")
        return result
    raw_title = _decode_entities(og_title_m.group(1)) or ""
    name_only = _LI_SUFFIX_RE.sub("", raw_title).strip() or None
    og_desc_m = _OG_DESC_RE.search(body)
    headline = _decode_entities(og_desc_m.group(1)) if og_desc_m else None
    result["name"]     = name_only
    result["headline"] = headline
    result["status"]   = "verified_company"
    result["errors"].append("fell back to og:tags (no JSON-LD organisation)")
    return result


# ── Pipeline adapter ────────────────────────────────────────────────
name = "linkedin"


def enrich(business: dict) -> dict:
    """Annotate the business dict with everything `check()` returns,
    persisted as the linkedin_* columns. Never raises.

    Two-tier strategy:
      1. Try the personal /in/ URL first when present — it gives the
         richest data (current_title, headline, work history).
      2. If no /in/ URL OR the personal fetch failed (blocked /
         not_found), fall back to the /company/ URL when available.
         This unblocks LinkedIn enrichment on the ~80% of UK SMB
         targets that only link a company page.
    """
    url     = (business.get("linkedin_url") or "").strip() or None
    co_url  = (business.get("linkedin_company_url") or "").strip() or None

    if not _enrich_enabled():
        # Feature off — record an explicit status so operator can see
        # WHY LinkedIn data is missing rather than silently failing.
        business["linkedin_status"] = "disabled"
        business["linkedin_last_checked_at"] = datetime.now(timezone.utc)
        return business

    try:
        r = check(url)
    except Exception as e:
        logger.exception("linkedin check failed for %s", str(url)[:80])
        business.setdefault("source_errors", {})["linkedin"] = f"unexpected: {str(e)[:120]}"
        business["linkedin_status"] = "blocked"
        return business

    # Fall back to the company page when the personal lookup didn't
    # give us anything usable. We bias toward "any signal beats none"
    # — a company follower count + recent-post date still feeds the
    # scoring + drafter, even if we don't get a personal headline.
    if r["status"] in ("no_url", "blocked", "not_found") and co_url:
        try:
            r2 = check_company(co_url)
        except Exception as e:
            logger.exception("linkedin company check failed for %s", str(co_url)[:80])
            r2 = None
        if r2 and r2["status"] == "verified_company":
            # Merge: keep any non-None field from the personal attempt
            # (rare, but possible — e.g. og:title fallback recovered
            # the name), and fill the rest from the company page.
            for k, v in r2.items():
                if k == "errors":
                    continue
                if r.get(k) is None or r.get(k) == [] or k == "status":
                    r[k] = v
            r["errors"].extend(r2.get("errors") or [])

    business["linkedin_status"]            = r["status"]
    business["linkedin_current_title"]     = r["current_title"]
    business["linkedin_headline"]          = r["headline"]
    business["linkedin_current_company"]   = r["current_company"]
    business["linkedin_location"]          = r["location"]
    business["linkedin_previous_companies"] = r["previous_companies"] or None
    business["linkedin_follower_count"]    = r["follower_count"]
    business["linkedin_profile_image_url"] = r["profile_image_url"]
    business["linkedin_recent_post_at"]    = r["recent_post_date"]
    business["linkedin_recent_post_title"] = r["recent_post_title"]
    business["linkedin_last_checked_at"]   = datetime.now(timezone.utc)

    # Backfill decision_maker_name from LinkedIn personal-profile only.
    # Company-page `name` is the firm, not the person — would wrongly
    # overwrite e.g. "Smith, John" with "Acme Accountants".
    if r["status"] == "verified" and r["name"] and not business.get("decision_maker_name"):
        business["decision_maker_name"] = r["name"]
    if r["errors"]:
        business.setdefault("source_errors", {})["linkedin"] = " | ".join(r["errors"])[:240]
    return business
