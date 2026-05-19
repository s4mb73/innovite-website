"""Accountancy mode — ROCA pipeline.

Stub. Wires to the existing Companies House + LinkedIn sources once the
runner is refactored to call modes instead of running its hard-coded flow.
Today the existing `crm.pipeline.runner` handles this work directly;
the migration to mode-dispatch is the next step.
"""
from __future__ import annotations

from typing import Any


class AccountancyMode:
    name = "accountancy"

    def discover(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "AccountancyMode.discover: wire to existing google_places + "
            "companies_house sources in crm/pipeline/sources/"
        )

    def enrich(self, lead: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(
            "AccountancyMode.enrich: wire to companies_house + apollo + "
            "website-signals scraper"
        )

    def audit(self, lead: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(
            "AccountancyMode.audit: wire to crm.pipeline.scoring.grade()"
        )

    def write(self, client_id: int, lead: dict[str, Any]) -> int:
        raise NotImplementedError(
            "AccountancyMode.write: wire to db.upsert_lead()"
        )
