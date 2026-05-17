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
from datetime import date

from pipeline.sources import Business


def _parse_iso_date(s: str | None) -> date | None:
    """CH returns dates as 'YYYY-MM-DD'. Return None on parse failure."""
    if not s or not isinstance(s, str):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _months_to_month(target_month: int, *, today: date | None = None) -> int:
    """Calendar months from today's month to the next occurrence of target_month.
    Returns 0-11. If target_month == today's month, returns 0."""
    today = today or date.today()
    return (target_month - today.month) % 12


def _derive_year_end(profile: dict, business: Business) -> None:
    """Item 1 — write year_end_month + months_to_year_end onto business.

    Prefers accounts.next_made_up_to (the actual upcoming year-end date in
    the API response). Falls back to (accounts.next_due - 9 months) for
    profiles missing the direct field. Logs which method was used in
    source_errors so coverage can be audited later."""
    accounts = (profile or {}).get("accounts") or {}
    next_made_up = _parse_iso_date(accounts.get("next_made_up_to"))
    if next_made_up:
        ye_month = next_made_up.month
        method = "direct"
    else:
        next_due = _parse_iso_date(accounts.get("next_due"))
        if not next_due:
            return
        # accounts due ~9 months after year-end (private cos); back out the month.
        ye_month = ((next_due.month - 9 - 1) % 12) + 1
        method = "derived_from_next_due"

    business["companies_house_year_end_month"] = ye_month
    business["companies_house_months_to_year_end"] = _months_to_month(ye_month)
    business.setdefault("source_errors", {})["companies_house_year_end_source"] = method


def _derive_company_age(profile: dict, business: Business) -> None:
    """Item 2 — write company_age_days from date_of_creation."""
    creation = _parse_iso_date((profile or {}).get("date_of_creation"))
    if creation:
        business["companies_house_company_age_days"] = (date.today() - creation).days


def _derive_director_change(officers: dict, business: Business) -> None:
    """Item 3 — flag any active officer appointed in the last 90 days.

    Uses data already fetched — no extra API call. The minimum (most
    recent) appointment age across all qualifying officers is recorded
    so the drafter can mention 'a new director was appointed N days ago'.
    """
    if not officers or not isinstance(officers.get("items"), list):
        return
    today = date.today()
    recent_ages: list[int] = []
    for o in officers["items"]:
        if o.get("resigned_on"):
            continue
        appointed = _parse_iso_date(o.get("appointed_on"))
        if not appointed:
            continue
        days_ago = (today - appointed).days
        if 0 <= days_ago <= 90:
            recent_ages.append(days_ago)
    if recent_ages:
        business["companies_house_recent_director_change"] = True
        business["companies_house_director_appointed_days_ago"] = min(recent_ages)


def _derive_overdue(profile: dict, business: Business) -> None:
    """Item 4 — extract accounts.overdue + confirmation_statement.overdue."""
    accounts = (profile or {}).get("accounts") or {}
    cs = (profile or {}).get("confirmation_statement") or {}
    business["companies_house_accounts_overdue"]     = bool(accounts.get("overdue"))
    business["companies_house_confirmation_overdue"] = bool(cs.get("overdue"))

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
    """Attach CH fields to the business if a confident match exists.

    Match strategy (tiered):
      Tier 1 — Footer scrape: if the lead's website displays its CH
               number (Companies Act s.82 requires it), use that
               directly. 100% accurate, no fuzzy match needed.
      Tier 2 — Fuzzy name + postcode search via the CH API.
    """
    bname = business.get("business_name", "")
    website = (business.get("website") or "").strip()

    company_number: str | None = None
    match_source = "fuzzy"

    # Tier 1 — footer scrape. Cheap when the homepage's already in cache;
    # ~2-4 proxy hits worst case (homepage + 1-3 legal pages until match).
    if website:
        try:
            # Local import avoids a circular dep at module load — the
            # scraper package imports from pipeline.sources for typing
            # in some adapters; safer to defer.
            from scraper import ch_footer as ch_footer_mod
            found = ch_footer_mod.find_for_website(website)
            if found:
                company_number = found
                match_source = "footer"
        except Exception as e:
            # Footer extraction is a nice-to-have; fall through to fuzzy.
            logger.exception("ch_footer extract failed for %s", website[:80])
            business.setdefault("source_errors", {})["ch_footer"] = f"unexpected: {str(e)[:120]}"

    # Tier 2 — fuzzy name+postcode search (existing path).
    if not company_number:
        pc = business.get("postcode", "")
        if not bname or not pc:
            business.setdefault("source_errors", {})["companies_house"] = "missing name or postcode (and no footer match)"
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

    business["companies_house_match_source"] = match_source

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
    # Also stash the active officer list for the decision_maker
    # source to pick from without a second CH round-trip.
    if officers and isinstance(officers.get("items"), list):
        active = [o for o in officers["items"] if not o.get("resigned_on")]
        business["companies_house_officer_count"] = len(active)
        business["companies_house_officers"] = [
            {
                "name":          (o.get("name") or "").strip(),
                "role":          (o.get("officer_role") or "").strip(),
                "appointed_on":  o.get("appointed_on") or "",
            }
            for o in active
            if (o.get("name") or "").strip()
        ]

    # Items 1-4: derive timing + pain signals from data already in memory.
    # Each is best-effort and never raises — partial enrichment is fine.
    try:
        _derive_year_end(profile or {}, business)
    except Exception as e:
        business.setdefault("source_errors", {})["companies_house_year_end"] = str(e)[:120]
    try:
        _derive_company_age(profile or {}, business)
    except Exception as e:
        business.setdefault("source_errors", {})["companies_house_company_age"] = str(e)[:120]
    try:
        _derive_director_change(officers or {}, business)
    except Exception as e:
        business.setdefault("source_errors", {})["companies_house_director_change"] = str(e)[:120]
    try:
        _derive_overdue(profile or {}, business)
    except Exception as e:
        business.setdefault("source_errors", {})["companies_house_overdue"] = str(e)[:120]

    return business
