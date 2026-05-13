"""Lead scoring — A/B/C/D/F grade + 0-100 overall_score + weakness_profile.

Rubric (weighted sum, 0-100):
  - Companies House viability  30 pts   (active + filed + non-micro band)
  - Google reputation          20 pts   (rating x review-count buckets)
  - Decision-maker present     30 pts   (Apollo lookup landed)
  - Website quality            20 pts   (has site, has SSL — upgrades when
                                          source 2 / PageSpeed lands)

Grade thresholds:
  A: 80+   |  B: 60-79  |  C: 40-59  |  D: 20-39  |  F: <20

The weakness_profile is not just for scoring — it is the **hook source**
for the Day-1 drafter. Each weakness becomes a candidate opener line.
"""
from __future__ import annotations

from pipeline.sources import Business


def _score_companies_house(b: Business) -> tuple[int, list[str]]:
    """0-30 pts. Hard floor at 0 for shells / dormant / no-match."""
    weaknesses: list[str] = []
    if not b.get("companies_house_number"):
        return 0, ["no_companies_house_match"]

    status = (b.get("companies_house_status") or "").lower()
    if status and status != "active":
        return 0, [f"ch_status_{status}"]

    band = b.get("companies_house_revenue_band") or ""
    if "dormant" in band:
        return 5, ["ch_dormant_filing"]
    if "micro" in band:
        return 15, ["ch_micro_filing"]
    if "small" in band or "abridged" in band:
        return 25, []
    if "medium" in band or "full" in band:
        return 30, []
    # Has a CH match but no readable filing band yet — partial credit.
    return 18, []


def _score_google_reputation(b: Business) -> tuple[int, list[str]]:
    """0-20 pts. Rewards established + decent rating; penalises low rating."""
    weaknesses: list[str] = []
    rating = b.get("google_rating") or 0
    reviews = b.get("google_review_count") or 0

    # Established signal: enough reviews to mean anything.
    if reviews < 5:
        weaknesses.append("few_reviews")
        established_pts = 4
    elif reviews < 20:
        established_pts = 8
    elif reviews < 100:
        established_pts = 12
    else:
        established_pts = 14

    # Reputation hook: low rating with many reviews is a strong outreach angle.
    if rating and rating < 3.5 and reviews >= 10:
        weaknesses.append("low_rating_with_volume")
        rating_pts = 2
    elif rating and rating < 4.0:
        weaknesses.append("rating_under_4")
        rating_pts = 4
    elif rating and rating >= 4.5:
        rating_pts = 6
    else:
        rating_pts = 5

    return min(20, established_pts + rating_pts), weaknesses


def _score_decision_maker(b: Business) -> tuple[int, list[str]]:
    """0-30 pts. The single biggest score lever once Apollo lands."""
    if b.get("decision_maker_email"):
        return 30, []
    if b.get("decision_maker_name"):
        return 15, ["dm_name_only_no_email"]
    return 0, ["no_decision_maker"]


def _score_website(b: Business) -> tuple[int, list[str]]:
    """0-20 pts. Upgrades when PageSpeed lands (source 2)."""
    weaknesses: list[str] = []
    if not b.get("website"):
        return 0, ["no_website"]

    pts = 10  # Has a site at all.
    if b.get("website", "").startswith("https://"):
        pts += 5
    else:
        weaknesses.append("no_ssl")

    # Future: PageSpeed score, CTA detection. For now, presence + SSL only.
    pts += 5  # Reserved slot for the missing source 2 signals.
    return min(20, pts), weaknesses


def _grade_from_score(score: int) -> str:
    if score >= 80:
        return "A"
    if score >= 60:
        return "B"
    if score >= 40:
        return "C"
    if score >= 20:
        return "D"
    return "F"


def _pick_hook_type(weaknesses: list[str]) -> str:
    """Choose the primary outreach hook from the strongest weakness signal.

    Priority is ordered by how compelling the resulting Day-1 line is —
    a specific review-driven hook beats a generic "we noticed you have
    no website" line.
    """
    priority = [
        "low_rating_with_volume",   # "noticed your last reviews mention..."
        "rating_under_4",
        "few_reviews",              # "businesses in your sector are running review campaigns..."
        "no_ssl",
        "no_website",
        "no_decision_maker",
        "dm_name_only_no_email",
    ]
    for w in priority:
        if w in weaknesses:
            return w
    return "general_growth"


def grade(business: Business) -> dict:
    """Return a dict of fields to merge onto the lead row.

    Returns keys: grade, overall_score, weakness_profile, hook_type.
    """
    ch_pts, ch_w = _score_companies_house(business)
    rep_pts, rep_w = _score_google_reputation(business)
    dm_pts, dm_w = _score_decision_maker(business)
    web_pts, web_w = _score_website(business)

    total = ch_pts + rep_pts + dm_pts + web_pts
    weaknesses = ch_w + rep_w + dm_w + web_w

    return {
        "grade":             _grade_from_score(total),
        "overall_score":     total,
        "weakness_profile":  {
            "weaknesses":  weaknesses,
            "components": {
                "companies_house": ch_pts,
                "google":          rep_pts,
                "decision_maker":  dm_pts,
                "website":         web_pts,
            },
        },
        "hook_type":  _pick_hook_type(weaknesses),
    }
