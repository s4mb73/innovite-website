"""Extract the Instagram handle a website links to.

Given a business website URL, fetch the homepage (and a couple of
likely contact/about pages if homepage has no IG link) via the proxy
pool, then regex out the first instagram.com/<handle> reference.

Returns the handle as a lowercase string, or None.

The Vidora discovery flow is:
    Google Places → business + website
    → instagram_link.find_for_website(website)  ← THIS MODULE
    → instagram_snapshot.snapshot(handle)
    → vidora_audit.audit(snapshot)

Handle parsing
--------------
Real-world IG links seen in footers / contact pages:
    https://www.instagram.com/foo.bar/
    https://instagram.com/foo_bar?hl=en
    http://www.instagram.com/foo.bar
    href="https://instagram.com/foo.bar/" data-track=...
    @foo.bar  (rare, plain-text only — we ignore these)

We also reject paths that aren't profiles:
    /reels/, /p/, /tv/, /explore/, /accounts/, /developer/, etc.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

from scraper import client

logger = logging.getLogger("crm.scraper.instagram_link")

# Match instagram.com/<handle>. Handle = 1-30 chars, [A-Za-z0-9._].
# Anchored on the IG domain to avoid matching random "instagram" mentions.
_IG_RE = re.compile(
    r"(?:https?:)?//(?:www\.)?instagram\.com/([A-Za-z0-9._]{1,30})(?:/|\?|#|$|[\"'])",
    re.IGNORECASE,
)

# Paths that are NOT user handles — IG namespaces them.
_NON_PROFILE_PATHS = {
    "p", "reel", "reels", "tv", "explore", "accounts", "directory",
    "developer", "about", "press", "api", "legal", "blog", "challenge",
    "stories", "create", "_n", "_u", "session", "ads", "web",
}

# Likely paths to check if homepage has no IG link. Most real SMB
# websites put social icons on the homepage, but the contact page is
# the common fallback.
_FALLBACK_PATHS = ("/contact", "/contact-us", "/about", "/about-us", "/get-in-touch")


def extract_from_html(html: str) -> str | None:
    """Find the first plausible Instagram handle in an HTML blob."""
    if not html:
        return None
    for match in _IG_RE.finditer(html):
        candidate = match.group(1).lower().strip("._")
        if not candidate:
            continue
        if candidate in _NON_PROFILE_PATHS:
            continue
        # Strip trailing period (IG handles can't end on a period
        # for our purposes — most likely a typo / sentence end).
        candidate = candidate.rstrip(".")
        if 1 <= len(candidate) <= 30:
            return candidate
    return None


def find_for_website(website_url: str) -> dict:
    """Locate the IG handle for a business website.

    Returns:
      {
        "handle":        str | None,
        "found_on":      str | None,        # which URL the link was on
        "pages_tried":   [url, ...],
        "homepage_body": str | None,        # raw HTML of the homepage when fetched
        "found_on_body": str | None,        # raw HTML of the page where the handle was found
      }

    homepage_body + found_on_body are surfaced so downstream callers
    (e.g. MediaMode email scrape) can reuse them instead of re-fetching
    the same pages. They're optional fields — callers that don't care
    can ignore them.
    """
    result: dict = {
        "handle":        None,
        "found_on":      None,
        "pages_tried":   [],
        "homepage_body": None,
        "found_on_body": None,
    }

    if not website_url or not client.is_available():
        return result

    # Normalise to a fetchable origin URL.
    parsed = urlparse(website_url)
    if not parsed.scheme:
        website_url = "https://" + website_url
        parsed = urlparse(website_url)
    if not parsed.hostname:
        return result

    origin = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        origin += f":{parsed.port}"

    # Try homepage first, then a couple of common fallback paths.
    pages = [website_url]
    # Avoid duplicating the homepage if website_url already is a /contact etc.
    if urlparse(website_url).path in ("", "/"):
        pages += [urljoin(origin + "/", p.lstrip("/")) for p in _FALLBACK_PATHS]

    for i, url in enumerate(pages):
        result["pages_tried"].append(url)
        body = client.fetch(url)
        if not body:
            continue
        # First fetched page is always the homepage in this flow.
        if i == 0 and result["homepage_body"] is None:
            result["homepage_body"] = body
        handle = extract_from_html(body)
        if handle:
            result["handle"]        = handle
            result["found_on"]      = url
            result["found_on_body"] = body
            return result

    return result
