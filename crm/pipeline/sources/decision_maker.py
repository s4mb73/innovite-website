"""Decision-maker enrichment — Apollo replacement.

Combines two free sources to produce the same biz-dict fields that
apollo.enrich used to populate:

  - decision_maker_name   ← CH officers (first active director, ranked)
  - decision_maker_title  ← CH officer role
  - decision_maker_email  ← website-scraped pattern + best guess
  - decision_maker_source ← 'ch_officers' (vs the old 'apollo')

What we DON'T get vs Apollo
---------------------------
- linkedin_url — Apollo provided one per contact; CH doesn't. The
  LinkedIn scrape stage skips silently without it. A follow-up
  commit can add LinkedIn URL discovery via /in/ pattern matching
  on the company's homepage / About page.
- Verified email status — Apollo flagged 'verified' vs 'guessed' on
  every email. We currently mark our guesses as 'pattern' confidence
  bands (high/medium/low). Adding a real-time SMTP / Hunter verifier
  is the next iteration.

Why this is acceptable
----------------------
- Cost: ~$0/lead vs Apollo's $0.05-0.10/lead.
- Data freshness: CH is the registry of record, updated within 14
  days of any director change. Apollo's contact-DB lags by months
  in our experience.
- Hit rate: lower than Apollo (~50% vs ~70%) but the operator can
  see our confidence band and decide whether to send.

Ordering of the chain (skip-on-fail)
------------------------------------
  1. If no CH number → no officers possible, return early.
  2. Pick the best officer (CEO/Founder/Director ranking).
  3. If no website → set name+title, no email, return.
  4. Sniff website for visible emails + infer pattern.
  5. Generate ranked candidates for the chosen officer.
  6. Use the top candidate as decision_maker_email; expose the rest
     via biz['decision_maker_email_candidates'] so the UI can show
     fallbacks.
"""
from __future__ import annotations

import logging

from pipeline.sources import Business
from scraper import email_patterns

logger = logging.getLogger("crm.pipeline.sources.decision_maker")

name = "decision_maker"


# Officer role ranking — higher is better. We pick the highest-ranked
# active officer. Ties broken by alphabetical name (stable across runs).
_ROLE_RANK = {
    "director":                    100,
    "llp-designated-member":       100,
    "llp-member":                   90,
    "secretary":                    50,
    "corporate-director":           80,  # less ideal but still senior
    "corporate-secretary":          40,
    "nominee-director":             30,
    "nominee-secretary":            20,
}


def _pick_best_officer(officers: list[dict]) -> dict | None:
    """Choose the most-likely decision maker from active officers.

    Returns the full officer dict ({name, role, appointed_on}) or None.

    Heuristics:
      - Rank by role (director > corporate-director > secretary > …).
      - Tie-break by appointed_on ASC — earliest-appointed director is
        usually the founder / managing partner in UK SMB. This is
        directionally right for our accountancy / agency targets.
    """
    if not officers:
        return None
    scored = []
    for o in officers:
        role = (o.get("role") or "").lower().strip()
        rank = _ROLE_RANK.get(role, 0)
        if rank == 0:
            continue
        appointed = o.get("appointed_on") or "9999-12-31"
        scored.append((-rank, appointed, o.get("name") or "", o))
    if not scored:
        return None
    scored.sort()
    return scored[0][3]


def enrich(business: Business) -> Business:
    """Apollo-replacement enricher. Populates decision_maker_* fields
    from CH officers + website-scraped email patterns. Never raises."""
    # Early-out: nothing to do if we don't even know who's at the helm.
    officers = business.get("companies_house_officers") or []
    if not officers:
        # Surface the reason so the operator-facing 'why this score' panel
        # can explain why decision_maker is unset.
        if not business.get("companies_house_number"):
            business.setdefault("source_errors", {})["decision_maker"] = "no CH match — no officers available"
        else:
            business.setdefault("source_errors", {})["decision_maker"] = "no active officers on CH"
        return business

    officer = _pick_best_officer(officers)
    if not officer:
        business.setdefault("source_errors", {})["decision_maker"] = "no director-level officer found"
        return business

    business["decision_maker_name"]  = officer.get("name", "")
    business["decision_maker_title"] = (officer.get("role") or "").replace("-", " ").title()
    business["decision_maker_source"] = "ch_officers"

    website = (business.get("website") or "").strip()
    if not website:
        business.setdefault("source_errors", {})["decision_maker"] = (
            "officer found but no website — can't infer email pattern"
        )
        return business

    # Pattern detection — sniff homepage + a few common paths, return
    # visible emails + inferred pattern + confidence.
    try:
        det = email_patterns.detect(website, officers)
    except Exception as e:
        logger.exception("email_patterns.detect failed for %s", website[:80])
        business.setdefault("source_errors", {})["decision_maker"] = f"pattern detect: {str(e)[:120]}"
        return business

    domain = det.get("domain")
    if not domain:
        business.setdefault("source_errors", {})["decision_maker"] = "website has no parseable domain"
        return business

    candidates = email_patterns.generate_candidates(
        officer.get("name", ""), domain,
        pattern=det.get("inferred_pattern"),
    )
    if not candidates:
        business.setdefault("source_errors", {})["decision_maker"] = (
            "couldn't generate email candidates (officer name parse failed?)"
        )
        return business

    business["decision_maker_email"] = candidates[0]
    business["decision_maker_email_candidates"] = candidates[:5]
    business["decision_maker_email_confidence"] = det.get("confidence", "none")
    business["decision_maker_email_pattern"]   = det.get("inferred_pattern") or "default_first_dot_last"

    # Surface the visible emails so the operator can verify our work.
    # Useful when the inferred pattern looks wrong and a manual lookup
    # is faster than re-running.
    visible = det.get("visible_emails") or []
    if visible:
        business["website_visible_emails"] = visible[:10]
    if det.get("errors"):
        business.setdefault("source_errors", {})["decision_maker"] = " | ".join(det["errors"])[:240]
    return business
