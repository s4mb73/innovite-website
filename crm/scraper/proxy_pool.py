"""Proxy pool — load + rotate UK ISP proxies for the scraper.

Source file: /etc/innovite/proxies.list (chmod 600, owner deploy).
Override path via PROXIES_LIST_PATH env var for local dev.

File format: one proxy per line, `IP:PORT:USERNAME:PASSWORD`. Lines
starting with `#` or empty are ignored. The username and password are
the same across the whole pool in practice (single provider account),
but the parser treats each line independently so per-IP rotation
remains possible if we later add multi-account support.

Rotation strategy: round-robin per request (see ROTATION at the
bottom for the rationale vs sticky-per-domain). The pool is
process-local; we run one worker so no cross-process state needed.

Health tracking
---------------
A proxy that fails 3 consecutive requests gets benched for 15 minutes.
When picking, benched proxies are skipped; if all are benched, the
oldest-benched one is rehabilitated and used. This avoids the "all
proxies are dead" deadlock when a target is briefly down across the
board.

Reload
------
The file is reread on SIGHUP (signal 1). Operator can add or remove
proxies without restarting crm-worker.service. Implementation lives
in worker.py — this module just exposes a reload() method.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("crm.scraper.proxy_pool")

DEFAULT_PROXY_LIST_PATH = "/etc/innovite/proxies.list"
BENCH_AFTER_CONSECUTIVE_FAILURES = 3
BENCH_DURATION_SECONDS = 15 * 60


@dataclass
class Proxy:
    """One proxy endpoint."""
    host: str
    port: int
    username: str
    password: str

    # Health state, mutated by client.py via record_success / record_failure.
    consecutive_failures: int = 0
    benched_until: datetime | None = None
    total_requests: int = 0
    total_failures: int = 0

    def as_url(self) -> str:
        """Format wreq + most HTTP clients accept: http://user:pass@host:port."""
        return f"http://{self.username}:{self.password}@{self.host}:{self.port}"

    def public_id(self) -> str:
        """IP:port — safe for logs (no credentials)."""
        return f"{self.host}:{self.port}"

    def is_benched(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return self.benched_until is not None and self.benched_until > now


@dataclass
class ProxyPool:
    proxies: list[Proxy] = field(default_factory=list)
    _index: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _path: str = DEFAULT_PROXY_LIST_PATH

    @classmethod
    def from_file(cls, path: str | None = None) -> "ProxyPool":
        """Build a pool by reading the proxy list file.

        Returns an empty pool (which makes the scraper a no-op via the
        `disabled` status) if the file is missing or unreadable. Logs
        a warning but never raises — the rest of the pipeline must keep
        running.
        """
        path = path or os.environ.get("PROXIES_LIST_PATH") or DEFAULT_PROXY_LIST_PATH
        proxies: list[Proxy] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split(":")
                    if len(parts) != 4:
                        logger.warning("Skipping malformed proxy line: %s", line[:32])
                        continue
                    host, port_str, user, pwd = parts
                    try:
                        port = int(port_str)
                    except ValueError:
                        logger.warning("Bad port in proxy line: %s", line[:32])
                        continue
                    proxies.append(Proxy(host=host, port=port,
                                          username=user, password=pwd))
        except FileNotFoundError:
            logger.warning(
                "Proxy list not found at %s — scraper will run in disabled mode",
                path,
            )
        except Exception:
            logger.exception("Failed to load proxy list from %s", path)

        pool = cls(proxies=proxies, _path=path)
        logger.info("ProxyPool loaded: %d proxies from %s", len(proxies), path)
        return pool

    def reload(self) -> int:
        """Re-read the file in place. Returns the new proxy count.

        Health state is reset (consecutive_failures etc.) because the
        operator is most likely refreshing the file because something
        in the old state was bad."""
        new_pool = ProxyPool.from_file(self._path)
        with self._lock:
            self.proxies = new_pool.proxies
            self._index = 0
        return len(self.proxies)

    def __len__(self) -> int:
        return len(self.proxies)

    def is_enabled(self) -> bool:
        return len(self.proxies) > 0

    def next_proxy(self) -> Proxy | None:
        """Round-robin next healthy proxy.

        Skips benched proxies. If every proxy is benched, rehabilitates
        the one whose bench expires soonest — better to retry with a
        recently-bad proxy than to fail the request entirely.
        Returns None only if the pool is empty.
        """
        with self._lock:
            n = len(self.proxies)
            if n == 0:
                return None

            now = datetime.now(timezone.utc)
            for _ in range(n):
                self._index = (self._index + 1) % n
                p = self.proxies[self._index]
                if not p.is_benched(now):
                    return p

            # All benched — rehabilitate the one with the earliest bench-out time.
            soonest = min(self.proxies, key=lambda p: p.benched_until or now)
            soonest.benched_until = None
            soonest.consecutive_failures = 0
            logger.warning("All proxies benched — rehabilitating %s early",
                           soonest.public_id())
            return soonest

    def record_success(self, proxy: Proxy) -> None:
        with self._lock:
            proxy.total_requests += 1
            proxy.consecutive_failures = 0

    def record_failure(self, proxy: Proxy, reason: str = "") -> None:
        with self._lock:
            proxy.total_requests += 1
            proxy.total_failures += 1
            proxy.consecutive_failures += 1
            if proxy.consecutive_failures >= BENCH_AFTER_CONSECUTIVE_FAILURES:
                proxy.benched_until = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=BENCH_DURATION_SECONDS)
                )
                logger.warning(
                    "BENCHED %s for %ds after %d consecutive failures (last: %s)",
                    proxy.public_id(), BENCH_DURATION_SECONDS,
                    proxy.consecutive_failures, reason[:80],
                )

    def stats(self) -> dict:
        """Snapshot of pool health — used by the worker heartbeat / log."""
        with self._lock:
            now = datetime.now(timezone.utc)
            return {
                "total":      len(self.proxies),
                "healthy":    sum(1 for p in self.proxies if not p.is_benched(now)),
                "benched":    sum(1 for p in self.proxies if p.is_benched(now)),
                "lifetime_requests": sum(p.total_requests for p in self.proxies),
                "lifetime_failures": sum(p.total_failures for p in self.proxies),
            }


# Module-level singleton — instantiated lazily so import-order is irrelevant.
_pool: ProxyPool | None = None
_pool_lock = threading.Lock()


def get_pool() -> ProxyPool:
    """Return the process-wide singleton. Loads from disk on first call."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ProxyPool.from_file()
    return _pool


def reload_pool() -> int:
    """Force-reload the singleton from disk. Returns the new count.
    Wired to SIGHUP in worker.py."""
    pool = get_pool()
    return pool.reload()


# ── ROTATION rationale ──────────────────────────────────────────────
# We use round-robin per request (not sticky-per-domain) because:
#   1. Each request is one-shot — homepage / about / services pages
#      don't carry session state, so no need for IP affinity.
#   2. With 50 proxies and ~3 pages per lead, sticky-per-domain would
#      route every request for one lead through one IP — high concentration.
#      Round-robin spreads across the pool so no single IP exceeds ~2%
#      of total traffic to any one target site.
#   3. Per-target rate limiting still happens at the client layer (token
#      bucket per host), so a single target isn't hammered regardless
#      of IP distribution.
