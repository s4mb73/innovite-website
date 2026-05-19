"""Mode strategy registry.

Two modes live in the CRM:
  - accountancy  -> ROCA pipeline (Companies House + LinkedIn enrichment)
  - media        -> Vidora pipeline (Google Places + IG vision audit)

The registry is the single dispatch point. The web app reads
`crm.searches.mode`, looks up the strategy, and calls
`discover / enrich / audit / write` in order. The runner stays mode-agnostic.

Strategies are duck-typed against the `Mode` protocol in `base.py`.
No ABC inheritance — Python protocols are enough for two implementations
and keep the call sites readable.
"""
from .base import Mode, ModeRegistry
from .accountancy import AccountancyMode
from .media import MediaMode

registry = ModeRegistry()
registry.register("accountancy", AccountancyMode())
registry.register("media", MediaMode())

__all__ = ["Mode", "ModeRegistry", "registry"]
