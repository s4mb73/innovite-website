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

import asyncio
import inspect
import logging
import os
import random
import threading
import time
from collections import defaultdict
from datetime import timedelta
from urllib.parse import urlparse

from scraper.proxy_pool import get_pool, Proxy
from scraper import linkedin_session

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


def fetch(url: str, *, max_bytes: int = 250_000, headers: dict | None = None) -> str | None:
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

    # LinkedIn requests are routed through a different path: per-account
    # cookie, sticky proxy pinned to that account, halt-on-challenge.
    # The cookie-based session lives or dies as one unit, so we never
    # fall back to the generic round-robin path for these hosts.
    if _is_linkedin_host(host):
        return _fetch_linkedin(url, host, max_bytes=max_bytes)

    _wait_for_host_slot(host)

    pool = get_pool()
    attempts = 0
    while attempts <= MAX_RETRIES:
        proxy = pool.next_proxy()
        if proxy is None:
            return None

        emulation = random.choice(_EMULATION_PROFILES) if _EMULATION_PROFILES else None
        try:
            # wreq.Client takes `proxies=[Proxy.all(url)]` (plural, list
            # of Proxy objects), not `proxy="url"`. Proxy.all() routes
            # both HTTP and HTTPS traffic — what we want for SMB
            # website scraping where targets are a mix of plain http
            # redirects and https.
            #
            # wreq does NOT follow redirects by default — the default
            # policy is `none`, which surfaces 301/302 as the final
            # status. SMB sites redirect heavily (apex → www, http →
            # https, /services → /our-services etc.), so without an
            # explicit limited policy the sub-page fetch returns a 301
            # body and we treat the proxy as broken. Bound to 10 hops
            # to defeat redirect loops.
            client_kwargs: dict = {
                "proxies": [wreq.Proxy.all(proxy.as_url())],
                "timeout": timedelta(seconds=TIMEOUT_S),
                "redirect": wreq.redirect.Policy.limited(10),
            }
            if emulation is not None:
                client_kwargs["emulation"] = emulation

            client = wreq.Client(**client_kwargs)  # type: ignore[union-attr]
            # wreq exposes an async API — client.get() returns a coroutine.
            # Each call gets its own event loop (asyncio.run); cost is
            # negligible relative to the HTTP request itself (~1ms vs
            # ~1-3s on the wire).
            resp, body = asyncio.run(_async_get(client, url, max_bytes, headers=headers))
            status = getattr(resp, "status", None) or getattr(resp, "status_code", None)

            # 2xx → success. 3xx → with the limited redirect policy set
            # above, wreq follows up to 10 hops; if we still see a 3xx
            # here the chain hit the cap or pointed at a non-resolving
            # host.
            if status is None or 200 <= status < 300:
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
            elif 400 <= status < 500:
                # Target answered cleanly with "no" (404, 401, 410…) —
                # the page doesn't exist, or the URL was malformed. Not
                # a proxy problem. Don't bench, and don't retry through
                # other proxies — re-fetching the same URL through a
                # different IP won't conjure a missing page into being.
                # This matters because the extractor probes a handful of
                # candidate sub-paths (/about, /services, /what-we-do)
                # — most sites don't have all of them. Without this
                # branch each miss burns 3 proxies' health unfairly.
                logger.info("HTTP %s on %s (target answered no) — giving up",
                            status, host)
                return None
            elif 500 <= status < 600:
                # Target server problem, not our IP — don't bench.
                logger.info("HTTP %s on %s (target error)", status, host)
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


async def _async_get(client, url: str, max_bytes: int, headers: dict | None = None):
    """Await `client.get(url)` and the response body in one shot.

    wreq's Response.text() is also async in current versions but older
    or future builds might expose it as a sync attribute. We probe with
    `inspect.isawaitable` so this stays robust across the version
    range without a hard version pin.
    """
    resp = await (client.get(url, headers=headers) if headers else client.get(url))

    text_attr = getattr(resp, "text", None)
    if callable(text_attr):
        text_call = text_attr()
        text = await text_call if inspect.isawaitable(text_call) else text_call
    else:
        text = text_attr  # plain attribute

    if text is None:
        return resp, None
    if len(text) > max_bytes:
        return resp, text[:max_bytes]
    return resp, text


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


