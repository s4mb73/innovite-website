"""Media mode — Vidora pipeline.

Stub. Wires to the new Linux-VPS scraper stack:
  1. google_places.search()          -> business + website
  2. instagram_link.find_for_website -> regex IG handle off homepage
  3. instagram_snapshot.snapshot()   -> public web_profile_info (no auth)
  4. vidora_audit.audit()            -> Claude Sonnet 4.6 vision scores grid

Smoke-tested end-to-end on Manchester Aesthetics (Grade F, image-grounded
weaknesses, sales hook generated). Working as of the rebuild.
"""
from __future__ import annotations

from typing import Any


class MediaMode:
    name = "media"

    def discover(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "MediaMode.discover: wire to crm.pipeline.sources.google_places"
        )

    def enrich(self, lead: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(
            "MediaMode.enrich: wire to instagram_link.find_for_website + "
            "instagram_snapshot.snapshot()"
        )

    def audit(self, lead: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(
            "MediaMode.audit: wire to vidora_audit.audit() — writes "
            "vidora_audit_grade / vidora_audit_score / vidora_audit_weaknesses "
            "/ vidora_sales_hook / vidora_snapshot / vidora_audited_at / "
            "audit_version on the lead"
        )

    def write(self, client_id: int, lead: dict[str, Any]) -> int:
        raise NotImplementedError(
            "MediaMode.write: wire to db.upsert_vidora_lead() — the existing "
            "vidora upsert path stays, schema-side it now lives under mode='media'"
        )
