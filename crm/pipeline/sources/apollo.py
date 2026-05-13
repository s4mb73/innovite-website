"""Apollo.io decision-maker enrichment.

For each business with a website, pull the highest-confidence director
contact from Apollo and attach name/title/email/LinkedIn URL.

Auth: APOLLO_API_KEY env var. Apollo restricts by domain at signup, not
by IP, so the key itself is the boundary — rotate if leaked.

Endpoint: POST /v1/mixed_people/search
  Filters:
    q_organization_domains  — derived from the business website
    person_titles[]         — Director / Founder / MD / Owner / CEO / Partner
    page                    — 1
    per_page                — 5 (we only need the top hit, the small page
                                buys a fallback if the top hit is unusable)
  Fields used from each result:
    name, title, linkedin_url, email, email_status, organization.name

Email confidence
----------------
Apollo flags emails as 'verified' / 'guessed' / 'unavailable'.
  - verified   → confidence 0.9   (use)
  - guessed    → confidence 0.5   (do not use — better handled by the
                                    deferred custom-email path which adds
                                    a real verifier)
  - unavailable → confidence 0.0  (skip)

The pipeline's drafter / outreach engine only ships to confidence >= 0.85,
so guessed emails will surface in the CRM (so the operator can decide)
but will not be auto-cadence'd.

Rate limit / cost
-----------------
Apollo Basic = 600 req/min, included in £40/mo flat. We sleep 100ms
between calls — well clear of the limit on a 200-lead run.

Errors
------
Never raises. On any failure (no key, no domain, HTTP error, no result)
returns the business unchanged with a source_errors entry. The runner
downgrades the lead's score for missing director_email but continues.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from pipeline.sources import Business

APOLLO_SEARCH_URL = "https://api.apollo.io/v1/mixed_people/search"
PER_CALL_SLEEP_S = 0.1

DECISION_MAKER_TITLES = [
    "Founder",
    "Co-Founder",
    "Managing Director",
    "Director",
    "Owner",
    "CEO",
    "Chief Executive Officer",
    "Partner",
    "Principal",
    "President",
]

name = "apollo"


def _api_key() -> str | None:
    return os.environ.get("APOLLO_API_KEY")


def _extract_domain(website: str) -> str | None:
    """Pull the bare domain out of a website URL.

    Apollo's organization-domain filter wants the apex / etld+1 without
    scheme or www prefix. We strip both. Subdomains other than www are
    preserved because they sometimes carry the company identity (rare
    but seen on UK SMBs hosted under a hosting-provider subdomain).
    """
    if not website:
        return None
    site = website.strip().lower()
    if site.startswith("http://"):
        site = site[7:]
    elif site.startswith("https://"):
        site = site[8:]
    site = site.split("/")[0]  # strip path
    if site.startswith("www."):
        site = site[4:]
    return site or None


def _confidence_from_email_status(status: str | None) -> float:
    if status == "verified":
        return 0.9
    if status == "guessed":
        return 0.5
    return 0.0


def _post(url: str, payload: dict, timeout: int = 10) -> dict | None:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
            "X-Api-Key": _api_key() or "",
            "User-Agent": "InnoviteCRM/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Surface the body if available — Apollo error messages are useful.
        body_msg = ""
        try:
            body_msg = e.read().decode("utf-8")[:200]
        except Exception:
            pass
        raise RuntimeError(f"apollo {e.code}: {body_msg or e.reason}") from None


def _pick_best(results: list[dict]) -> dict | None:
    """Best decision-maker from the response 'people' list.

    Ranking:
      1. Has a verified email                (top priority)
      2. Title matches our preferred order   (Founder > MD > Director > ...)
      3. Has a linkedin_url                  (tiebreaker)
    """
    if not results:
        return None

    def score(p: dict) -> tuple:
        # All Python tuples are compared lexicographically; we want a higher
        # tuple to win, so encode each criterion as (higher_is_better).
        email_pts = 2 if p.get("email_status") == "verified" else (1 if p.get("email_status") == "guessed" else 0)
        title = (p.get("title") or "").lower()
        # Find the highest-priority title hit; absent = 0.
        title_pts = 0
        for i, t in enumerate(DECISION_MAKER_TITLES):
            if t.lower() in title:
                title_pts = len(DECISION_MAKER_TITLES) - i
                break
        linkedin_pts = 1 if p.get("linkedin_url") else 0
        return (email_pts, title_pts, linkedin_pts)

    return max(results, key=score)


def enrich(business: Business) -> Business:
    """Attach decision-maker fields to the business via Apollo."""
    if not _api_key():
        business.setdefault("source_errors", {})["apollo"] = "APOLLO_API_KEY not set"
        return business

    domain = _extract_domain(business.get("website", ""))
    if not domain:
        business.setdefault("source_errors", {})["apollo"] = "no website / domain to search"
        return business

    time.sleep(PER_CALL_SLEEP_S)
    try:
        data = _post(APOLLO_SEARCH_URL, {
            "q_organization_domains": domain,
            "person_titles": DECISION_MAKER_TITLES,
            "page": 1,
            "per_page": 5,
        })
    except RuntimeError as e:
        business.setdefault("source_errors", {})["apollo"] = str(e)
        return business
    except Exception as e:
        business.setdefault("source_errors", {})["apollo"] = f"network error: {e}"
        return business

    people = (data or {}).get("people") or []
    best = _pick_best(people)
    if not best:
        business.setdefault("source_errors", {})["apollo"] = "no director-level match"
        return business

    business["decision_maker_name"] = best.get("name") or ""
    business["decision_maker_title"] = best.get("title") or ""
    business["linkedin_url"] = best.get("linkedin_url") or ""

    email = best.get("email") or ""
    email_status = best.get("email_status")
    confidence = _confidence_from_email_status(email_status)
    if email and confidence > 0:
        business["decision_maker_email"] = email
        business["decision_maker_email_confidence"] = confidence
    else:
        # Name + title found but no usable email — still useful to the
        # operator. Source_errors makes the gap visible.
        business.setdefault("source_errors", {})["apollo"] = f"email status={email_status or 'absent'}"

    return business
