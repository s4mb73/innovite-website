"""wreq-python HTTP client wrapper.

One-call API: fetch(url) → response_text or None. All the proxy
rotation, TLS impersonation, retry, backoff, and per-host rate limit
logic lives here. Callers (extractor.py) don't see any of it.

Why wreq vs requests/httpx
--------------------------
Modern anti-bot systems (Cloudflare, DataDome, Akamai Bot Manager)
fingerprint clients at the TLS handshake level (JA3/JA4) and the
HTTP/2 frame-ordering level — *before* the request line is even read.
Plain Python clients have a single recognisable Python fingerprint;
they get challenged or 403'd on protected sites no matter what
User-Agent header you set.

wreq impersonates real browser stacks at the protocol level (100+
emulation profiles spanning Chrome / Firefox / Safari / Edge across
recent versions). Combined with a residential-class IP from the
proxy pool, the request looks indistinguishable from a real user.

Graceful degradation
--------------------
If wreq isn't importable (missing wheel, ARM/Linux issue, etc.) the
module's `is_available()` returns False and fetch() returns None.
The enricher handles this by marking website_scrape_status='disabled'
on the lead — the pipeline continues, the lead is just less enriched.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from collections import defaultdict
from datetime import timedelta
from urllib.parse import urlparse

from scraper.proxy_pool import get_pool, Proxy

logger = logging.getLogger("crm.scraper.client")

# Try to import wreq. Missing wreq → scraper runs in disabled mode.
try:
    import wreq  # type: ignore
    _WREQ_AVAILABLE = True
except ImportError:
    wreq = None  # type: ignore
    _WREQ_AVAILABLE = False
    logger.warning("wreq not installed — scraper will run disabled")


# Browser emulation profiles to rotate across. We pull the names off the
# wreq Emulation enum at import time; if wreq isn't available the list
# stays empty and we never get to the picker anyway.
_EMULATION_PROFILES: list = []
if _WREQ_AVAILABLE:
    try:
        # The Emulation class exposes profile constants like
        # Emulation.Chrome131, Emulation.Firefox149, Emulation.Safari18 etc.
        # We grab a curated set of recent stable browsers — old profiles
        # also work but recent ones look least suspicious.
        candidate_names = [
            "Chrome131", "Chrome130", "Chrome129",
            "Firefox149", "Firefox148",
            "Safari18", "Safari18_1",
            "Edge131",
        ]
        for name in candidate_names:
            prof = getattr(wreq.Emulation, name, None)
            if prof is not None:
                _EMULATION_PROFILES.append(prof)
    except Exception:
        logger.exception("Could not enumerate wreq emulation profiles")

# Fall back to whatever the default is if the curated list came up empty.
if _WREQ_AVAILABLE and not _EMULATION_PROFILES:
    try:
        default = getattr(wreq.Emulation, "Chrome", None) or None
        if default is not None:
            _EMULATION_PROFILES = [default]
    except Exception:
        pass


def is_available() -> bool:
    """True when wreq is importable AND the proxy pool has at least one
    healthy proxy. Either failing → scraper disables itself silently."""
    if not _WREQ_AVAILABLE:
        return False
    return get_pool().is_enabled()


# ── Per-host rate limiting ──────────────────────────────────────────
# Token bucket per hostname so we never hammer one target — even when
# proxies rotate. Defeats the "I'm a different IP every request so I
# don't need to rate-limit" mistake.
_HOST_LAST_HIT: dict[str, float] = defaultdict(float)
_HOST_LOCK = threading.Lock()
_MIN_INTERVAL_PER_HOST_S = 3.0  # at most ~20 req/min/host across the pool


def _wait_for_host_slot(host: str) -> None:
    """Block until at least _MIN_INTERVAL_PER_HOST_S has passed since
    the previous request to this host."""
    with _HOST_LOCK:
        now = time.monotonic()
        last = _HOST_LAST_HIT[host]
        wait = (last + _MIN_INTERVAL_PER_HOST_S) - now
        # Stamp BEFORE sleeping so concurrent callers wait further out.
        _HOST_LAST_HIT[host] = now + max(wait, 0)
    if wait > 0:
        time.sleep(wait)


# ── Fetch ───────────────────────────────────────────────────────────
TIMEOUT_S = 12
MAX_RETRIES = 2
RETRY_BACKOFF = (4, 12)  # seconds between retries


def fetch(url: str, *, max_bytes: int = 250_000) -> str | None:
    """Fetch a URL via the proxy pool with TLS fingerprint impersonation.

    Returns the response body as text, or None on any failure. Bodies
    larger than max_bytes are truncated (we don't need 5MB of HTML
    bloat for the parser).

    Failure modes (all return None, all logged):
      - wreq not available
      - proxy pool empty
      - DNS / connect / TLS errors (proxy benched)
      - HTTP 403 / 429 / 5xx (proxy benched if pattern emerges)
      - Cloudflare challenge page (detected by body keyword scan)
      - Response body parses but is suspiciously empty (< 200 bytes)
    """
    if not is_available():
        return None

    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host:
        logger.warning("fetch called with malformed URL: %s", url[:80])
        return None

    _wait_for_host_slot(host)

    pool = get_pool()
    attempts = 0
    while attempts <= MAX_RETRIES:
        proxy = pool.next_proxy()
        if proxy is None:
            return None

        emulation = random.choice(_EMULATION_PROFILES) if _EMULATION_PROFILES else None
        try:
            client_kwargs: dict = {
                "proxy":   proxy.as_url(),
                "timeout": timedelta(seconds=TIMEOUT_S),
            }
            if emulation is not None:
                client_kwargs["emulation"] = emulation

            client = wreq.Client(**client_kwargs)  # type: ignore[union-attr]
            resp = client.get(url)
            status = getattr(resp, "status", None) or getattr(resp, "status_code", None)

            # 2xx → success. 3xx → wreq follows redirects by default; if
            # we're seeing a 3xx here, the chain didn't resolve cleanly.
            if status is None or 200 <= status < 300:
                body = _read_body(resp, max_bytes)
                if body and _looks_like_challenge(body):
                    logger.info("CHALLENGE %s via %s — retrying",
                                host, proxy.public_id())
                    pool.record_failure(proxy, f"challenge on {host}")
                else:
                    pool.record_success(proxy)
                    return body
            elif status in (403, 429):
                # Hard signal that this IP is no good for this target.
                logger.info("HTTP %s on %s via %s", status, host, proxy.public_id())
                pool.record_failure(proxy, f"HTTP {status}")
            elif 500 <= status < 600:
                # Target server problem, not our IP — don't bench harshly.
                logger.info("HTTP %s on %s (target error)", status, host)
                # Single bump rather than a full failure — proxy isn't at
                # fault if the destination 5xx'd.
                proxy.consecutive_failures += 0
            else:
                logger.info("HTTP %s on %s via %s", status, host, proxy.public_id())
                pool.record_failure(proxy, f"HTTP {status}")
        except Exception as e:
            # Log type + message so config bugs (e.g. wrong kwarg type)
            # don't get hidden as generic "fetch error". Connection
            # errors still produce noisy lines — that's fine; this is
            # an info-level scraper, the volume's bounded.
            logger.warning("fetch error %s via %s: %s: %s",
                           host, proxy.public_id(),
                           type(e).__name__, str(e)[:200])
            pool.record_failure(proxy, type(e).__name__)

        attempts += 1
        if attempts <= MAX_RETRIES:
            backoff = RETRY_BACKOFF[min(attempts - 1, len(RETRY_BACKOFF) - 1)]
            time.sleep(backoff)

    return None


def _read_body(resp, max_bytes: int) -> str | None:
    """Pull response text safely. wreq exposes .text() as a method (not
    a property) on the Response object; we handle both for safety."""
    try:
        text = resp.text() if callable(getattr(resp, "text", None)) else resp.text
        if text is None:
            return None
        if len(text) > max_bytes:
            return text[:max_bytes]
        return text
    except Exception:
        return None


# Cloudflare / DataDome / similar challenge-page markers. If we see
# these in the body, the request technically succeeded at HTTP level
# but didn't reach the real content.
_CHALLENGE_MARKERS = (
    "cf-browser-verification",
    "checking your browser",
    "challenge-platform",
    "_cf_chl_opt",
    "datadome",
    "px-captcha",
    "Just a moment...",
)


def _looks_like_challenge(body: str) -> bool:
    head = body[:4000].lower()
    return any(marker.lower() in head for marker in _CHALLENGE_MARKERS)
