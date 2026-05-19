"""Media mode — Vidora pipeline.

End-to-end orchestration of the Linux-native Vidora flow:

    google_places.discover(industry, location)
       -> for each candidate:
            instagram_link.find_for_website(candidate.website)
            instagram_snapshot.snapshot(handle)
            vidora_audit.audit(snapshot)
            db.upsert_vidora_lead(payload, pdf_path=None)

Skips that don't produce a lead (no website, no IG handle, no snapshot,
no audit) are counted and logged; they're not errors.

Unlike accountancy, this mode does not use crm.pipeline_runs — it writes
directly via db.upsert_vidora_lead (which already handles idempotent
upsert on (source='vidora_instagram', external_id=<username>)).
"""
from __future__ import annotations

import logging
from typing import Any

import db
from pipeline import vidora_audit, vidora_drafter
from pipeline.sources import google_places
from scraper import instagram_link, instagram_snapshot

logger = logging.getLogger("crm.pipeline.modes.media")


class MediaMode:
    name = "media"

    def run(self, search_id: int) -> dict[str, Any]:
        search = db.fetch_one(
            "select id, client_id, params from crm.searches where id = %s",
            (search_id,),
        )
        if not search:
            raise ValueError(f"search {search_id} not found")

        params = search["params"] or {}
        industry = params.get("industry") or params.get("query") or ""
        location = params.get("location") or ""
        limit = int(params.get("limit") or 40)

        if not industry or not location:
            return {
                "leads_found": 0,
                "leads_errored": 0,
                "error": "media search requires params.industry and params.location",
            }

        candidates = google_places.discover(industry, location, limit=limit)

        counts = {"found": 0, "skipped_no_website": 0, "skipped_no_handle": 0,
                  "skipped_no_snapshot": 0, "skipped_no_audit": 0, "errored": 0}

        for biz in candidates:
            website = (biz.get("website") or "").strip()
            if not website:
                counts["skipped_no_website"] += 1
                continue

            link = instagram_link.find_for_website(website)
            handle = link.get("handle")
            if not handle:
                counts["skipped_no_handle"] += 1
                continue

            snap = instagram_snapshot.snapshot(handle)
            if not snap:
                counts["skipped_no_snapshot"] += 1
                continue

            audit = vidora_audit.audit(snap)
            if not audit:
                counts["skipped_no_audit"] += 1
                continue

            payload = _build_payload(biz, snap, audit)
            try:
                lead_id = db.upsert_vidora_lead(payload, pdf_path=None)
                counts["found"] += 1
            except Exception:
                logger.exception("upsert_vidora_lead failed for %s", handle)
                counts["errored"] += 1
                # Continue — one bad row shouldn't kill the search.
                continue

            # Draft Day-1 only for grades worth contacting. D/F grades
            # skip the Anthropic call — same gating policy as the
            # accountancy drafter (runner.py:418).
            grade = (audit.get("grade") or "").upper()
            if grade in ("A", "B", "C"):
                try:
                    _draft_and_queue_day1(
                        lead_id=lead_id,
                        client_id=search["client_id"],
                        audit=audit,
                        snapshot=snap,
                        business=biz,
                    )
                except Exception:
                    logger.exception("vidora draft/queue failed for lead %s", lead_id)
                    # Lead already exists — surface the draft failure
                    # in counters but don't drop the row.
                    counts["errored"] += 1

        return {
            "leads_found": counts["found"],
            "leads_errored": counts["errored"],
            "skipped_no_website": counts["skipped_no_website"],
            "skipped_no_handle": counts["skipped_no_handle"],
            "skipped_no_snapshot": counts["skipped_no_snapshot"],
            "skipped_no_audit": counts["skipped_no_audit"],
            "candidates_seen": len(candidates),
        }


def _draft_and_queue_day1(*, lead_id: int, client_id: int, audit: dict,
                          snapshot: dict, business: dict) -> None:
    """Generate the Day-1 cold email and queue it for outreach.

    Mirrors runner._queue_day1_email's split contract: when we have a
    deliverable to-address, insert a scheduled row in crm.emails;
    when we don't, still cache the draft on the lead so the operator
    can review (and manually paste in a recipient).
    """
    draft = vidora_drafter.draft_day1(audit, snapshot, business)
    db.execute(
        "update crm.leads set email_subject = %s, email_body_day1 = %s "
        "where id = %s",
        (draft["subject"], draft["body"], lead_id),
    )

    # The Vidora pipeline doesn't currently produce a verified
    # decision_maker_email — IG snapshot doesn't expose business_email,
    # and the audit only generates copy. If a future enricher lands
    # one on the lead row, _ready_emails picks it up automatically.
    # For now, cache-only when no to_address is known.
    to_row = db.fetch_one(
        "select decision_maker_email from crm.leads where id = %s",
        (lead_id,),
    )
    to_addr = (to_row or {}).get("decision_maker_email") or ""
    if not to_addr:
        return

    db.execute(
        """insert into crm.emails
             (lead_id, client_id, email_number, subject, body,
              to_address, status, scheduled_at)
           values (%s, %s, 1, %s, %s, %s, 'scheduled', null)""",
        (lead_id, client_id, draft["subject"], draft["body"], to_addr),
    )


def _build_payload(biz: dict, snap: dict, audit: dict) -> dict:
    """Shape the upsert_vidora_lead payload from the three sources.

    Keys mirror the legacy Windows-runner bridge so the existing upsert
    function (db.upsert_vidora_lead) accepts both inputs unchanged.
    """
    return {
        "username":          snap.get("handle"),
        "business_name":     biz.get("business_name") or snap.get("full_name"),
        "maps_address":      biz.get("address"),
        "maps_phone":        biz.get("phone"),
        "maps_website":      biz.get("website"),
        "maps_rating":       biz.get("google_rating"),
        "maps_review_count": biz.get("google_review_count"),
        "maps_url":          biz.get("google_maps_url"),
        "followers":         snap.get("follower_count"),
        "engagement_rate":   None,
        "posting_frequency": None,
        "avg_likes":         None,
        "last_post_date":    (snap.get("last_post_at") or "")[:10] or None,
        "lead_grade":        audit.get("grade"),
        "overall_score":     audit.get("overall_score"),
        "top_weaknesses":    audit.get("weaknesses") or [],
        "scores":            audit.get("scores") or {},
        "sales_notes":       audit.get("summary"),
        "personalised_pitch": audit.get("sales_hook"),
        "audit_version":     audit.get("model"),
    }
