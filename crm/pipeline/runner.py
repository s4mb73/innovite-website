"""Pipeline runner — turns a queued pipeline_runs row into rows in crm.leads.

Flow
----
1. Worker calls run(run_id).
2. Runner marks the run 'running', reads the client's targeting config.
3. For each (industry x location), Google Places discovers candidates.
4. Each candidate is deduped against crm.leads and the client's exclusion list.
5. Survivors are enriched (Companies House, Apollo) and scored.
6. Grade A/B/C leads get a Day-1 email draft queued to crm.emails.
   Grade D/F skip the draft step — no point burning Anthropic tokens on
   leads we are unlikely to ever contact.
7. Counts are written back to pipeline_runs. activity_log gets a
   'pipeline_run' entry tied to the client.

Error handling
--------------
- A failing source per lead is logged on the lead and the run continues.
- A failing source for an entire (industry, location) pair marks that
  pair as errored in progress JSONB; the run continues with other pairs.
- A run-killing exception (DB connection lost, etc.) marks the run
  'failed' with the error message and re-raises so the worker logs it.

Concurrency
-----------
The worker holds an advisory lock on the run_id while processing.
No two worker instances will pick up the same run because the claim
query uses SELECT ... FOR UPDATE SKIP LOCKED.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

# Make 'crm' importable when this is run as a worker from the systemd unit.
# The Procfile / systemd unit sets WORKDIR to /srv/innovite/innovite-website/crm/
# so 'pipeline' and 'db' resolve from sys.path directly.
import db
from pipeline import scoring, drafter
from pipeline.sources import google_places, companies_house, decision_maker
from scraper import dns_signals
from scraper import enricher as website_scraper
from scraper import gazette as gazette_scraper
from scraper import google_places_free
from scraper import jobs as jobs_scraper
from scraper import linkedin as linkedin_scraper
from scraper import website_discovery

logger = logging.getLogger("crm.pipeline.runner")


# ── Helpers ──────────────────────────────────────────────────────────
def _set_status(run_id: int, **fields) -> None:
    """Patch a pipeline_runs row. Pass kwargs matching column names."""
    if not fields:
        return
    set_clauses = []
    values: list = []
    for k, v in fields.items():
        if k == "progress":
            v = Jsonb(v)
        set_clauses.append(f"{k} = %s")
        values.append(v)
    values.append(run_id)
    sql = f"update crm.pipeline_runs set {', '.join(set_clauses)} where id = %s"
    db.execute(sql, tuple(values))


def _log_activity(client_id: int, action: str, detail: dict) -> None:
    db.execute(
        "insert into crm.activity_log (client_id, action, detail) values (%s, %s, %s)",
        (client_id, action, json.dumps(detail)),
    )


def _load_client(client_id: int) -> dict:
    row = db.fetch_one(
        "select id, name, target_industries, target_locations, targeting_filters "
        "from crm.clients where id = %s",
        (client_id,),
    )
    if not row:
        raise ValueError(f"client {client_id} not found")
    return row


def _existing_lead_keys(client_id: int) -> set[tuple[str, str]]:
    """The (name_lower, postcode_lower) keys that already exist for the client.

    Used to dedupe across runs. Postcode_lower may be empty for legacy rows;
    we still match on name only as a fallback so we never duplicate.
    """
    rows = db.fetch_all(
        "select business_name, address from crm.leads where client_id = %s",
        (client_id,),
    )
    keys: set[tuple[str, str]] = set()
    for r in rows:
        name = (r.get("business_name") or "").strip().lower()
        # We do not store postcode separately on the lead row today — address
        # contains it. Use just name for the dedupe key for now; the runner's
        # next iteration adds a postcode column to crm.leads.
        if name:
            keys.add((name, ""))
    return keys


def _client_exclusion_set(client: dict) -> set[str]:
    """Lower-cased set of domain + company-name strings to exclude.

    Per US-017 the exclusion list lives in targeting_filters JSONB.
    """
    filters = client.get("targeting_filters") or {}
    raw = filters.get("exclusion_list") if isinstance(filters, dict) else None
    if not raw:
        return set()
    return {str(x).strip().lower() for x in raw if x}


def _is_excluded(business: dict, exclusions: set[str]) -> bool:
    if not exclusions:
        return False
    name = (business.get("business_name") or "").strip().lower()
    site = (business.get("website") or "").strip().lower()
    if name in exclusions:
        return True
    for excl in exclusions:
        if excl and (excl in name or (site and excl in site)):
            return True
    return False


def _stage1_filter_reason(business: dict) -> str | None:
    """Apply the Stage-1 (Companies House) qualification filter.
    Returns a short reason if the lead should be dropped before any
    further enrichment runs, or None to continue through Stages 2-6.

    Conservative for now — only drops definitively-dead companies.
    Status values that warrant staying in the pipeline:
      - 'active'                — normal
      - 'liquidation' / 'administration' / 'receivership'
        — distressed but potentially gold for turnaround / insolvency
          specialist clients; Gazette stage will flag them louder.
      - None (no CH match)
        — could be a sole trader; existing PECR logic skips drafting
          but keeps the lead.

    Only an explicit 'dissolved' status means there's no business to
    sell to and no decision-maker to email. Drop those before
    spending Apollo / LinkedIn / Reed / Gazette quota on them."""
    status = (business.get("companies_house_status") or "").strip().lower()
    if status == "dissolved":
        return "dissolved at Companies House"
    return None


# ── Enrichment chain (shared by discovery + assignment paths) ──────
# Extracted from the run() inner loop so a single lead can be
# (re-)enriched without needing a full discovery pass. Each enricher
# is independently try/except'd — a failure on one source attaches
# to biz['source_errors'] and the chain continues.

def _enrich_chain(biz: dict) -> dict:
    """Run every enrichment source in the documented Stage 1-6 order.
    Returns the same biz dict mutated in-place (also returned for
    chaining ergonomics). Never raises — per-source failures are
    captured in biz['source_errors'].
    """
    # Stage 1 — Companies House (qualification + identity)
    try:
        biz = companies_house.enrich(biz)
    except Exception as e:
        logger.exception("CH enrich failed")
        biz.setdefault("source_errors", {})["companies_house"] = str(e)

    # Stage 2a — Website discovery from the CH name (no-op if website set)
    try:
        biz = website_discovery.enrich(biz)
    except Exception as e:
        logger.exception("Website discovery failed")
        biz.setdefault("source_errors", {})["website_discovery"] = str(e)

    # Stage 2b — DNS signals (email provider, hosting, SPF/DMARC)
    try:
        biz = dns_signals.enrich(biz)
    except Exception as e:
        logger.exception("DNS signals enrich failed")
        biz.setdefault("source_errors", {})["dns_signals"] = str(e)

    # Stage 3a — Free Google Maps rating backfill (no-op if rating set)
    try:
        biz = google_places_free.enrich(biz)
    except Exception as e:
        logger.exception("Free Places enrich failed")
        biz.setdefault("source_errors", {})["google_places_free"] = str(e)

    # Stage 3b — Decision-maker: CH officers + email pattern detection.
    # Replaces the old Apollo call. Free, no API key, runs against the
    # officer list CH already attached to biz earlier in the chain.
    try:
        biz = decision_maker.enrich(biz)
    except Exception as e:
        logger.exception("decision_maker enrich failed")
        biz.setdefault("source_errors", {})["decision_maker"] = str(e)

    # Stage 4 — LinkedIn profile parse (skipped silently without linkedin_url)
    try:
        biz = linkedin_scraper.enrich(biz)
    except Exception as e:
        logger.exception("LinkedIn enrich failed")
        biz.setdefault("source_errors", {})["linkedin"] = str(e)

    # Stage 4b — Website scrape + Haiku extraction
    try:
        biz = website_scraper.enrich(biz)
    except Exception as e:
        logger.exception("Website scrape enrich failed")
        biz.setdefault("source_errors", {})["website_scraper"] = str(e)

    # Stage 5 — Reed open-jobs signal
    try:
        biz = jobs_scraper.enrich(biz)
    except Exception as e:
        logger.exception("Jobs enrich failed")
        biz.setdefault("source_errors", {})["jobs"] = str(e)

    # Stage 6 — Gazette distress flag
    try:
        biz = gazette_scraper.enrich(biz)
    except Exception as e:
        logger.exception("Gazette enrich failed")
        biz.setdefault("source_errors", {})["gazette"] = str(e)

    return biz


def _score_with_errors(biz: dict) -> dict:
    """Grade + tack source_errors onto the weakness_profile for the
    operator-facing 'why this score' panel."""
    score = scoring.grade(biz)
    src_errors = biz.get("source_errors")
    if src_errors:
        score["weakness_profile"]["source_errors"] = dict(src_errors)
    return score


import re as _re
_UK_POSTCODE_RE = _re.compile(
    r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b",
    _re.IGNORECASE,
)


def _lead_row_to_biz(row: dict) -> dict:
    """Convert a DB lead row into the biz-dict shape the enrichers
    expect. The lead row uses the same column names as the biz dict
    for most fields, so this is mostly a copy with a couple of
    aliases (decision_maker_email is stored as 'email' in some places).

    Postcode is not its own column on crm.leads — we regex it out of
    the formatted address string so CH fuzzy matching has the
    disambiguator it needs.
    """
    biz: dict = {}
    for k in (
        "business_name", "address", "city", "phone", "website",
        "google_rating", "google_review_count", "google_maps_url",
        "companies_house_number", "companies_house_sic_code",
        "companies_house_revenue_band", "companies_house_year_end_month",
        "companies_house_months_to_year_end", "companies_house_company_age_days",
        "companies_house_recent_director_change",
        "companies_house_director_appointed_days_ago",
        "companies_house_accounts_overdue", "companies_house_confirmation_overdue",
        "decision_maker_name", "decision_maker_title",
        "linkedin_url",
    ):
        v = row.get(k)
        if v is not None:
            biz[k] = v
    if row.get("email"):
        biz["decision_maker_email"] = row["email"]

    # Postcode extraction from the formatted address. Without it, the
    # CH fuzzy match short-circuits with "missing name or postcode"
    # and the whole enrichment chain downstream stops.
    addr = (row.get("address") or "").strip()
    if addr:
        m = _UK_POSTCODE_RE.search(addr)
        if m:
            biz["postcode"] = m.group(1).upper().strip()
    return biz


def _calculate_pain_score(business: dict, score: dict) -> int:
    """Derived 0-100 pain rollup so the Leads page can sort without parsing
    JSONB. Capped at 100. Booleans wrapped in int() so the arithmetic is
    explicit rather than relying on Python's bool-is-int trick.

    Formula:
      accounts_overdue        * 40
      confirmation_overdue    * 15
      director_change_recent  * 20
      year_end_imminent       * 15
      (overall_score < 40)    * 10
    """
    overall = int(score.get("overall_score") or 0)
    weaknesses = (score.get("weakness_profile") or {}).get("weaknesses") or []
    total = (
        int(bool(business.get("companies_house_accounts_overdue")))     * 40
        + int(bool(business.get("companies_house_confirmation_overdue")))* 15
        + int(bool(business.get("companies_house_recent_director_change")))* 20
        + int("year_end_imminent" in weaknesses)                         * 15
        + int(overall < 40)                                              * 10
    )
    return min(100, total)


def _insert_lead(client_id: int, business: dict, score: dict) -> int | None:
    """Insert into crm.leads. Returns the new lead id, or None on conflict."""
    sql = """
        insert into crm.leads (
            client_id, business_name, address, city, phone, website,
            google_rating, google_review_count, google_maps_url,
            companies_house_number, companies_house_sic_code,
            companies_house_incorporated, companies_house_revenue_band,
            companies_house_year_end_month, companies_house_months_to_year_end,
            companies_house_company_age_days,
            companies_house_recent_director_change,
            companies_house_director_appointed_days_ago,
            companies_house_accounts_overdue, companies_house_confirmation_overdue,
            decision_maker_name, decision_maker_title, email, linkedin_url,
            grade, overall_score, pain_score, hook_type, weakness_profile,
            website_signals, website_scraped_at, website_scrape_status,
            gazette_status, gazette_notice_count,
            gazette_last_notice_date, gazette_last_notice_url,
            jobs_signal, jobs_open_count,
            jobs_last_checked_at, jobs_source_url,
            linkedin_status, linkedin_current_title,
            linkedin_headline, linkedin_last_checked_at,
            linkedin_current_company, linkedin_location,
            linkedin_previous_companies, linkedin_follower_count,
            linkedin_profile_image_url,
            linkedin_recent_post_at, linkedin_recent_post_title,
            email_provider, website_host, dmarc_present, spf_present,
            status, source
        )
        values (%s, %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s,
                %s,
                %s,
                %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s,
                %s, %s,
                %s,
                %s, %s,
                %s, %s, %s, %s,
                'new', 'outbound')
        returning id
    """
    incorp = business.get("companies_house_incorporated") or None
    pain = _calculate_pain_score(business, score)
    web_signals = business.get("website_signals")
    row = db.fetch_one(sql, (
        client_id,
        business.get("business_name", "")[:512],
        business.get("address", "")[:1024] if business.get("address") else None,
        business.get("city") or None,
        business.get("phone") or None,
        business.get("website") or None,
        business.get("google_rating") or None,
        business.get("google_review_count") or None,
        business.get("google_maps_url") or None,
        business.get("companies_house_number") or None,
        business.get("companies_house_sic_code") or None,
        incorp if incorp else None,
        business.get("companies_house_revenue_band") or None,
        business.get("companies_house_year_end_month") or None,
        business.get("companies_house_months_to_year_end") if business.get("companies_house_months_to_year_end") is not None else None,
        business.get("companies_house_company_age_days") or None,
        business.get("companies_house_recent_director_change") or None,
        business.get("companies_house_director_appointed_days_ago") or None,
        business.get("companies_house_accounts_overdue") or None,
        business.get("companies_house_confirmation_overdue") or None,
        business.get("decision_maker_name") or None,
        business.get("decision_maker_title") or None,
        business.get("decision_maker_email") or None,
        business.get("linkedin_url") or None,
        score["grade"],
        score["overall_score"],
        pain,
        score["hook_type"],
        Jsonb(score["weakness_profile"]),
        Jsonb(web_signals) if web_signals else None,
        business.get("website_scraped_at") or None,
        business.get("website_scrape_status") or None,
        business.get("gazette_status") or None,
        business.get("gazette_notice_count") if business.get("gazette_notice_count") is not None else None,
        business.get("gazette_last_notice_date") or None,
        business.get("gazette_last_notice_url") or None,
        business.get("jobs_signal") or None,
        business.get("jobs_open_count") if business.get("jobs_open_count") is not None else None,
        business.get("jobs_last_checked_at") or None,
        business.get("jobs_source_url") or None,
        business.get("linkedin_status") or None,
        business.get("linkedin_current_title") or None,
        business.get("linkedin_headline") or None,
        business.get("linkedin_last_checked_at") or None,
        business.get("linkedin_current_company") or None,
        business.get("linkedin_location") or None,
        business.get("linkedin_previous_companies") or None,
        business.get("linkedin_follower_count") if business.get("linkedin_follower_count") is not None else None,
        business.get("linkedin_profile_image_url") or None,
        business.get("linkedin_recent_post_at") or None,
        business.get("linkedin_recent_post_title") or None,
        business.get("email_provider") or None,
        business.get("website_host") or None,
        business.get("dmarc_present") if business.get("dmarc_present") is not None else None,
        business.get("spf_present") if business.get("spf_present") is not None else None,
    ))
    return row["id"] if row else None


def _update_lead(lead_id: int, business: dict, score: dict) -> None:
    """Write enrichment + scoring fields back to an existing lead row.
    Mirror of _insert_lead's column list but as UPDATE. Only used by
    the enrich_existing_lead path — discovery still uses _insert_lead.
    """
    sql = """
        update crm.leads set
            address                                    = coalesce(%s, address),
            city                                       = coalesce(%s, city),
            phone                                      = coalesce(%s, phone),
            website                                    = coalesce(%s, website),
            google_rating                              = coalesce(%s, google_rating),
            google_review_count                        = coalesce(%s, google_review_count),
            google_maps_url                            = coalesce(%s, google_maps_url),
            companies_house_number                     = coalesce(%s, companies_house_number),
            companies_house_sic_code                   = coalesce(%s, companies_house_sic_code),
            companies_house_incorporated               = coalesce(%s, companies_house_incorporated),
            companies_house_revenue_band               = coalesce(%s, companies_house_revenue_band),
            companies_house_year_end_month             = coalesce(%s, companies_house_year_end_month),
            companies_house_months_to_year_end         = coalesce(%s, companies_house_months_to_year_end),
            companies_house_company_age_days           = coalesce(%s, companies_house_company_age_days),
            companies_house_recent_director_change     = coalesce(%s, companies_house_recent_director_change),
            companies_house_director_appointed_days_ago = coalesce(%s, companies_house_director_appointed_days_ago),
            companies_house_accounts_overdue           = coalesce(%s, companies_house_accounts_overdue),
            companies_house_confirmation_overdue       = coalesce(%s, companies_house_confirmation_overdue),
            decision_maker_name                        = coalesce(%s, decision_maker_name),
            decision_maker_title                       = coalesce(%s, decision_maker_title),
            email                                      = coalesce(%s, email),
            linkedin_url                               = coalesce(%s, linkedin_url),
            grade                                      = %s,
            overall_score                              = %s,
            pain_score                                 = %s,
            hook_type                                  = %s,
            weakness_profile                           = %s,
            website_signals                            = coalesce(%s, website_signals),
            website_scraped_at                         = coalesce(%s, website_scraped_at),
            website_scrape_status                      = coalesce(%s, website_scrape_status),
            gazette_status                             = coalesce(%s, gazette_status),
            gazette_notice_count                       = coalesce(%s, gazette_notice_count),
            gazette_last_notice_date                   = coalesce(%s, gazette_last_notice_date),
            gazette_last_notice_url                    = coalesce(%s, gazette_last_notice_url),
            jobs_signal                                = coalesce(%s, jobs_signal),
            jobs_open_count                            = coalesce(%s, jobs_open_count),
            jobs_last_checked_at                       = coalesce(%s, jobs_last_checked_at),
            jobs_source_url                            = coalesce(%s, jobs_source_url),
            linkedin_status                            = coalesce(%s, linkedin_status),
            linkedin_current_title                     = coalesce(%s, linkedin_current_title),
            linkedin_headline                          = coalesce(%s, linkedin_headline),
            linkedin_last_checked_at                   = coalesce(%s, linkedin_last_checked_at),
            linkedin_current_company                   = coalesce(%s, linkedin_current_company),
            linkedin_location                          = coalesce(%s, linkedin_location),
            linkedin_previous_companies                = coalesce(%s, linkedin_previous_companies),
            linkedin_follower_count                    = coalesce(%s, linkedin_follower_count),
            linkedin_profile_image_url                 = coalesce(%s, linkedin_profile_image_url),
            linkedin_recent_post_at                    = coalesce(%s, linkedin_recent_post_at),
            linkedin_recent_post_title                 = coalesce(%s, linkedin_recent_post_title),
            email_provider                             = coalesce(%s, email_provider),
            website_host                               = coalesce(%s, website_host),
            dmarc_present                              = coalesce(%s, dmarc_present),
            spf_present                                = coalesce(%s, spf_present)
        where id = %s
    """
    incorp = business.get("companies_house_incorporated") or None
    pain   = _calculate_pain_score(business, score)
    web_signals = business.get("website_signals")
    db.execute(sql, (
        business.get("address", "")[:1024] if business.get("address") else None,
        business.get("city") or None,
        business.get("phone") or None,
        business.get("website") or None,
        business.get("google_rating") or None,
        business.get("google_review_count") or None,
        business.get("google_maps_url") or None,
        business.get("companies_house_number") or None,
        business.get("companies_house_sic_code") or None,
        incorp if incorp else None,
        business.get("companies_house_revenue_band") or None,
        business.get("companies_house_year_end_month") or None,
        business.get("companies_house_months_to_year_end") if business.get("companies_house_months_to_year_end") is not None else None,
        business.get("companies_house_company_age_days") or None,
        business.get("companies_house_recent_director_change") or None,
        business.get("companies_house_director_appointed_days_ago") or None,
        business.get("companies_house_accounts_overdue") or None,
        business.get("companies_house_confirmation_overdue") or None,
        business.get("decision_maker_name") or None,
        business.get("decision_maker_title") or None,
        business.get("decision_maker_email") or None,
        business.get("linkedin_url") or None,
        score["grade"],
        score["overall_score"],
        pain,
        score["hook_type"],
        Jsonb(score["weakness_profile"]),
        Jsonb(web_signals) if web_signals else None,
        business.get("website_scraped_at") or None,
        business.get("website_scrape_status") or None,
        business.get("gazette_status") or None,
        business.get("gazette_notice_count") if business.get("gazette_notice_count") is not None else None,
        business.get("gazette_last_notice_date") or None,
        business.get("gazette_last_notice_url") or None,
        business.get("jobs_signal") or None,
        business.get("jobs_open_count") if business.get("jobs_open_count") is not None else None,
        business.get("jobs_last_checked_at") or None,
        business.get("jobs_source_url") or None,
        business.get("linkedin_status") or None,
        business.get("linkedin_current_title") or None,
        business.get("linkedin_headline") or None,
        business.get("linkedin_last_checked_at") or None,
        business.get("linkedin_current_company") or None,
        business.get("linkedin_location") or None,
        business.get("linkedin_previous_companies") or None,
        business.get("linkedin_follower_count") if business.get("linkedin_follower_count") is not None else None,
        business.get("linkedin_profile_image_url") or None,
        business.get("linkedin_recent_post_at") or None,
        business.get("linkedin_recent_post_title") or None,
        business.get("email_provider") or None,
        business.get("website_host") or None,
        business.get("dmarc_present") if business.get("dmarc_present") is not None else None,
        business.get("spf_present") if business.get("spf_present") is not None else None,
        lead_id,
    ))


def enrich_existing_lead(lead_id: int) -> dict:
    """Run the full enrichment chain against a lead already in crm.leads.

    Used by the Search → Assign flow: discovery inserts a lightweight
    row with only name + address + website + rating; this function
    backfills CH + DNS + Apollo + LinkedIn + website scrape + jobs +
    Gazette, scores it, and drafts a Day-1 email if the result grades
    A/B/C and the lead is corporate.

    Returns a small summary dict: {lead_id, grade, score, drafted}.
    """
    row = db.fetch_one(
        """select id, client_id, business_name, address, city, phone, website,
                  google_rating, google_review_count, google_maps_url,
                  companies_house_number, companies_house_sic_code,
                  companies_house_revenue_band, companies_house_year_end_month,
                  companies_house_months_to_year_end, companies_house_company_age_days,
                  companies_house_recent_director_change,
                  companies_house_director_appointed_days_ago,
                  companies_house_accounts_overdue, companies_house_confirmation_overdue,
                  decision_maker_name, decision_maker_title, email, linkedin_url
           from crm.leads where id = %s""",
        (lead_id,),
    )
    if not row:
        raise ValueError(f"lead {lead_id} not found")

    client_id = row["client_id"]
    biz = _lead_row_to_biz(row)

    biz = _enrich_chain(biz)
    score = _score_with_errors(biz)
    _update_lead(lead_id, biz, score)

    drafted = False
    is_corporate = bool(biz.get("companies_house_number"))
    if score["grade"] in ("A", "B", "C") and is_corporate:
        try:
            campaign_id = db.find_or_create_campaign(client_id, score["hook_type"])
            template_hint = db.best_template(campaign_id, step=1)
            draft = drafter.draft_day1(biz, score["hook_type"], template_hint=template_hint)
            _queue_day1_email(
                lead_id, client_id, biz, draft,
                campaign_id=campaign_id,
                template_id=(template_hint or {}).get("id"),
            )
            drafted = True
        except Exception:
            logger.exception("Draft/queue failed for lead %s", lead_id)

    return {
        "lead_id": lead_id,
        "grade":   score["grade"],
        "score":   score["overall_score"],
        "drafted": drafted,
    }


def _queue_day1_email(lead_id: int, client_id: int, business: dict, draft: dict,
                      *, campaign_id: int | None = None,
                      template_id: int | None = None) -> None:
    """Insert a scheduled Day-1 email and link it to its parent campaign.

    Cache the subject + body on the lead too, so /leads/<id> can render
    the draft preview without a join. If a template seeded this draft,
    bump the template's times_used counter for reply-rate analytics."""
    to_addr = business.get("decision_maker_email") or business.get("email") or ""
    if not to_addr:
        # No deliverable address — store the draft on the lead and skip
        # queueing. The drafter's output still lives on crm.leads.email_body_day1
        # so the operator can review it manually.
        db.execute(
            "update crm.leads set email_subject = %s, email_body_day1 = %s where id = %s",
            (draft["subject"], draft["body"], lead_id),
        )
        return

    db.execute(
        """insert into crm.emails
             (lead_id, client_id, campaign_id, email_number, subject, body,
              to_address, status, scheduled_at)
           values (%s, %s, %s, 1, %s, %s, %s, 'scheduled', null)""",
        (lead_id, client_id, campaign_id, draft["subject"], draft["body"], to_addr),
    )
    # Also cache on the lead so the lead detail page can render it.
    db.execute(
        "update crm.leads set email_subject = %s, email_body_day1 = %s where id = %s",
        (draft["subject"], draft["body"], lead_id),
    )
    # Bump the template's usage counter for per-template reply-rate tracking.
    if template_id:
        db.execute(
            "update crm.campaign_templates set times_used = times_used + 1 where id = %s",
            (template_id,),
        )


