"""LinkedIn account jar — loader, picker, accounting.

Owns /etc/innovite/linkedin_accounts.json. The scraper client.py asks
this module for an account on each LinkedIn-bound request, reports the
outcome, and never touches the file directly.

Why a separate module
---------------------
LinkedIn's anti-bot regime is fundamentally different from the rest of
the scraper:
  - one *account* (not one *IP*) is the unit of trust
  - the session cookie must be presented every request
  - the cookie + IP pairing must stay stable for the cookie's lifetime
    (rotating IPs on a single account is the strongest kill signal)
  - per-account daily caps + cooldown windows are required to avoid
    burning the account on day 1
None of that maps onto proxy_pool's stateless round-robin model. Hence
this module — same shape (singleton + lock + record_success/failure)
but the unit is `Account`, not `Proxy`.

File format
-----------
JSON, version-tagged, atomic-write. See seed_linkedin_account.sh for
the bootstrap shape. Per-account fields:
  label, li_at, daily_cap, daily_request_count, daily_count_date,
  last_used_at, cooldown_until, cooldown_reason, pinned_proxy_id,
  total_lifetime_requests, total_lifetime_failures, status, notes
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger("crm.scraper.linkedin_session")

DEFAULT_JAR_PATH = "/etc/innovite/linkedin_accounts.json"

# Operational tunables — exposed at module level for tests.
DEFAULT_DAILY_CAP = 15           # starting cap; raise once warmup proven
COOLDOWN_ON_CHALLENGE_HOURS = 24   # 999 / login-redirect → 24h pause
COOLDOWN_ON_HARD_FAIL_HOURS = 1    # connection error → short cooloff

_jar_lock = threading.Lock()
_jar_cache: dict | None = None
_jar_path = os.environ.get("LINKEDIN_JAR_PATH") or DEFAULT_JAR_PATH


def _load_locked() -> dict:
    """Read the jar from disk. Caller must hold _jar_lock."""
    global _jar_cache
    if _jar_cache is not None:
        return _jar_cache
    try:
        with open(_jar_path, "r", encoding="utf-8") as f:
            _jar_cache = json.load(f)
    except FileNotFoundError:
        logger.warning("LinkedIn jar not found at %s — no accounts available", _jar_path)
        _jar_cache = {"version": 1, "accounts": []}
    except Exception:
        logger.exception("Failed to load LinkedIn jar from %s", _jar_path)
        _jar_cache = {"version": 1, "accounts": []}
    return _jar_cache


def _save_locked(jar: dict) -> None:
    """Atomic write of the jar to disk. Caller must hold _jar_lock."""
    dirpath = os.path.dirname(_jar_path) or "."
    fd, tmp = tempfile.mkstemp(dir=dirpath, prefix=".linkedin_accounts.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(jar, f, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o640)
        os.replace(tmp, _jar_path)
    except Exception:
        try: os.unlink(tmp)
        except FileNotFoundError: pass
        raise


def reload() -> int:
    """Force-reread the jar from disk (e.g. after seed script).
    Returns the number of accounts loaded."""
    global _jar_cache
    with _jar_lock:
        _jar_cache = None
        jar = _load_locked()
    return len(jar.get("accounts", []))


# ── State helpers ──────────────────────────────────────────────────

def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _today_utc() -> str:
    return _now_utc().date().isoformat()


def _reset_daily_if_new_day(acct: dict) -> None:
    """Zero the daily_request_count when today's UTC date differs from
    the stored daily_count_date. Mutates in place."""
    today = _today_utc()
    if acct.get("daily_count_date") != today:
        acct["daily_count_date"] = today
        acct["daily_request_count"] = 0


def _is_eligible(acct: dict, now: datetime) -> tuple[bool, str]:
    """True iff the account can take a fresh request right now."""
    if acct.get("status") != "active":
        return False, f"status={acct.get('status')}"
    if not acct.get("li_at"):
        return False, "no cookie installed"
    cooldown = _parse_iso(acct.get("cooldown_until"))
    if cooldown and cooldown > now:
        return False, f"cooldown until {cooldown.isoformat()}"
    cap = int(acct.get("daily_cap") or DEFAULT_DAILY_CAP)
    if acct.get("daily_count_date") == _today_utc():
        if int(acct.get("daily_request_count") or 0) >= cap:
            return False, f"daily cap reached ({cap})"
    return True, ""


# ── Public API ─────────────────────────────────────────────────────

@dataclass
class AccountSnapshot:
    """Immutable view of an account, returned by pick_account().

    The caller uses .label to refer back to the account when calling
    record_success / record_failure — the snapshot itself is not
    write-back-able, which keeps the file the single source of truth."""
    label: str
    li_at: str
    pinned_proxy_id: str | None
    daily_cap: int
    daily_request_count: int
    cookie_age_days: int | None = None


def pick_account() -> AccountSnapshot | None:
    """Choose the LRU-healthy account from the jar.

    Selection rules:
      1. status == "active"
      2. cookie installed
      3. not in cooldown
      4. under daily cap
      → among the survivors, pick the one with the oldest last_used_at
        (ties broken by lowest daily_request_count, then label).

    Returns None when no eligible account exists. Caller treats that
    as "scraper disabled for LinkedIn" — same as wreq missing.
    """
    with _jar_lock:
        jar = _load_locked()
        now = _now_utc()

        eligible: list[dict] = []
        for acct in jar.get("accounts", []):
            _reset_daily_if_new_day(acct)
            ok, why = _is_eligible(acct, now)
            if ok:
                eligible.append(acct)
            else:
                logger.debug("LinkedIn account '%s' not eligible: %s",
                             acct.get("label"), why)

        if not eligible:
            return None

        # Persist any daily-reset mutations even if we return nothing
        # selected — otherwise the same accounts keep resetting on each
        # call. (Safe: _save_locked runs under the same lock.)
        _save_locked(jar)

        def sort_key(a: dict) -> tuple:
            last = _parse_iso(a.get("last_used_at"))
            # Older last_used_at → comes first; None means never used.
            return (
                last or datetime.min.replace(tzinfo=timezone.utc),
                int(a.get("daily_request_count") or 0),
                a.get("label") or "",
            )

        chosen = sorted(eligible, key=sort_key)[0]
        installed = _parse_iso(chosen.get("cookie_installed_at"))
        age_days = None
        if installed is not None:
            age_days = max(0, (now - installed).days)
        return AccountSnapshot(
            label=chosen["label"],
            li_at=chosen["li_at"],
            pinned_proxy_id=chosen.get("pinned_proxy_id"),
            daily_cap=int(chosen.get("daily_cap") or DEFAULT_DAILY_CAP),
            daily_request_count=int(chosen.get("daily_request_count") or 0),
            cookie_age_days=age_days,
        )


def _mutate(label: str, fn) -> None:
    """Apply fn(acct) to the named account and persist."""
    with _jar_lock:
        jar = _load_locked()
        for acct in jar.get("accounts", []):
            if acct.get("label") == label:
                fn(acct)
                _save_locked(jar)
                return
        logger.warning("LinkedIn account '%s' not in jar — mutation skipped", label)


def pin_proxy(label: str, proxy_public_id: str) -> None:
    """Tie an account to a specific proxy IP:port. First-write-wins —
    once an account has a pin, it doesn't shift even if the proxy is
    later unhealthy (that becomes a cooldown signal, not a rotation
    signal — IP changes mid-cookie-life are how LinkedIn kills you)."""
    def fn(a: dict) -> None:
        if not a.get("pinned_proxy_id"):
            a["pinned_proxy_id"] = proxy_public_id
            logger.info("PINNED LinkedIn account '%s' → %s", label, proxy_public_id)
    _mutate(label, fn)


def record_success(label: str) -> None:
    def fn(a: dict) -> None:
        _reset_daily_if_new_day(a)
        a["daily_request_count"] = int(a.get("daily_request_count") or 0) + 1
        a["total_lifetime_requests"] = int(a.get("total_lifetime_requests") or 0) + 1
        a["last_used_at"] = _now_utc().isoformat()
    _mutate(label, fn)


def record_failure(label: str, reason: str, hours: float | None = None) -> None:
    """A non-challenge failure (connect error, timeout). Counts against
    lifetime stats and triggers a short cooldown after 3-in-a-row."""
    def fn(a: dict) -> None:
        _reset_daily_if_new_day(a)
        a["total_lifetime_requests"]  = int(a.get("total_lifetime_requests") or 0) + 1
        a["total_lifetime_failures"]  = int(a.get("total_lifetime_failures") or 0) + 1
        a["last_used_at"] = _now_utc().isoformat()
        h = hours if hours is not None else COOLDOWN_ON_HARD_FAIL_HOURS
        a["cooldown_until"]  = (_now_utc() + timedelta(hours=h)).isoformat()
        a["cooldown_reason"] = reason[:200]
        logger.warning("LinkedIn account '%s' cooldown %.1fh after: %s",
                       a.get("label"), h, reason[:120])
    _mutate(label, fn)


def record_challenge(label: str, reason: str) -> None:
    """A challenge response (999, login redirect, 'verify you're not a
    bot'). The account is paused for COOLDOWN_ON_CHALLENGE_HOURS and
    flagged so the operator notices on the next health check."""
    def fn(a: dict) -> None:
        _reset_daily_if_new_day(a)
        a["total_lifetime_requests"]  = int(a.get("total_lifetime_requests") or 0) + 1
        a["total_lifetime_failures"]  = int(a.get("total_lifetime_failures") or 0) + 1
        a["last_used_at"] = _now_utc().isoformat()
        a["cooldown_until"]  = (_now_utc() + timedelta(hours=COOLDOWN_ON_CHALLENGE_HOURS)).isoformat()
        a["cooldown_reason"] = f"challenge: {reason[:200]}"
        a["status"]          = "cooldown"
        logger.error("LinkedIn account '%s' CHALLENGED — %dh cooldown — reason: %s",
                     a.get("label"), COOLDOWN_ON_CHALLENGE_HOURS, reason[:120])
    _mutate(label, fn)


def stats() -> dict[str, Any]:
    """Snapshot of jar health — used by worker heartbeat / health-check."""
    with _jar_lock:
        jar = _load_locked()
        now = _now_utc()
        accounts = jar.get("accounts", [])
        active = 0
        on_cooldown = 0
        at_cap = 0
        for a in accounts:
            ok, why = _is_eligible(a, now)
            if ok:
                active += 1
            elif "cooldown" in why:
                on_cooldown += 1
            elif "daily cap" in why:
                at_cap += 1
        return {
            "total":      len(accounts),
            "eligible":   active,
            "cooldown":   on_cooldown,
            "at_cap":     at_cap,
            "jar_path":   _jar_path,
        }
