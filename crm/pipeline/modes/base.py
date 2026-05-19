"""Mode protocol and registry."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Mode(Protocol):
    """A pipeline strategy for one client_type.

    Lifecycle of a search run:
        candidates = mode.discover(params)
        for c in candidates:
            enriched = mode.enrich(c)
            audited  = mode.audit(enriched)
            mode.write(client_id, audited)
    """

    name: str

    def discover(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Return raw candidate businesses from the mode's discovery source."""
        ...

    def enrich(self, lead: dict[str, Any]) -> dict[str, Any]:
        """Add mode-specific enrichment (Companies House, IG snapshot, etc.)."""
        ...

    def audit(self, lead: dict[str, Any]) -> dict[str, Any]:
        """Grade the lead. Returns the lead dict with grade + weaknesses set."""
        ...

    def write(self, client_id: int, lead: dict[str, Any]) -> int:
        """Persist to crm.leads. Returns the new lead id."""
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
