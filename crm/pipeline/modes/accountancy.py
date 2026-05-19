"""Accountancy mode — ROCA pipeline.

Wires the search-row dispatch to the existing pipeline_runs flow.
A search-row is converted to a pipeline_runs row with mode='manual',
the existing runner does the heavy lifting (Companies House + DNS +
LinkedIn + Gazette + Reed + website scrape + scoring + drafter), and
we mirror the counts back onto the search row.

Why we don't tear runner.run apart into discover/enrich/audit/write:
the accountancy flow is ~8 enrichment passes with a PECR compliance
gate before drafting. Slicing it into mode-protocol hooks would either
leak detail or hide it. Reusing runner.run wholesale keeps the proven
path intact.
"""
from __future__ import annotations

import logging
from typing import Any

import db
from pipeline import runner

logger = logging.getLogger("crm.pipeline.modes.accountancy")


class AccountancyMode:
    name = "accountancy"

    def run(self, search_id: int) -> dict[str, Any]:
        search = db.fetch_one(
            "select id, client_id, params from crm.searches where id = %s",
            (search_id,),
        )
        if not search:
            raise ValueError(f"search {search_id} not found")

        run_row = db.fetch_one(
            """insert into crm.pipeline_runs (client_id, status, mode, triggered_by)
               values (%s, 'pending', 'manual', 'search')
               returning id""",
            (search["client_id"],),
        )
        run_id = run_row["id"]
        logger.info("accountancy search %s -> pipeline_run %s", search_id, run_id)

        result = runner.run(run_id)

        return {
            "leads_found": result.get("leads_added", 0),
            "leads_skipped": result.get("leads_skipped", 0),
            "leads_errored": result.get("leads_errored", 0),
            "pipeline_run_id": run_id,
        }
