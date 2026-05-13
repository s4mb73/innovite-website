"""Data sources for the pipeline runner.

Each source is one of two shapes:

1. **Discovery source** — takes a target (industry, location) and returns
   a list of candidate business dicts. Currently: google_places only.

2. **Enrichment source** — takes a business dict and returns it with
   additional fields populated. Currently: companies_house, apollo (stub).

Sources are designed to be swappable behind the same shape so that the
runner does not have to know which provider is in use. This is the seam
that lets us plug Apollo → CustomEmailProvider → Hybrid in later without
touching the runner.

A failing source must never bring the whole run down. Each source
catches its own errors and returns partial results with a 'source_error'
key on the business dict if anything went wrong.
"""
from typing import Protocol, TypedDict


class Business(TypedDict, total=False):
    """The data shape that flows between sources.

    Fields are filled progressively — discovery sets a few, each enrichment
    source adds more. Anything not yet known is simply absent.
    """
    # Identity (from discovery)
    business_name: str
    address: str
    postcode: str
    city: str
    phone: str
    website: str

    # Google
    google_rating: float
    google_review_count: int
    google_maps_url: str
    google_place_id: str

    # Companies House
    companies_house_number: str
    companies_house_sic_code: str
    companies_house_incorporated: str  # ISO date string
    companies_house_revenue_band: str
    companies_house_status: str
    companies_house_officer_count: int

    # Decision maker (Apollo or future custom provider)
    decision_maker_name: str
    decision_maker_title: str
    decision_maker_email: str
    decision_maker_email_confidence: float
    linkedin_url: str

    # Per-source error trail — sources append to this rather than raising.
    source_errors: dict[str, str]


class DiscoverySource(Protocol):
    def discover(self, industry: str, location: str, limit: int = 60) -> list[Business]:
        ...


class EnrichmentSource(Protocol):
    name: str

    def enrich(self, business: Business) -> Business:
        ...
