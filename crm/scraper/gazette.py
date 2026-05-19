"""The Gazette — distress-signal lookup for UK SMB leads.

Why this matters
----------------
The Gazette is the UK government's official journal of record. Every
insolvency event (winding-up petition, administrator appointment,
creditors' voluntary liquidation, strike-off notice, etc.) gets
published here. For an accountancy firm prospecting, a target with
an active Gazette insolvency notice is either:
  - GOLD if the client offers turnaround / insolvency advisory
  - SKIP if they're general-practice (you won't beat the appointed IP)

Either way, knowing is high-leverage information.

How we query
------------
The Gazette exposes a JSON search endpoint (no auth, no anti-bot):
  https://www.thegazette.co.uk/all-notices/notice/data.json
       ?text="<EXACT_PHRASE>"
       &categories-notices=insolvency
       &numberOfLinksToView=10

We always search with the **exact business name quoted** + insolvency
category filter. This narrows from ~1000s of fuzzy matches to ~0-20
real ones. We then verify each entry's content snippet actually
contains the business name (the JSON endpoint's text search is
documented as fulltext, so quotes alone aren't a hard guarantee).

Matching is intentionally conservative — false positives flag a clean
lead as distressed, which costs the operator one click of investigation
to disprove. False negatives miss a real distress signal entirely.
We bias toward "we'd rather miss some than mis-flag many".

Output (None = couldn't query / too ambiguous to claim):
  status:  'clear' | 'distressed' | 'unknown'
  count:   number of confident insolvency-notice matches
  last_date: most recent notice publication date
  last_url:  link to the most recent matching notice
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from datetime import date, datetime

from scraper import client as scraper_client

logger = logging.getLogger("crm.scraper.gazette")

GAZETTE_BASE = "https://www.thegazette.co.uk"
SEARCH_URL = f"{GAZETTE_BASE}/all-notices/notice/data.json"

# Minimum length (post suffix-strip) for the name we'll actually query.
# Two thresholds because Companies House number disambiguates noisy
# names — when we have it, we can accept short names because the CH
# number verification step will catch false positives. Without CH, we
# need a longer/rarer name to limit false matches at search time.
_MIN_NAME_LEN_WITH_CH    = 4   # Tesco, Greggs are fine if CRN known
_MIN_NAME_LEN_WITHOUT_CH = 8   # Carillion ok; Tesco/BHS/Smith rejected
_AMBIGUOUS_SUFFIXES = {"ltd", "limited", "plc", "llp", "uk", "the", "and", "co"}

# Cap on entries we examine per query. We're looking for a binary signal
# (any genuine match → distressed), so 20 is plenty. Speeds up worst-
# case "company name is a common English word" queries that return
# pages of false positives.
_MAX_ENTRIES = 20


def _strip_html(s: str) -> str:
    """Gazette's `content` field has highlighted spans wrapping matches.
    We only need plain text for verification."""
    return re.sub(r"<[^>]+>", " ", s or "")


def _normalise_name(name: str) -> str:
    """Lowercase, collapse whitespace, strip the corporate-form suffix
    so 'ROCA Accountants Ltd' matches 'Roca Accountants Limited' in
    free-text Gazette entries."""
    n = (name or "").lower().strip()
    n = re.sub(r"\b(limited|ltd|plc|llp|uk)\b\.?", "", n)
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def _is_too_ambiguous(name: str, *, have_ch: bool) -> bool:
    """Reject queries that would produce a flood of false positives.
    Threshold relaxes when the CH number is available because that
    becomes the disambiguator in the verification pass."""
    norm = _normalise_name(name)
    floor = _MIN_NAME_LEN_WITH_CH if have_ch else _MIN_NAME_LEN_WITHOUT_CH
    return len(norm) < floor


def _verify_entry_matches(entry: dict, normalised_name: str,
                          ch_number: str | None) -> bool:
    """Return True iff the entry's content matches our target lead.
    The Gazette search is fulltext over decades of PDF-OCR'd notices —
    quotes alone don't guarantee the company we want is the actual
    subject of the notice, so we re-verify here.

    Rule:
      - Name must appear in the entry text.
      - If we have a CH number, it must appear as a WHOLE TOKEN
        (not as a substring inside a longer digit run — that was
        the bug that flagged active accountancy firms as 'distressed'
        when their 7-digit CH number happened to be a substring of
        an unrelated 11-digit ref number elsewhere on the page).
    Both conditions → confident match. Anything weaker is discarded
    rather than falsely flagged.
    """
    blob = _strip_html(
        (entry.get("content") or "") + " " + (entry.get("title") or "")
    ).lower()
    if normalised_name and normalised_name not in blob:
        return False
    if ch_number:
        # CH numbers in Gazette notices appear as "Company No. 08741703"
        # — a standalone digit token (with or without leading zeros).
        # Use \b word-boundary anchors against the raw text. Try both
        # the padded and unpadded forms because the Gazette publishes
        # historical notices in both styles.
        padded   = ch_number.lstrip("0") or "0"
        unpadded = ch_number.lstrip("0") or "0"
        full     = ch_number.zfill(8)
        # \b doesn't match between two digits, so a 7-digit number
        # embedded in a longer digit run won't match — exactly what
        # we want. Letter-prefixed numbers (SC/NI/OC) also match.
        if not re.search(rf"\b{re.escape(full)}\b", blob) \
                and not re.search(rf"\b{re.escape(unpadded)}\b", blob):
            return False
    return True


def _verify_strict_no_ch(entry: dict, normalised_name: str) -> bool:
    """When we don't have a CH number to cross-check, name-only matching
    is too loose — the company name might appear coincidentally (a
    person named the same, an unrelated company with similar branding,
    or a director listed in someone else's insolvency proceedings).
    Require additional anchors:

      - Name appears with a corporate suffix nearby (limited / ltd /
        plc / llp) within 50 chars, OR
      - Name appears in the entry TITLE (not just body content).

    A miss here returns False → status='clear'/'unknown' rather than
    'distressed', biasing toward false negatives over false positives.
    A false-positive distress flag is operationally bad (would prompt
    'sorry to hear about your insolvency' email to a thriving firm).
    """
    title = _strip_html(entry.get("title") or "").lower()
    if normalised_name in title:
        return True

    content = _strip_html(entry.get("content") or "").lower()
    if normalised_name not in content:
        return False

    # Look for any corporate suffix within 50 chars of the name match.
    idx = content.find(normalised_name)
    window = content[max(0, idx - 5): idx + len(normalised_name) + 50]
    return bool(re.search(r"\b(limited|ltd|plc|llp)\b", window))


def _parse_published(entry: dict) -> date | None:
    raw = entry.get("published") or ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def _absolute_url(entry: dict) -> str | None:
    eid = entry.get("id") or ""
    if eid.startswith("http"):
        return eid
    if eid.startswith("/"):
        return GAZETTE_BASE + eid
    return None


# ── Public entry ────────────────────────────────────────────────────
def check(business_name: str, *, companies_house_number: str | None = None) -> dict:
    """Look up a business in the Gazette insolvency notices.

    Returns:
      {status, count, last_date, last_url, errors}
      status:  'clear' | 'distressed' | 'unknown'
      count:   int (verified matches only)
      last_date: date | None
      last_url:  str | None
      errors:   list of short strings for debugging
    """
    result: dict = {"status": "unknown", "count": 0,
                    "last_date": None, "last_url": None, "errors": []}

    if not business_name or _is_too_ambiguous(business_name, have_ch=bool(companies_house_number)):
        result["errors"].append("name too ambiguous to query confidently")
        return result

    if not scraper_client.is_available():
        result["errors"].append("scraper disabled (wreq/proxy pool)")
        return result

    norm_name = _normalise_name(business_name)
    quoted = f'"{business_name.strip()}"'
    params = urllib.parse.urlencode({
        "text": quoted,
        "numberOfLinksToView": _MAX_ENTRIES,
        "categories-notices": "insolvency",
    })
    body = scraper_client.fetch(f"{SEARCH_URL}?{params}")
    if not body:
        result["errors"].append("fetch failed")
        return result

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        result["errors"].append("invalid JSON response")
        return result

    entries = data.get("entry") or []
    if companies_house_number:
        verified: list[dict] = [
            e for e in entries
            if _verify_entry_matches(e, norm_name, companies_house_number)
        ]
    else:
        # No CH disambiguator available — apply stricter name-only rules
        # to avoid false-positive distress flags.
        verified = [
            e for e in entries
            if _verify_strict_no_ch(e, norm_name)
        ]

    if not verified:
        result["status"] = "clear"
        return result

    # Find the most recent by publication date.
    most_recent = max(verified, key=lambda e: _parse_published(e) or date.min)
    result["status"]    = "distressed"
    result["count"]     = len(verified)
    result["last_date"] = _parse_published(most_recent)
    result["last_url"]  = _absolute_url(most_recent)
    return result


# ── Pipeline adapter ────────────────────────────────────────────────
# Thin wrapper matching the EnrichmentSource protocol so the runner
# can call this just like companies_house.enrich / decision_maker.enrich.

name = "gazette"


def enrich(business: dict) -> dict:
    """Annotate the business dict with gazette_status, count, last_date,
    last_url. Never raises — failures attach to source_errors instead.

    Defers to CH status when present: if Companies House currently
    says the company is 'active', any Gazette match is historical
    (strike-off threats they recovered from, old administrator
    discharges, etc.) and we override status to 'clear'. Real
    operational distress shows up in CH status, not Gazette history.
    """
    bn  = (business.get("business_name") or "").strip()
    crn = (business.get("companies_house_number") or "").strip() or None
    try:
        r = check(bn, companies_house_number=crn)
    except Exception as e:
        logger.exception("gazette check failed for %s", bn[:80])
        business.setdefault("source_errors", {})["gazette"] = f"unexpected: {str(e)[:120]}"
        business["gazette_status"] = "unknown"
        return business

    ch_status = (business.get("companies_house_status") or "").lower().strip()
    if r["status"] == "distressed" and ch_status == "active":
        # Keep the count + URL for audit (operator can click through
        # to see the historical record), but downgrade the flag so
        # the drafter doesn't pick gazette_distressed as the hook.
        business["gazette_status"]           = "clear"
        business["gazette_notice_count"]     = r["count"]
        business["gazette_last_notice_date"] = r["last_date"]
        business["gazette_last_notice_url"]  = r["last_url"]
        business.setdefault("source_errors", {})["gazette"] = (
            f"{r['count']} historical notices; CH currently active — flag suppressed"
        )
        return business

    business["gazette_status"]           = r["status"]
    business["gazette_notice_count"]     = r["count"]
    business["gazette_last_notice_date"] = r["last_date"]
    business["gazette_last_notice_url"]  = r["last_url"]
    if r["errors"]:
        business.setdefault("source_errors", {})["gazette"] = " | ".join(r["errors"])[:240]
    return business
