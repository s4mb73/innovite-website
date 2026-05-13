"""Apollo.io decision-maker enrichment — STUB.

Status: stub. Apollo signup is on Sammy. Once an APOLLO_API_KEY is set,
this module's enrich() will:
  1. POST /v1/mixed_people/search with the company domain + decision-maker
     title filters (Founder/MD/Director/Owner)
  2. Pull the first result's name, title, email, LinkedIn URL
  3. Attach to the business dict with confidence

The runner already handles the case where decision_maker_email is empty
(downgrades scoring + skips email draft generation), so today's runs
proceed without Apollo — they just produce leads without director emails.

When Apollo lands, this is also the seam where the deferred
CustomEmailProvider (CH officers + pattern detection + verifier) plugs
in via INNOVITE_EMAIL_PROVIDER=apollo|custom|hybrid. The Protocol shape
in this module is stable — switching providers is one env var.

See: project_innovite_custom_email_provider.md
"""
from __future__ import annotations

import os

from pipeline.sources import Business

name = "apollo"


def enrich(business: Business) -> Business:
    """Attach decision-maker fields to the business via Apollo.

    Today: returns the business unchanged with a clear 'not configured'
    error so the run still produces leads. Scoring will downgrade them
    for missing director email rather than fail the run.
    """
    api_key = os.environ.get("APOLLO_API_KEY")
    if not api_key:
        business.setdefault("source_errors", {})["apollo"] = "APOLLO_API_KEY not set — Apollo signup pending"
        return business

    # TODO: real implementation when Apollo signup lands.
    # 1. Derive domain from business.website
    # 2. POST https://api.apollo.io/v1/mixed_people/search
    #    body: {q_organization_domains, person_titles, page, per_page}
    # 3. Take first result, attach decision_maker_*
    business.setdefault("source_errors", {})["apollo"] = "stub — implementation pending"
    return business
