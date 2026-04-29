"""Typed data models for query results.

Kept thin on purpose — dataclasses are added per-step alongside the
queries that return them. The first ones arrive in Step 3 (Overview).
"""
from dataclasses import dataclass


@dataclass(slots=True)
class Client:
    id: int
    name: str
    industry: str | None
    status: str