def _run_enrich_assigned(run_id: int, run_row: dict) -> dict:
    """Handle a pipeline_runs row with mode='enrich_assigned'. Walks
    progress.lead_ids, runs enrich_existing_lead per lead, updates
    per-lead progress so the UI can poll a live counter.
    """
    progress = run_row.get("progress") or {}
    if isinstance(progress, str):
        try:
            progress = json.loads(progress)
        except Exception:
            progress = {}
    lead_ids = list(progress.get("lead_ids") or [])
    total = len(lead_ids)

    counts = {"added": 0, "skipped": 0, "errored": 0}
    progress.update({
        "phase": "enriching",
        "total": total,
        "done": 0,
        "drafted": 0,
        "per_lead": [],
    })
    _set_status(run_id, progress=progress)

    for i, lid in enumerate(lead_ids, 1):
        try:
            result = enrich_existing_lead(int(lid))
            progress["done"] = i
            if result.get("drafted"):
                progress["drafted"] = progress.get("drafted", 0) + 1
            counts["added"] += 1
            progress["per_lead"].append({
                "lead_id": result["lead_id"],
                "grade":   result["grade"],
                "score":   result["score"],
                "drafted": result["drafted"],
            })
        except Exception as e:
            logger.exception("enrich lead %s failed", lid)
            counts["errored"] += 1
            progress.setdefault("errors", []).append(f"lead {lid}: {str(e)[:120]}")
        # Heartbeat after every lead — small batch, every step matters.
        _set_status(
            run_id,
            leads_added=counts["added"],
            leads_errored=counts["errored"],
            progress=progress,
        )

    finished = datetime.now(timezone.utc)
    final_status = "partial" if counts["errored"] else "succeeded"
    if counts["added"] == 0 and not counts["errored"]:
        final_status = "succeeded"  # empty list isn't an error
    _set_status(
        run_id,
        status=final_status,
        leads_added=counts["added"],
        leads_skipped=counts["skipped"],
        leads_errored=counts["errored"],
        progress=progress,
        finished_at=finished,
    )

    # Activity-log entry mirrors the discovery-path one so the
    # operator's run history shows enrichment runs alongside discoveries.
    client_id = run_row.get("client_id")
    if client_id:
        try:
            _log_activity(client_id, "pipeline_run", {
                "run_id": run_id,
                "status": final_status,
                "mode":   "enrich_assigned",
                "leads_added": counts["added"],
                "leads_errored": counts["errored"],
                "drafted": progress.get("drafted", 0),
            })
        except Exception:
            logger.exception("activity_log write failed for run %s", run_id)

    return {
        "leads_added":   counts["added"],
        "leads_skipped": counts["skipped"],
        "leads_errored": counts["errored"],
    }


