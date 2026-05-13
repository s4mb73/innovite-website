"""Companies House enrichment.

For each business with a name + UK postcode, query Companies House to
attach: company number, SIC code, incorporation date, officer count,
filing-type-derived revenue band, and active/dissolved status.

Auth: COMPANIES_HOUSE_API_KEY env var (basic auth, key as username, no
password). The key is IP-restricted to the VPS at the CH end.

Rate limit: 600 requests / 5 minutes shared across the key. We sleep
500ms between calls — even a 200-lead run with two CH calls per lead
(search + profile) stays comfortably inside budget.

Matching strategy
-----------------
CH search is fuzzy. We accept a match only if:
  (a) name token overlap > 60% AND
  (b) postcode area (first 2-4 chars before space) matches.

Below-threshold matches return the business unenriched, with a
source_errors entry noting "no CH match". The lead still gets ingested
— scoring just downgrades it for missing CH signals.

Revenue band heuristic
----------------------
UK SMBs rarely file turnover. We derive a band from the filing TYPE:
  - micro-entity   →  < £632k     (small company exemption)
  - abridged       →  £632k–£10.2m
  - full           →  £10.2m+
This is correlated with company size *by statute*, not survey. A
company can choose to file fuller than required, so the band is a
floor estimate, not the actual figure. Documented and surfaced as such.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
import urllib.request

from pipeline.sources import Business

CH_SEARCH_URL = "https://api.company-information.service.gov.uk/search/companies"
CH_PROFILE_URL = "https://api.company-information.service.gov.uk/company/{number}"
CH_OFFICERS_URL = "https://api.company-information.service.gov.uk/company/{number}/officers"

PER_CALL_SLEEP_S = 0.5

name = "companies_house"


def _auth_header() -> dict[str, str]:
    key = os.environ.get("COMPANIES_HOUSE_API_KEY")
    if not key:
        raise RuntimeError(
            "COMPANIES_HOUSE_API_KEY not set. Already in /etc/innovite/crm-worker.env."
        )
    token = base64.b64encode(f"{key}:".encode()).decode()
    return {"Authorization": f"Basic {token}", "User-Agent": "InnoviteCRM/1.0"}


def _http_get(url: str) -> dict | None:
    req = urllib.request.Request(url, headers=_auth_header())
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        if e.code == 429:
            # Rate-limited — back off and retry once.
            time.sleep(10)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        raise


def _postcode_area(pc: str) -> str:
    """First 2-4 chars before the space — e.g. 'SW1A 1AA' -> 'SW1A'."""
    return (pc.split(" ")[0] if " " in pc else pc[:4]).upper().strip()


def _name_overlap(a: str, b: str) -> float:
    """Token-set Jaccard overlap, lower-cased, stripped of corporate suffixes."""
    def tokens(s: str) -> set[str]:
        s = s.lower()
        for suffix in (" ltd", " limited", " plc", " llp", " uk", " group", " holdings"):
            s = s.replace(suffix, "")
        return {t for t in s.replace(",", " ").split() if t and len(t) > 1}

    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _revenue_band_from_accounts(profile: dict) -> str | None:
    """Heuristic from filing type. See module docstring."""
    accounts = profile.get("accounts") or {}
    last_accounts = accounts.get("last_accounts") or {}
    accounts_type = (last_accounts.get("type") or "").lower()

    if "micro" in accounts_type:
        return "<£632k (micro)"
    if "abridged" in accounts_type or "small" in accounts_type or "filleted" in accounts_type:
        return "£632k–£10.2m (small)"
    if "full" in accounts_type or "medium" in accounts_type:
        return "£10.2m+ (medium/full)"
    if "dormant" in accounts_type:
        return "dormant"
    return None


def _search_match(business_name: str, postcode: str) -> dict | None:
    """Best CH search hit for (name, postcode) above the match threshold."""
    if not business_name or not postcode:
        return None

    time.sleep(PER_CALL_SLEEP_S)
    qs = urllib.parse.urlencode({"q": business_name, "items_per_page": 10})
    data = _http_get(f"{CH_SEARCH_URL}?{qs}") or {}

    target_area = _postcode_area(postcode)
    best: tuple[float, dict] | None = None

    for item in data.get("items", []):
        if item.get("kind") != "searchresults#company":
            continue
        cand_name = item.get("title", "")
        cand_pc = (item.get("address", {}) or {}).get("postal_code", "")
        if not cand_pc:
            continue

        name_score = _name_overlap(business_name, cand_name)
        pc_match = _postcode_area(cand_pc) == target_area

        # Threshold: name overlap >= 0.6 AND postcode area matches.
        if name_score >= 0.6 and pc_match:
            score = name_score + (0.1 if item.get("company_status") == "active" else 0.0)
            if best is None or score > best[0]:
                best = (score, item)

    return best[1] if best else None


def enrich(business: Business) -> Business:
    """Attach CH fields to the business if a confident match exists."""
    bname = business.get("business_name", "")
    pc = business.get("postcode", "")

    if not bname or not pc:
        business.setdefault("source_errors", {})["companies_house"] = "missing name or postcode"
        return business

    try:
        match = _search_match(bname, pc)
    except Exception as e:
        business.setdefault("source_errors", {})["companies_house"] = f"search failed: {e}"
        return business

    if not match:
        business.setdefault("source_errors", {})["companies_house"] = "no confident match"
        return business

    company_number = match.get("company_number")
    if not company_number:
        return business

    try:
        profile = _http_get(CH_PROFILE_URL.format(number=company_number))
        officers = _http_get(CH_OFFICERS_URL.format(number=company_number))
    except Exception as e:
        business.setdefault("source_errors", {})["companies_house"] = f"profile fetch failed: {e}"
        # Still attach the search-level data
        business["companies_house_number"] = company_number
        return business

    business["companies_house_number"] = company_number
    business["companies_house_status"] = (profile or {}).get("company_status", "")
    business["companies_house_incorporated"] = (profile or {}).get("date_of_creation", "")

    sic = (profile or {}).get("sic_codes") or []
    if sic:
        business["companies_house_sic_code"] = sic[0]

    band = _revenue_band_from_accounts(profile or {})
    if band:
        business["companies_house_revenue_band"] = band

    # Officer count — only counts active officers (no resigned_on).
    if officers and isinstance(officers.get("items"), list):
        active = [o for o in officers["items"] if not o.get("resigned_on")]
        business["companies_house_officer_count"] = len(active)

    return business
