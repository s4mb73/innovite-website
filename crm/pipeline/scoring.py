"""Lead scoring — A/B/C/D/F grade + overall_score + weakness_profile.

Rubric (weighted sum, max ~155):
  - Companies House viability  0-65 pts  (base 0-30 + up to +35 from pain
                                          signals: overdue, director change,
                                          new incorporation. Capped at 65.)
  - Google reputation          0-20 pts  (rating x review-count buckets)
  - Decision-maker present     0-30 pts  (Apollo lookup landed)
  - Website quality            0-20 pts  (has site, has SSL — upgrades when
                                          source 2 / PageSpeed lands)
  - Year-end timing            0-20 pts  (proximity to next filing year-end)

Grade thresholds (intentionally generous against the 155 ceiling so a
typical "good lead" with no pain signals still grades B+):
  A: 96+   |  B: 72-95  |  C: 48-71  |  D: 24-47  |  F: <24

The weakness_profile is not just for scoring — it is the **hook source**
for the Day-1 drafter. Each weakness becomes a candidate opener line.
The drafter picks ONE hook per email; priority order lives in
_pick_hook_type() below.
"""
from __future__ import annotations

from pipeline.sources import Business


# Items 2-4 add pain signals on top of the base CH score. Capping the total
# CH contribution prevents a single 'fully pained' company dominating the
# overall score and forcing every other component to be ignored.
CH_COMPONENT_CAP = 65  # base max 30 + new signals max 35 (overdue 25 + 10)


def _score_companies_house(b: Business) -> tuple[int, list[str]]:
    """0-65 pts (capped). Hard floor at 0 for shells / dormant / no-match.

    Layered:
      base  (0-30) — viability from filing band + status
      pain  (+58 max before cap) — accounts_overdue +25, confirmation +10,
                                    director_change +10, new_incorporation +15
                                    or early_stage +8 (mutually exclusive).
    """
    weaknesses: list[str] = []
    if not b.get("companies_house_number"):
        return 0, ["no_companies_house_match"]

    status = (b.get("companies_house_status") or "").lower()
    if status and status != "active":
        return 0, [f"ch_status_{status}"]

    # ── Base viability from filing band ──────────────────────────────
    band = b.get("companies_house_revenue_band") or ""
    if "dormant" in band:
        base, base_w = 5, ["ch_dormant_filing"]
    elif "micro" in band:
        base, base_w = 15, ["ch_micro_filing"]
    elif "small" in band or "abridged" in band:
        base, base_w = 25, []
    elif "medium" in band or "full" in band:
        base, base_w = 30, []
    else:
        # Has a CH match but no readable filing band yet — partial credit.
        base, base_w = 18, []
    weaknesses.extend(base_w)

    # ── Item 2: new incorporation (mutually exclusive with early_stage) ──
    bonus = 0
    age = b.get("companies_house_company_age_days")
    if isinstance(age, int):
        if age <= 180:
            bonus += 15
            weaknesses.append("new_incorporation")
            # Reset the band — micro/dormant is the wrong label for a brand-new co.
            if not band:
                b["companies_house_revenue_band"] = "<£632k (new)"
        elif age <= 365:
            bonus += 8
            weaknesses.append("early_stage")

    # ── Item 3: director change in last 90 days ──────────────────────
    if b.get("companies_house_recent_director_change"):
        bonus += 10
        weaknesses.append("director_change_recent")

    # ── Item 4: overdue filings (strongest pain signal) ──────────────
    if b.get("companies_house_accounts_overdue"):
        bonus += 25
        weaknesses.append("accounts_overdue")
    elif b.get("companies_house_confirmation_overdue"):
        # Only flag confirmation_overdue when accounts is clean — they're
        # not both worth mentioning in one email.
        bonus += 10
        weaknesses.append("confirmation_overdue")

    return min(CH_COMPONENT_CAP, base + bonus), weaknesses


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


def _score_year_end_timing(b: Business) -> tuple[int, list[str]]:
    """0-20 pts. Strongest when year-end is 2-3 months out — the window
    where accountants are actively being chosen for the upcoming filing."""
    m = b.get("companies_house_months_to_year_end")
    if not isinstance(m, int):
        return 0, []
    if 2 <= m <= 3:
        return 20, ["year_end_imminent"]
    if 4 <= m <= 5:
        return 12, ["year_end_soon"]
    return 0, []


def _grade_from_score(score: int) -> str:
    """Thresholds scaled for the ~155-pt ceiling. A is 96+ — a clean lead
    with no pain signals (CH 30 + Google 14 + DM 30 + Web 20 + YE 12 ≈ 106)
    still hits A. Pain-signal-heavy leads can exceed 120 comfortably."""
    if score >= 96:
        return "A"
    if score >= 72:
        return "B"
    if score >= 48:
        return "C"
    if score >= 24:
        return "D"
    return "F"


def _pick_hook_type(weaknesses: list[str]) -> str:
    """Choose the primary outreach hook from the strongest weakness signal.

    Priority is ordered by conversion lift, not severity of pain. Overdue
    filings is the strongest pain signal so it leads, but year-end timing
    edges out a historical director change because YE timing is a live
    decision window — a director change is a past event whose window may
    already be closing.
    """
    priority = [
        "accounts_overdue",          # Items 4 — direct pain, urgent
        "year_end_imminent",         # Item 1 — live decision window
        "director_change_recent",    # Item 3 — supplier-review trigger
        "new_incorporation",         # Item 2 — needs an accountant urgently
        "early_stage",               # Item 2 — first-year supplier choice
        "year_end_soon",             # Item 1 — softer timing prompt
        "confirmation_overdue",      # Item 4 — minor compliance nudge
        "low_rating_with_volume",
        "rating_under_4",
        "few_reviews",
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
    ye_pts, ye_w = _score_year_end_timing(business)

    total = ch_pts + rep_pts + dm_pts + web_pts + ye_pts
    weaknesses = ch_w + rep_w + dm_w + web_w + ye_w

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
                "year_end":        ye_pts,
            },
        },
        "hook_type":  _pick_hook_type(weaknesses),
    }
