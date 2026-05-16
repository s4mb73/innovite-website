"""LinkedIn — decision-maker verification & title refresh (Stage 4).

Realistic scope (HTTP-only)
---------------------------
LinkedIn is the most aggressively bot-defended consumer surface on
the public internet. wreq's TLS-fingerprint impersonation + UK
residential proxies gets us through *sometimes* on /in/<slug>
profile pages and *almost never* on /company/<slug> pages. There
is no browser-automation fallback per the operator's "HTTPS only"
constraint, so the deliverable is bounded by what LinkedIn exposes
without auth: the og:title + og:description meta tags on the
public profile teaser.

What we extract per profile (when fetch succeeds)
-------------------------------------------------
  - name (the "Name" segment of og:title before the first " - ")
  - current_title (the headline segment after " - " and before
    " | LinkedIn", which is current role + employer)
  - headline (raw og:description — useful for the drafter as
    outreach context)

What we do NOT extract (would need auth)
----------------------------------------
  - Work history ("previously at Deloitte")
  - Activity ("posts weekly", post counts)
  - Mutual connections
  - Employee-count band on /company/ pages

What this adds vs. existing Apollo data
---------------------------------------
Apollo already supplies decision_maker_name + decision_maker_title
+ linkedin_url. The LinkedIn fetch refreshes the title (Apollo data
ages — people change jobs and Apollo lags weeks-to-months) and
acts as a liveness check on the URL (a 'verified' status confirms
the lead's profile still resolves to a real person).

Statuses
--------
  verified    — fetched OK, og:title parsed; name+title populated
  blocked     — fetch failed (LinkedIn 999 / proxy reject / etc.)
  not_found   — fetched but og:title absent (deleted profile, redirect)
  no_url      — no usable LinkedIn URL on the lead
"""
from __future__ import annotations

import html
import logging
import re
import urllib.parse
from datetime import datetime, timezone

from scraper import client as scraper_client

logger = logging.getLogger("crm.scraper.linkedin")

# Match URLs we can actually do something with — only public /in/<slug>
# profile pages. /company/<slug> is mostly blocked at probe time so we
# explicitly reject those rather than burn a proxy on a guaranteed miss.
_VALID_IN_PATH = re.compile(r"^/in/[A-Za-z0-9_\-%.]+/?$")

# og:title pattern. LinkedIn always renders this on /in/ pages with the
# shape "Full Name - Headline | LinkedIn" (the " | LinkedIn" suffix is
# present on profiles linked to corporate accounts and absent on many
# personal-only profiles, so we strip it optionally).
_OG_TITLE_RE = re.compile(r'property="og:title"\s+content="([^"]+)"')
_OG_DESC_RE  = re.compile(r'property="og:description"\s+content="([^"]+)"')
_OG_TYPE_RE  = re.compile(r'property="og:type"\s+content="([^"]+)"')
_LI_SUFFIX_RE = re.compile(r"\s*\|\s*LinkedIn\s*$")


def _normalise_url(url: str | None) -> str | None:
    """Accept variants like 'linkedin.com/in/foo', '/in/foo', or full
    https URLs with query strings. Returns a canonical
    https://www.linkedin.com/in/<slug> URL or None if not /in/<slug>."""
    if not url:
        return None
    u = url.strip()
    if not u:
        return None
    # Add scheme if missing.
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
    # Canonicalise on www.linkedin.com (uk.linkedin.com also works but
    # www. is the form LinkedIn redirects to and the form we probed).
    return f"https://www.linkedin.com{path}"


def _decode_html_entities(s: str) -> str:
    """LinkedIn double-encodes og: meta values — the source shows things
    like 'AI &amp;amp; Inflection' meaning '&' was encoded twice during
    SSR. One unescape pass per layer."""
    out = html.unescape(s or "")
    if "&amp;" in out or "&#" in out:
        out = html.unescape(out)
    return out.strip()


