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
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import date

logger = logging.getLogger("crm.pipeline.sources.companies_house")

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


def _name_variants(business_name: str) -> list[str]:
    """Generate query variants in fall-back order. The CH search API
    matches better on cleaned names — corporate suffixes and trailing
    location qualifiers often miss otherwise-good candidates.

    Returns in priority order: raw → suffix-stripped → location-stripped
    → first-2-distinctive-tokens. Deduped, original first."""
    out: list[str] = []
    seen: set[str] = set()

    def _add(s: str) -> None:
        s = s.strip()
        if s and s.lower() not in seen and len(s) >= 3:
            seen.add(s.lower())
            out.append(s)

    _add(business_name)

    # Strip parenthetical location qualifiers: "Charlton Baker (Bath) Ltd"
    cleaned = re.sub(r"\s*\([^)]+\)\s*", " ", business_name).strip()
    _add(cleaned)

    # Strip common suffixes + corporate forms.
    NOISE = (
        "& co", "and co", "and company", "group", "holdings",
        "uk", "the", "international", "limited", "ltd", "plc", "llp",
    )
    tokens = cleaned.lower().split()
    kept = [t for t in tokens if t not in NOISE and not t.startswith("(")]
    if kept and len(" ".join(kept)) >= 3:
        _add(" ".join(kept))

    # First 2-3 distinctive tokens only (e.g. 'Charlton Baker' from
    # 'Charlton Baker Bath Accountants Ltd').
    if len(kept) >= 2:
        _add(" ".join(kept[:2]))

    return out


def _city_in_address(city: str, address: dict | None) -> bool:
    """True when the CH candidate's address contains the lead's city."""
    if not city or not address or not isinstance(address, dict):
        return False
    city_l = city.lower().strip()
    for field in ("locality", "region", "address_line_1", "address_line_2", "premises"):
        v = (address.get(field) or "")
        if isinstance(v, str) and city_l in v.lower():
            return True
    return False


def _score_candidate(item: dict, business_name: str, postcode: str, city: str) -> int:
    """Multi-signal confidence score 0-100 for a single CH search hit.

    Components:
      Name overlap (Jaccard, 0-1)     ×40    weight 40
      Postcode exact match              +30
      Postcode area match (not exact)   +15
      City present in address           +10
      Status = 'active'                  +5
      Has SIC codes (real trading co)    +5
    """
    cand_name = item.get("title", "")
    name_overlap = _name_overlap(business_name, cand_name)
    score = int(name_overlap * 40)

    cand_pc = (item.get("address", {}) or {}).get("postal_code", "").strip().upper()
    if postcode and cand_pc:
        target_pc = postcode.strip().upper()
        # Normalise the gap so 'CB4 3BW' == 'CB43BW'.
        if cand_pc.replace(" ", "") == target_pc.replace(" ", ""):
            score += 30
        elif _postcode_area(cand_pc) == _postcode_area(target_pc):
            score += 15

    if _city_in_address(city, item.get("address")):
        score += 10
    if item.get("company_status") == "active":
        score += 5
    if item.get("description_identifier"):
        # 'description_identifier' is e.g. 'incorporated-on' — most
        # active trading companies have one. Cheap signal.
        score += 5

    return score


def _search_match(business_name: str, postcode: str,
                  city: str | None = None) -> dict | None:
    """Best CH search hit for (name, postcode, optional city) above
    a multi-signal confidence threshold.

    Tier 2 strategy:
      - Try multiple name variants (raw → suffix-stripped → cleaned)
        and pool candidates across them.
      - Score each candidate on name + postcode + city + status.
      - Accept best if score >= 50, OR name overlap >= 0.85 (very
        confident name match overrides geo requirement — useful for
        leads with bad postcode data).
    """
    if not business_name:
        return None
    variants = _name_variants(business_name)
    if not variants:
        return None

    seen_company_numbers: set[str] = set()
    candidates: list[dict] = []
    for query in variants[:3]:  # cap at 3 variants per lead to bound API spend
        time.sleep(PER_CALL_SLEEP_S)
        qs = urllib.parse.urlencode({"q": query, "items_per_page": 10})
        data = _http_get(f"{CH_SEARCH_URL}?{qs}") or {}
        for item in data.get("items", []):
            if item.get("kind") != "searchresults#company":
                continue
            cn = item.get("company_number")
            if not cn or cn in seen_company_numbers:
                continue
            seen_company_numbers.add(cn)
            candidates.append(item)
        if len(candidates) >= 30:
            break  # plenty to score

    if not candidates:
        return None

    # Name-overlap floor — geo + status signals alone aren't enough to
    # claim a CH match. Without this, a generic name like 'JJS
    # Accountants' searched with postcode BA1 1HE would match any
    # 'XYZ Accountants Limited' in Bath via postcode + city + active
    # signals scoring 50+ despite the wrong name.
    #
    # Two acceptance paths from here:
    #   - Combined score ≥ 60 AND name overlap ≥ 0.40
    #   - Very strong name match (≥ 0.85) on its own → no geo required
    best_score = 0
    best_item: dict | None = None
    for item in candidates:
        name_overlap = _name_overlap(business_name, item.get("title", ""))
        s = _score_candidate(item, business_name, postcode or "", city or "")

        accept = False
        if name_overlap >= 0.85:
            accept = True
        elif name_overlap >= 0.40 and s >= 60:
            accept = True

        if accept and s > best_score:
            best_score = s
            best_item = item

    return best_item


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

    # Tier 2 — fuzzy name+postcode+city search (multi-signal scoring,
    # query variations). City is a tiebreaker, not required.
    if not company_number:
        pc = business.get("postcode", "")
        city = business.get("city", "")
        if not bname:
            business.setdefault("source_errors", {})["companies_house"] = "missing business name (and no footer match)"
            return business

        try:
            match = _search_match(bname, pc, city)
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
