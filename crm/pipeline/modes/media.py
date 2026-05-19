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
from pipeline import vidora_audit
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
                db.upsert_vidora_lead(payload, pdf_path=None)
                counts["found"] += 1
            except Exception:
                logger.exception("upsert_vidora_lead failed for %s", handle)
                counts["errored"] += 1
                # Continue — one bad row shouldn't kill the search.

        return {
            "leads_found": counts["found"],
            "leads_errored": counts["errored"],
            "skipped_no_website": counts["skipped_no_website"],
            "skipped_no_handle": counts["skipped_no_handle"],
            "skipped_no_snapshot": counts["skipped_no_snapshot"],
            "skipped_no_audit": counts["skipped_no_audit"],
            "candidates_seen": len(candidates),
        }


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