def _split_title(og_title: str) -> tuple[str | None, str | None]:
    """og:title shape is 'Full Name - Headline... | LinkedIn'. Returns
    (name, headline). Either may be None if the split is degenerate."""
    if not og_title:
        return None, None
    # Strip trailing " | LinkedIn" if present.
    cleaned = _LI_SUFFIX_RE.sub("", og_title).strip()
    # Split on first " - " (LinkedIn uses ASCII hyphen with spaces).
    if " - " not in cleaned:
        # Some profiles render only the name with no headline.
        return cleaned or None, None
    name, _, headline = cleaned.partition(" - ")
    return name.strip() or None, headline.strip() or None


# ── Public entry ────────────────────────────────────────────────────
def check(linkedin_url: str | None) -> dict:
    """Look up a LinkedIn /in/ profile and extract public meta data.

    Returns:
      {status, name, current_title, headline, url, errors}
        status:        'verified' | 'blocked' | 'not_found' | 'no_url'
        name:          str | None (extracted from og:title)
        current_title: str | None (headline portion of og:title)
        headline:      str | None (raw og:description)
        url:           str | None (canonicalised URL we fetched)
        errors:        list of short strings for debugging
    """
    result: dict = {"status": "no_url", "name": None, "current_title": None,
                    "headline": None, "url": None, "errors": []}

    canonical = _normalise_url(linkedin_url)
    if not canonical:
        result["errors"].append("no /in/<slug> URL on lead")
        return result
    result["url"] = canonical

    if not scraper_client.is_available():
        result["status"] = "blocked"
        result["errors"].append("scraper disabled (wreq/proxy pool)")
        return result

    body = scraper_client.fetch(canonical)
    if not body:
        result["status"] = "blocked"
        result["errors"].append("fetch failed (LinkedIn 999 or proxy reject)")
        return result

    og_type = (_OG_TYPE_RE.search(body) or [None, ""])[1] if _OG_TYPE_RE.search(body) else ""
    title_m = _OG_TITLE_RE.search(body)
    if not title_m:
        # Page loaded but no og:title — LinkedIn served the auth-wall
        # shell or a deleted-profile redirect.
        result["status"] = "not_found"
        result["errors"].append("og:title absent in response body")
        return result

    og_title = _decode_html_entities(title_m.group(1))
    desc_m = _OG_DESC_RE.search(body)
    og_desc = _decode_html_entities(desc_m.group(1)) if desc_m else None

    name, current_title = _split_title(og_title)
    result["status"]        = "verified"
    result["name"]          = name
    result["current_title"] = current_title
    result["headline"]      = og_desc
    return result


# ── Pipeline adapter ────────────────────────────────────────────────
name = "linkedin"


def enrich(business: dict) -> dict:
    """Annotate the business dict with linkedin_status, refreshed title,
    and headline. Never raises. Skipped silently when the lead has no
    /in/ URL — Apollo's data stands alone in that case."""
    url = (business.get("linkedin_url") or "").strip() or None
    try:
        r = check(url)
    except Exception as e:
        logger.exception("linkedin check failed for %s", str(url)[:80])
        business.setdefault("source_errors", {})["linkedin"] = f"unexpected: {str(e)[:120]}"
        business["linkedin_status"] = "blocked"
        return business

    business["linkedin_status"]            = r["status"]
    business["linkedin_current_title"]     = r["current_title"]
    business["linkedin_headline"]          = r["headline"]
    business["linkedin_last_checked_at"]   = datetime.now(timezone.utc)
    # If LinkedIn returned a fresher name than Apollo, prefer it on
    # the lead — the drafter will use whichever field is populated.
    if r["status"] == "verified" and r["name"] and not business.get("decision_maker_name"):
        business["decision_maker_name"] = r["name"]
    if r["errors"]:
        business.setdefault("source_errors", {})["linkedin"] = " | ".join(r["errors"])[:240]
    return business
