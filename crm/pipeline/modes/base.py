"""Mode protocol and registry.

Mode = "which pipeline strategy turns a search row into rows in crm.leads".
Each mode owns its own orchestration end-to-end. We deliberately do NOT
force a shared discover/enrich/audit/write lifecycle: accountancy runs
~8 enrichment passes plus a PECR compliance gate plus a drafter, while
media has an IG-handle resolution step that can fail before any audit
work happens. Forcing both into a common shape produces leaky
abstractions both directions.

A mode is just `name + run(search_id) -> dict`. The dict shape is the
counters written back to crm.searches (leads_found, errors, ...).
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Mode(Protocol):
    name: str

    def run(self, search_id: int) -> dict[str, Any]:
        """Execute the strategy for one crm.searches row.

        Returns a result dict the worker writes back to crm.searches.
        Must include at least `leads_found: int`. May include `error: str`
        on partial failure (raising is reserved for unrecoverable errors).
        """
        ...


class ModeRegistry:
    """Name -> Mode dispatch."""

    def __init__(self) -> None:
        self._modes: dict[str, Mode] = {}

    def register(self, name: str, mode: Mode) -> None:
        if name in self._modes:
            raise ValueError(f"Mode already registered: {name}")
        self._modes[name] = mode

    def get(self, name: str) -> Mode:
        try:
            return self._modes[name]
        except KeyError:
            raise KeyError(
                f"Unknown mode: {name!r}. Registered: {list(self._modes)}"
            ) from None

    def names(self) -> list[str]:
        return list(self._modes)