# ── Main entry point ─────────────────────────────────────────────────
def run(run_id: int) -> dict:
    """Process one pipeline_runs row. Returns the final counts dict.

    Raises on unrecoverable DB errors. Source-level failures are logged
    onto the lead and the run continues.
    """
    started = datetime.now(timezone.utc)

    # Read mode + progress BEFORE any _set_status call — otherwise the
    # default "loading_client" progress overwrites the lead_ids that
    # the Search → Assign API endpoint stashed in progress for the
    # enrich_assigned path.
    run_row = db.fetch_one(
        "select client_id, mode, progress from crm.pipeline_runs where id = %s",
        (run_id,),
    )
    if not run_row:
        raise ValueError(f"pipeline run {run_id} not found")

    # ── enrich_assigned: enrich a specific list of existing leads ──
    # The Search → Assign UI inserts lightweight rows then enqueues a
    # run with this mode and lead_ids in progress JSONB. No discovery,
    # no targeting required — we just walk the list.
    if run_row.get("mode") == "enrich_assigned":
        _set_status(run_id, status="running", started_at=started)
        return _run_enrich_assigned(run_id, run_row)

    # Discovery path — safe to overwrite progress now.
    _set_status(run_id, status="running", started_at=started,
                progress={"phase": "loading_client"})

    client_id = run_row["client_id"]
    client = _load_client(client_id)

    industries = client.get("target_industries") or []
    locations = client.get("target_locations") or []
    if isinstance(industries, str):
        industries = json.loads(industries)
    if isinstance(locations, str):
        locations = json.loads(locations)

    if not industries or not locations:
        _set_status(
            run_id,
            status="failed",
            error_msg="Client has no target industries or locations configured.",
            finished_at=datetime.now(timezone.utc),
        )
        return {"leads_added": 0, "leads_skipped": 0, "leads_errored": 0}

    existing_keys = _existing_lead_keys(client_id)
    exclusions = _client_exclusion_set(client)

    counts = {"added": 0, "skipped": 0, "errored": 0}
    progress = {
        "phase": "discovering",
        "client": client["name"],
        "industries": list(industries),
        "locations": list(locations),
        "pairs_total": len(industries) * len(locations),
        "pairs_done": 0,
        "current": None,
    }
    _set_status(run_id, progress=progress)

    had_quota_error = False

    for industry in industries:
        for location in locations:
            progress["current"] = f"{industry} in {location}"
            _set_status(run_id, progress=progress)

            try:
                candidates = google_places.discover(industry, location, limit=40)
            except Exception as e:
                logger.exception("Places discovery failed for %s/%s", industry, location)
                progress.setdefault("errors", []).append(f"discovery {industry}/{location}: {e}")
                counts["errored"] += 1
                progress["pairs_done"] += 1
                _set_status(run_id, progress=progress)
                continue

            for biz in candidates:
                # Quota exhaustion marker from google_places.
                if biz.get("source_errors", {}).get("google_places", "").startswith("Geocode quota"):
                    had_quota_error = True
                if "quota" in (biz.get("source_errors", {}).get("google_places", "") or "").lower():
                    had_quota_error = True

                name_key = (biz.get("business_name") or "").strip().lower()
                if not name_key:
                    counts["skipped"] += 1
                    continue

                if (name_key, "") in existing_keys:
                    counts["skipped"] += 1
                    continue

                if _is_excluded(biz, exclusions):
                    counts["skipped"] += 1
                    continue

                # Stage 1 runs as part of _enrich_chain, but we need
                # the CH result BEFORE the rest of the chain so we can
                # drop dissolved companies and save the downstream
                # proxy/scraper budget. So we call CH directly first,
                # apply the stage-1 filter, then run the remaining chain.
                try:
                    biz = companies_house.enrich(biz)
                except Exception as e:
                    logger.exception("CH enrich failed")
                    biz.setdefault("source_errors", {})["companies_house"] = str(e)

                filter_reason = _stage1_filter_reason(biz)
                if filter_reason:
                    counts.setdefault("filtered_stage1", 0)
                    counts["filtered_stage1"] += 1
                    logger.info("stage1 filter dropped %r — %s",
                                biz.get("business_name", "")[:60], filter_reason)
                    continue

                # CH already ran; _enrich_chain is idempotent thanks to
                # per-enricher skip-if-set guards, so re-calling it is
                # safe and the duplication-vs-deduplication trade-off
                # comes out cleaner this way (one chain definition).
                biz = _enrich_chain(biz)
                score = _score_with_errors(biz)

                # Persist lead.
                try:
                    lead_id = _insert_lead(client_id, biz, score)
                except Exception as e:
                    logger.exception("Lead insert failed for %r", biz.get("business_name"))
                    counts["errored"] += 1
                    continue

                if not lead_id:
                    counts["skipped"] += 1
                    continue

                existing_keys.add((name_key, ""))

                # PECR compliance: under UK regulations, 'individual
                # subscribers' (sole traders, ordinary partnerships)
                # require consent for marketing email — legitimate
                # interests doesn't cover them. Ltd / PLC / LLP are
                # 'corporate subscribers' and fall under UK GDPR
                # legitimate interests instead.
                #
                # Sole traders aren't registered with Companies House
                # at all, so a missing CH match means we cannot verify
                # corporate status. Insert the lead (don't waste the
                # discovery work) but skip drafting — the operator can
                # manually re-grade if they have evidence the business
                # is actually incorporated.
                is_corporate = bool(biz.get("companies_house_number"))

                # Draft Day-1 only for grades worth contacting AND
                # verified-corporate subscribers.
                if score["grade"] in ("A", "B", "C") and is_corporate:
                    try:
                        # Resolve the campaign first — every email is
                        # owned by exactly one campaign for (client,
                        # hook_type). Lazily creates the campaign on the
                        # cold path (first lead of a new hook for this
                        # client). Drafter seeds with the campaign's
                        # best template if one is saved.
                        campaign_id = db.find_or_create_campaign(
                            client_id, score["hook_type"],
                        )
                        template_hint = db.best_template(campaign_id, step=1)
                        draft = drafter.draft_day1(
                            biz, score["hook_type"],
                            template_hint=template_hint,
                        )
                        _queue_day1_email(
                            lead_id, client_id, biz, draft,
                            campaign_id=campaign_id,
                            template_id=(template_hint or {}).get("id"),
                        )
                    except Exception as e:
                        logger.exception("Draft/queue failed for lead %s", lead_id)
                        progress.setdefault("errors", []).append(f"draft {lead_id}: {e}")
                elif score["grade"] in ("A", "B", "C") and not is_corporate:
                    # Audit the compliance-gated skip — surfaces in the
                    # run progress for the operator.
                    progress["compliance_gated"] = progress.get("compliance_gated", 0) + 1

                counts["added"] += 1

                # Heartbeat — surface progress every 5 leads to keep the modal lively.
                if counts["added"] % 5 == 0:
                    _set_status(
                        run_id,
                        leads_added=counts["added"],
                        leads_skipped=counts["skipped"],
                        leads_errored=counts["errored"],
                        progress=progress,
                    )

            progress["pairs_done"] += 1
            _set_status(run_id, progress=progress)

    finished = datetime.now(timezone.utc)
    final_status = "partial" if (had_quota_error or progress.get("errors")) else "succeeded"
    if counts["added"] == 0 and final_status == "succeeded":
        # Ran clean but produced nothing — likely a targeting issue.
        final_status = "partial"

    _set_status(
        run_id,
        status=final_status,
        leads_added=counts["added"],
        leads_skipped=counts["skipped"],
        leads_errored=counts["errored"],
        progress=progress,
        finished_at=finished,
    )

    try:
        _log_activity(client_id, "pipeline_run", {
            "run_id": run_id,
            "status": final_status,
            "leads_added": counts["added"],
            "leads_skipped": counts["skipped"],
            "leads_errored": counts["errored"],
        })
    except Exception:
        logger.exception("activity_log write failed for run %s", run_id)

    return {
        "leads_added": counts["added"],
        "leads_skipped": counts["skipped"],
        "leads_errored": counts["errored"],
    }


if __name__ == "__main__":
    # Manual one-off invocation for debugging:
    #   cd /srv/innovite/innovite-website/crm && python -m pipeline.runner <run_id>
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) != 2:
        print("usage: python -m pipeline.runner <run_id>", file=sys.stderr)
        sys.exit(1)
    out = run(int(sys.argv[1]))
    print(json.dumps(out))