# ── LinkedIn path ──────────────────────────────────────────────────
# Cookie-based, single-account, pinned-proxy. Halts on 999 /
# login-redirect / "verify you're not a bot" — at the first sign of a
# challenge the account is parked in cooldown for 24h via
# linkedin_session.record_challenge().

_LINKEDIN_HOSTS = ("linkedin.com", "www.linkedin.com")
_LINKEDIN_TIMEOUT_S = 15
# Stricter per-host rate-limiting on LinkedIn — typical real-user
# profile-view interval at the API layer is well above this. Adds
# jitter on top in _fetch_linkedin so we don't look metronomic.
_LINKEDIN_MIN_INTERVAL_S = 30.0
_LINKEDIN_LAST_HIT_LOCK = threading.Lock()
_LINKEDIN_LAST_HIT: float = 0.0


def _is_linkedin_host(host: str) -> bool:
    h = (host or "").lower()
    return h in _LINKEDIN_HOSTS or h.endswith(".linkedin.com")


def _linkedin_enabled() -> bool:
    """Feature flag — set LINKEDIN_ENRICH_ENABLED=true in worker env
    to flip on. Until then every LinkedIn fetch short-circuits to None
    and linkedin.enrich() reports status='disabled'."""
    val = (os.environ.get("LINKEDIN_ENRICH_ENABLED") or "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _wait_for_linkedin_slot() -> None:
    """Single-token bucket spanning ALL LinkedIn traffic, plus jitter.
    Even with one account we never want to look like a metronome."""
    global _LINKEDIN_LAST_HIT
    with _LINKEDIN_LAST_HIT_LOCK:
        now = time.monotonic()
        wait = (_LINKEDIN_LAST_HIT + _LINKEDIN_MIN_INTERVAL_S) - now
        _LINKEDIN_LAST_HIT = now + max(wait, 0)
    jitter = random.uniform(0.0, 20.0)  # 0-20s on top of the floor
    total = max(wait, 0) + jitter
    if total > 0:
        time.sleep(total)


def _find_proxy_by_id(public_id: str) -> Proxy | None:
    """Find the proxy whose public_id() matches; None if not in the pool."""
    for p in get_pool().proxies:
        if p.public_id() == public_id:
            return p
    return None


_LINKEDIN_CHALLENGE_MARKERS = (
    "authwall",
    "checkpoint/challenge",
    "uas/login",
    "/login?",
    "please verify you're not a bot",
)


def _linkedin_looks_challenged(body: str) -> bool:
    head = body[:8000].lower()
    return any(m in head for m in _LINKEDIN_CHALLENGE_MARKERS)


def _fetch_linkedin(url: str, host: str, *, max_bytes: int) -> str | None:
    """LinkedIn-specific fetch path.

    1. Check the feature flag — if off, return None silently.
    2. Pick an eligible account from the jar (cap, cooldown, status).
    3. Resolve / pin a proxy to that account.
    4. Attach Cookie: li_at=... and request as a logged-in browser.
    5. Detect 999 / login-redirect → record_challenge + 24h cooldown.
    6. Other failures → record_failure (short cooldown after a streak).
    7. Success → record_success (bumps daily counter + last_used_at).
    """
    if not _linkedin_enabled():
        logger.debug("LinkedIn fetch attempted but LINKEDIN_ENRICH_ENABLED is off")
        return None
    if not _WREQ_AVAILABLE:
        return None

    acct = linkedin_session.pick_account()
    if acct is None:
        logger.info("LinkedIn fetch %s skipped — no eligible account in jar", host)
        return None

    # Resolve the pinned proxy. If unset (first request), pin to the
    # next healthy proxy and persist. If the previously-pinned proxy
    # is no longer in the pool, the account goes into a short cooldown
    # rather than rotating IP under an established cookie.
    pool = get_pool()
    proxy: Proxy | None = None
    if acct.pinned_proxy_id:
        proxy = _find_proxy_by_id(acct.pinned_proxy_id)
        if proxy is None:
            logger.error(
                "LinkedIn account '%s' pinned to %s but proxy is gone from pool — "
                "cooling down (manual repin required)",
                acct.label, acct.pinned_proxy_id,
            )
            linkedin_session.record_failure(
                acct.label, f"pinned proxy {acct.pinned_proxy_id} not in pool",
                hours=4,
            )
            return None
    else:
        proxy = pool.next_proxy()
        if proxy is None:
            return None
        linkedin_session.pin_proxy(acct.label, proxy.public_id())

    _wait_for_linkedin_slot()

    try:
        client_kwargs: dict = {
            "proxies": [wreq.Proxy.all(proxy.as_url())],
            "timeout": timedelta(seconds=_LINKEDIN_TIMEOUT_S),
            "redirect": wreq.redirect.Policy.limited(5),
        }
        # Force a Firefox-on-Windows emulation that matches the
        # registration-time fingerprint (Pixelscan: Firefox 146 / Win64).
        ff_profile = (
            getattr(wreq.Emulation, "Firefox149", None)
            or getattr(wreq.Emulation, "Firefox148", None)
        )
        if ff_profile is not None:
            client_kwargs["emulation"] = ff_profile

        client = wreq.Client(**client_kwargs)  # type: ignore[union-attr]
        headers = {
            "Cookie":          f"li_at={acct.li_at}",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.5",
            "DNT":             "1",
            "Sec-GPC":         "1",
            "Upgrade-Insecure-Requests": "1",
        }
        resp, body = asyncio.run(_async_get_with_headers(client, url, headers, max_bytes))
        status = getattr(resp, "status", None) or getattr(resp, "status_code", None)

        # LinkedIn's bot-block returns HTTP 999 — non-standard, but the
        # de-facto signal that anti-bot intercepted the request.
        if status == 999:
            linkedin_session.record_challenge(acct.label, f"HTTP 999 from {host}")
            return None
        # Auth-wall / login redirect → final URL contains /authwall
        # or /uas/login. wreq.redirect.Policy.limited follows the
        # redirect, so we end up with a 200 + a login HTML body. The
        # body sniffer covers that.
        if status and 200 <= status < 300:
            if body and _linkedin_looks_challenged(body):
                linkedin_session.record_challenge(
                    acct.label, f"challenge body served by {host}"
                )
                return None
            linkedin_session.record_success(acct.label)
            return body
        if status in (401, 403):
            linkedin_session.record_challenge(acct.label, f"HTTP {status} from {host}")
            return None
        if status == 429:
            linkedin_session.record_failure(
                acct.label, f"HTTP 429 from {host}", hours=2,
            )
            return None
        if status and 500 <= status < 600:
            # Server-side problem, not a session-trust signal — short cooldown
            linkedin_session.record_failure(
                acct.label, f"HTTP {status} from {host}", hours=0.25,
            )
            return None
        # 404 etc → URL miss, no account-level penalty
        logger.info("LinkedIn HTTP %s on %s (target answered no) — giving up", status, host)
        return None
    except Exception as e:
        logger.warning("LinkedIn fetch error %s via %s: %s: %s",
                       host, proxy.public_id() if proxy else "?",
                       type(e).__name__, str(e)[:200])
        linkedin_session.record_failure(
            acct.label, f"{type(e).__name__}: {str(e)[:120]}",
        )
        return None


async def _async_get_with_headers(client, url: str, headers: dict, max_bytes: int):
    """Same shape as _async_get but with explicit headers (cookies)."""
    resp = await client.get(url, headers=headers)
    text_attr = getattr(resp, "text", None)
    if callable(text_attr):
        text_call = text_attr()
        text = await text_call if inspect.isawaitable(text_call) else text_call
    else:
        text = text_attr
    if text is None:
        return resp, None
    if len(text) > max_bytes:
        return resp, text[:max_bytes]
    return resp, text
