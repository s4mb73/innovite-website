"""Media mode — Vidora pipeline.

End-to-end orchestration of the Linux-native Vidora flow. Designed for
cost-and-time efficiency on a per-candidate basis — every step that
costs money or seconds runs only when prior cheap signals say it's
worth it.

Per-candidate flow:

    google_places.discover(industry, location)
       -> for each candidate:
            instagram_link.find_for_website(candidate.website)
                returns handle + cached homepage HTML for later reuse
            instagram_snapshot.snapshot(handle)
            _low_signal_reason(snapshot)
                cheap pre-filter: private, low followers, dormant, or
                <12 posts. Skip the audit (and storage) on definite-fail.
            _resolve_recipient(snapshot, business, homepage_body)
                IG business_email -> JSON-LD/mailto on cached homepage
                -> last-resort website scrape via email_patterns.detect.
                If nothing: store the lead with email_status='not_found'
                and skip the audit (saves the most expensive step on a
                lead we can't contact anyway).
            _media_recipient_gate(candidate_email)
                Local-only: syntax + MX + disposable check via
                email_verifier_local.precheck. No Reoon call — Vidora
                targets small-biz domains where role addresses dominate
                and catch-all detection isn't worth the paid step.
            vidora_audit.audit(snapshot)            <- ~$0.10 per call
            db.upsert_vidora_lead(payload)
            _draft_and_queue_day1                   <- A/B/C only

Skips that don't produce a lead are counted by reason so the operator
can see exactly where the funnel is leaking on each search.

Unlike accountancy, this mode does not use crm.pipeline_runs — it writes
directly via db.upsert_vidora_lead (which already handles idempotent
upsert on (source='vidora_instagram', external_id=<username>)).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import db
from pipeline import vidora_audit, vidora_drafter
from pipeline.vidora_drafter import TemplatedFallbackError
from pipeline.sources import google_places
from scraper import (
    email_patterns,
    email_verifier_local,
    instagram_link,
    instagram_snapshot,
)

logger = logging.getLogger("crm.pipeline.modes.media")


# ── Pre-filter thresholds ─────────────────────────────────────────────
# Each one gates the expensive Sonnet 4.6 vision call. Tuned for the
# "media-shy SMB" prospect Vidora targets — clinics, salons, fitness
# studios. Adjust as we learn what predicts a paying customer.
_MIN_FOLLOWERS      = 500
_MIN_POSTS          = 12     # audit guard refuses to score <9; below 12 the grid is too thin to grade meaningfully
_MAX_DAYS_INACTIVE  = 60     # last post older than 60 days = dormant


def _low_signal_reason(snap: dict) -> str | None:
    """Return a skip reason if the snapshot doesn't justify the audit cost.

    Cheap — purely arithmetic on fields already in the snapshot dict.
    Returns None when the profile is worth grading.
    """
    if snap.get("is_private"):
        return "private"
    followers = snap.get("follower_count") or 0
    if followers < _MIN_FOLLOWERS:
        return f"followers<{_MIN_FOLLOWERS}"
    post_count = snap.get("post_count") or 0
    if post_count < _MIN_POSTS:
        return f"posts<{_MIN_POSTS}"
    last_post_iso = snap.get("last_post_at")
    if last_post_iso:
        try:
            last = datetime.fromisoformat(last_post_iso.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - last).days
            if age_days > _MAX_DAYS_INACTIVE:
                return f"inactive>{_MAX_DAYS_INACTIVE}d"
        except (ValueError, TypeError):
            pass  # malformed timestamp — let the audit decide
    return None


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

        counts = {
            "found":                0,
            "skipped_no_website":   0,
            "skipped_no_handle":    0,
            "skipped_no_snapshot":  0,
            "skipped_low_signal":   0,
            "stored_no_recipient":  0,  # stored as not_found, audit skipped
            "skipped_no_audit":     0,  # snapshot passed filters + recipient found, audit refused
            "errored":              0,
        }
        skipped_low_signal_reasons: dict[str, int] = {}

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

            # Cheap pre-filter — kills the most-likely-Grade-F audits
            # before they cost us $0.10 + 30s. Saved candidates aren't
            # stored: an account with <500 followers / dormant / private
            # isn't a Vidora prospect at all.
            reason = _low_signal_reason(snap)
            if reason:
                counts["skipped_low_signal"] += 1
                skipped_low_signal_reasons[reason] = (
                    skipped_low_signal_reasons.get(reason, 0) + 1
                )
                logger.info(
                    "vidora low-signal skip: @%s (%s)", handle, reason
                )
                continue

            # Resolve recipient BEFORE the audit. A lead we can't contact
            # is worth storing (operator can manually research) but the
            # audit grade adds no immediate value — skip the expensive
            # vision call and mark it not_found.
            homepage_body = link.get("homepage_body")
            candidate_email = _resolve_recipient(
                snap, biz, homepage_body=homepage_body,
            )
            if not candidate_email:
                payload = _build_payload(biz, snap, audit=None)
                try:
                    lead_id = db.upsert_vidora_lead(payload, pdf_path=None)
                    db.execute(
                        "update crm.leads set email_status = 'not_found' "
                        "where id = %s",
                        (lead_id,),
                    )
                    counts["stored_no_recipient"] += 1
                    logger.info(
                        "vidora lead %s: stored without audit — no recipient",
                        lead_id,
                    )
                except Exception:
                    logger.exception(
                        "upsert_vidora_lead (no-recipient path) failed for %s",
                        handle,
                    )
                    counts["errored"] += 1
                continue

            # Recipient candidate exists. Now the audit is justified.
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
                continue

            try:
                _draft_and_queue_day1(
                    lead_id=lead_id,
                    client_id=search["client_id"],
                    audit=audit,
                    snapshot=snap,
                    business=biz,
                    candidate_email=candidate_email,
                )
            except TemplatedFallbackError as e:
                # Drafter couldn't produce a real email — flag the lead for
                # manual follow-up instead of shipping the debug fallback.
                # The audit + recipient are already persisted; only the
                # email_subject/body and crm.emails row are missing.
                logger.warning(
                    "vidora lead %s: %s — marking email_status=not_found",
                    lead_id, e,
                )
                db.execute(
                    "update crm.leads set email_status = 'not_found' "
                    "where id = %s",
                    (lead_id,),
                )
            except Exception:
                logger.exception("vidora draft/queue failed for lead %s", lead_id)
                counts["errored"] += 1

        return {
            "leads_found":               counts["found"],
            "leads_errored":             counts["errored"],
            "skipped_no_website":        counts["skipped_no_website"],
            "skipped_no_handle":         counts["skipped_no_handle"],
            "skipped_no_snapshot":       counts["skipped_no_snapshot"],
            "skipped_low_signal":        counts["skipped_low_signal"],
            "skipped_low_signal_reasons": skipped_low_signal_reasons,
            "stored_no_recipient":       counts["stored_no_recipient"],
            "skipped_no_audit":          counts["skipped_no_audit"],
            "candidates_seen":           len(candidates),
        }


def _resolve_recipient(
    snapshot: dict, business: dict, *, homepage_body: str | None = None,
) -> str | None:
    """Find a candidate email for the lead.

    Three-step lookup, cheapest first:
      1. snapshot.business_email (or public_email fallback) — set by the
         IG account holder via Professional Dashboard. Most reliable.
      2. If homepage_body was already fetched by instagram_link, scan it
         in-memory via email_patterns.extract_from_html — free, no I/O.
      3. Full website scrape via email_patterns.detect() as last resort —
         fetches contact paths through the proxy pool. Most expensive.

    Returns the first candidate or None. Does NOT gate — caller
    must run _media_recipient_gate() before persisting.
    """
    ig_email = snapshot.get("business_email") or snapshot.get("public_email")
    if ig_email:
        return ig_email.strip().lower()

    website = (business.get("website") or "").strip()
    if not website:
        return None

    # Step 2: free scan of the homepage we already have in memory.
    if homepage_body:
        try:
            hits = email_patterns.extract_from_html(homepage_body, website)
        except Exception:
            logger.exception(
                "email_patterns.extract_from_html failed for %s", website[:80]
            )
            hits = None
        if hits:
            candidates = (list(hits.get("personal_emails") or [])
                          + list(hits.get("role_emails") or []))
            if candidates:
                return candidates[0]

    # Step 3: fall through to a full proxy-pool fetch of contact paths.
    try:
        det = email_patterns.detect(website, officers=None)
    except Exception:
        logger.exception("email_patterns.detect failed for %s", website[:80])
        return None
    candidates = (list(det.get("personal_emails") or [])
                  + list(det.get("role_emails") or []))
    return candidates[0] if candidates else None


# Local-only acceptance gate for Vidora.
#
# Accountancy targets named decision-makers (jane.doe@firm.co.uk) where
# Reoon's catch-all detection is worth paying for — sending a guessed
# first.last to a catch-all domain looks spammy. Vidora targets clinics
# / salons / restaurants where the available addresses are nearly always
# role accounts (hello@, info@, bookings@) on small business domains.
# Those domains rarely run catch-all-with-quarantine SaaS suites; an
# MX-confirmed role address bounces or it doesn't, and Reoon doesn't
# help us distinguish the two. So we drop the paid call on this path
# entirely.
#
# Accepted iff:
#   - syntactically valid
#   - domain has MX records (mail infrastructure exists)
#   - domain is not on the disposable-provider list
# i.e. email_verifier_local.precheck returns status == 'plausible'.

def _media_recipient_gate(email: str) -> dict:
    """Run the local-only Vidora gate. Returns the precheck verdict
    augmented with `passes: bool` and the persistable column values
    (status, confidence, source) the lead row expects.

    No paid Reoon call. No REOON_API_KEY required on this path.
    """
    v = email_verifier_local.precheck(email)
    passes = v.get("status") == "plausible"
    return {
        "passes":      passes,
        "status":      v.get("status"),
        "confidence":  "medium" if passes else "none",
        "source":      "local_only",
        "reason":      v.get("reason"),
    }


def _draft_and_queue_day1(*, lead_id: int, client_id: int, audit: dict,
                          snapshot: dict, business: dict,
                          candidate_email: str) -> None:
    """Verify the recipient, draft Day-1, queue if shippable.

    candidate_email is already resolved by MediaMode.run — passed
    through instead of re-resolved here.
    """
    draft = vidora_drafter.draft_day1(audit, snapshot, business)
    db.execute(
        "update crm.leads set email_subject = %s, email_body_day1 = %s "
        "where id = %s",
        (draft["subject"], draft["body"], lead_id),
    )

    gate = _media_recipient_gate(candidate_email)

    # Persist gate verdict even when we reject — same diagnostic surface
    # accountancy leads have. checked_at written in DB-now for parity
    # with the Reoon path's datetime field.
    db.execute(
        """update crm.leads set
               email_verification_status     = %s,
               email_verification_confidence = %s,
               email_verification_source     = %s,
               email_verification_checked_at = now()
             where id = %s""",
        (gate["status"], gate["confidence"], gate["source"], lead_id),
    )

    grade = (audit.get("grade") or "").upper()
    if not gate["passes"]:
        db.execute(
            "update crm.leads set email_status = 'not_found' where id = %s",
            (lead_id,),
        )
        logger.info(
            "vidora lead %s: recipient %s rejected (%s — %s)",
            lead_id, candidate_email, gate["status"], gate["reason"],
        )
        return

    # Verifier approved. Mark found + write recipient regardless of grade
    # so the operator has the contact even on D/F leads.
    db.execute(
        """update crm.leads
              set decision_maker_email = %s,
                  email_status         = 'found'
            where id = %s""",
        (candidate_email, lead_id),
    )

    # Auto-queue Day-1 only for grades worth contacting. D/F grades land
    # with a contact + cached draft but no scheduled email; operator
    # decides whether to pitch manually.
    if grade not in ("A", "B", "C"):
        logger.info(
            "vidora lead %s: contact found, grade %s — manual pitch",
            lead_id, grade or "?",
        )
        return

    db.execute(
        """insert into crm.emails
             (lead_id, client_id, email_number, subject, body,
              to_address, status, scheduled_at,
              needs_approval, approved_at, approved_by)
           values (%s, %s, 1, %s, %s, %s, 'scheduled', null,
                   false, now(), 'mediamode_auto')""",
        (lead_id, client_id, draft["subject"], draft["body"], candidate_email),
    )


def _build_payload(biz: dict, snap: dict, audit: dict | None) -> dict:
    """Shape the upsert_vidora_lead payload from the three sources.

    audit is optional — the no-recipient path stores a lead without
    grading to save the audit cost. Grade/score/weaknesses fields end
    up null on those rows (schema allows it).
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
        "lead_grade":        (audit or {}).get("grade"),
        "overall_score":     (audit or {}).get("overall_score"),
        "top_weaknesses":    (audit or {}).get("weaknesses") or [],
        "scores":            (audit or {}).get("scores") or {},
        "sales_notes":       (audit or {}).get("summary"),
        "personalised_pitch": (audit or {}).get("sales_hook"),
        "audit_version":     (audit or {}).get("model"),
    }
