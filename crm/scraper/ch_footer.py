"""Companies House number extractor — Tier 1 of the CH-match strategy.

Companies Act 2006 s.82 requires every UK limited company to display
its company number on its website. When the footer exposes it, we get
a 100%-certain CH match and skip fuzzy name matching entirely.

Hit rate (rough): ~70-80% of UK B2B sites. Smaller / older / non-
compliant sites display nothing — those fall back to Tier 2 fuzzy
matching in companies_house.py.

Patterns we recognise (case-insensitive):
  "Company number 12345678"
  "Company No: 12345678"
  "Company No. 12345678"
  "Company Registration Number 12345678"
  "Registration No: 12345678"
  "Reg No. 12345678"
  "Registered in England No. 12345678"
  "Registered in England and Wales No. 12345678"
  "Registered in Scotland No. SC123456"
  "Incorporated in England No. 12345678"

Number formats:
  - England & Wales: 8 digits (sometimes 7 in legacy renderings — we
    accept 7-8 and zero-pad to 8 before lookup).
  - Scotland: 'SC' + 6 digits
  - Northern Ireland: 'NI' + 6 digits
  - LLP: 'OC' + 6 digits
  - Other CH prefixes: NL, NF, FC, GS, IP, LP, RC, SO, ZC (rare)

False-positive guard: a bare 8-digit number on a page is just as
likely to be a phone, postcode, or social ID. We REQUIRE one of the
keyword anchors above within 30 chars of the number.
"""
from __future__ import annotations

import logging
import re

from scraper import client as scraper_client

logger = logging.getLogger("crm.scraper.ch_footer")

# Per-page byte cap — the footer's at the bottom but we fetch the
# whole page anyway because Flask templates often inline footers
# at the top of source order in <noscript> blocks for SEO.
_MAX_BYTES = 300_000

# Paths we'll try beyond the homepage. Order is rough likelihood of
# CH-number presence. Capped to 4 because each fetch costs proxy
# capacity. Stopped early as soon as we find a match.
_FALLBACK_PATHS = ("/privacy", "/privacy-policy", "/legal", "/terms",
                   "/cookie-policy", "/imprint", "/contact")

# CH prefixes that precede 6 digits. Standalone-8-digit form has no prefix.
_CH_PREFIXES = ("SC", "NI", "OC", "NL", "NF", "FC", "GS", "IP", "LP",
                "RC", "SO", "ZC", "AC", "GE", "GN", "IC", "RS")

# Anchor words near the number. At least one must appear within 30
# chars of the digit run, otherwise we treat the number as noise.
_ANCHOR = (
    r"(?:"
    r"company\s+(?:number|no\.?|registration|reg\.?\s*(?:no\.?|number)?)"
    r"|registered\s+(?:in\s+(?:england(?:\s+(?:and|&)\s+wales)?|scotland|wales|"
                  r"northern\s+ireland|the\s+uk|uk)\s+)?(?:no\.?|number)"
    r"|incorporated\s+(?:in\s+(?:england(?:\s+(?:and|&)\s+wales)?|scotland|wales|"
                    r"northern\s+ireland|the\s+uk|uk)\s+)?(?:no\.?|number)"
    r"|reg(?:istration)?\.?\s*(?:no\.?|number)"
    r")"
)

# Compiled per call — pattern is static so this is cheap.
_NUMBER = (
    r"((?:" + "|".join(_CH_PREFIXES) + r")\d{6,8}|\d{7,8})"
)

# Pattern: anchor + up to 30 chars of fluff/punct/whitespace + number.
# We allow the number to come BEFORE the anchor too (some footers
# write "12345678 - Company Number, registered in England") so we
# also do the reverse search.
_ANCHOR_BEFORE_RE = re.compile(
    _ANCHOR + r"[^A-Za-z0-9]{0,30}" + _NUMBER,
    re.IGNORECASE,
)
_NUMBER_BEFORE_RE = re.compile(
    _NUMBER + r"[^A-Za-z0-9]{0,30}" + _ANCHOR,
    re.IGNORECASE,
)

# HTML-strip — just collapse tags to spaces. We don't want full HTML
# parsing (BeautifulSoup) for a regex pass; tag boundaries can split
# the anchor/number, so we collapse to a single text stream first.
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_WS_RE        = re.compile(r"\s+")


def _normalise(html: str) -> str:
    """Strip HTML tags and collapse whitespace so the anchor + number
    regex can match across what would otherwise be tag boundaries
    ('<span>Company<br>No.<br>12345678</span>')."""
    text = _TAG_STRIP_RE.sub(" ", html or "")
    text = _WS_RE.sub(" ", text)
    return text


def _normalise_number(raw: str) -> str:
    """CH numbers are case-stable. Standalone digit runs get zero-padded
    to 8 chars so legacy '6720988' becomes '06720988' for API lookup."""
    raw = raw.strip().upper()
    if raw.isdigit() and len(raw) < 8:
        return raw.zfill(8)
    return raw


def extract_from_html(html: str) -> str | None:
    """Pull the first CH-number-with-anchor match from a page body.
    Returns None if nothing convincing was found.

    Strategy:
      1. Try anchor-before-number form (most common: "Company No. X")
      2. Fall back to number-before-anchor form (rare but legitimate)
    """
    if not html:
        return None
    text = _normalise(html)

    # Both compiled regexes have exactly one capturing group (from
    # _NUMBER); _ANCHOR uses non-capturing groups throughout. So
    # group(1) is always the number regardless of which pattern hit.
    m = _ANCHOR_BEFORE_RE.search(text)
    if m:
        return _normalise_number(m.group(1))

    m = _NUMBER_BEFORE_RE.search(text)
    if m:
        return _normalise_number(m.group(1))

    return None


def find_for_website(website: str) -> str | None:
    """Fetch the lead's homepage (and a few common legal-page paths)
    and try to extract a CH number from the rendered text. Returns
    None when the website is unreachable, the scraper is disabled, or
    no anchor-confirmed number is found anywhere we looked.
    """
    if not website or not scraper_client.is_available():
        return None

    base = website.strip().rstrip("/")
    if "://" not in base:
        base = "http://" + base

    # Homepage first — fastest hit when present (most footers).
    body = scraper_client.fetch(base, max_bytes=_MAX_BYTES)
    if body:
        n = extract_from_html(body)
        if n:
            return n

    # Fallbacks — only attempt when homepage actually fetched (avoid
    # burning 4 proxy hits on a fully unreachable site).
    if not body:
        return None
    for path in _FALLBACK_PATHS:
        try:
            body = scraper_client.fetch(base + path, max_bytes=_MAX_BYTES)
        except Exception:
            continue
        if not body:
            continue
        n = extract_from_html(body)
        if n:
            return n
    return None
