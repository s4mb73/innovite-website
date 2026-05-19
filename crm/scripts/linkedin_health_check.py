#!/usr/bin/env python3
"""LinkedIn account health check — one-shot diagnostic.

Run AFTER seed_linkedin_account.sh has installed a cookie. Performs a
single authenticated GET against a known stable /in/<slug> URL through
the account's pinned proxy and reports:

  - whether the proxy answered at all
  - HTTP status code
  - whether the body looks like a logged-in profile page or a login wall
  - JSON-LD Person presence (richer-data signature)
  - account state delta (counters bumped or cooldown set)

Does NOT touch the DB. Does NOT update any lead row. Safe to run
ad-hoc.

NOTE: this script BUMPS the account's daily_request_count and updates
last_used_at, since it routes through the real fetch path. That's
intentional — we want the health check to count like a real request.

Usage:
  python3 scripts/linkedin_health_check.py --account sammy-primary
  python3 scripts/linkedin_health_check.py --account sammy-primary --url https://www.linkedin.com/in/satyanadella
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

# Make `scraper.*` importable when invoked directly.
sys.path.insert(0, "/srv/innovite/innovite-website/crm")

# Force the feature flag ON for the duration of this script so we can
# health-check before flipping the worker-wide env var.
os.environ.setdefault("LINKEDIN_ENRICH_ENABLED", "true")

from scraper import client as scraper_client       # noqa: E402
from scraper import linkedin as scraper_linkedin   # noqa: E402
from scraper import linkedin_session                # noqa: E402

DEFAULT_PROBE_URL = "https://www.linkedin.com/in/williamhgates"

LOGGED_IN_MARKERS = (
    'name="ember-app"',
    "voyager-web",
    '<code id="bpr-guid-',
    "linkedin-bg-color-canvas",
)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--account", required=True, help="account label from the jar")
    p.add_argument("--url", default=DEFAULT_PROBE_URL,
                   help="profile URL to fetch (default: a stable public account)")
    args = p.parse_args()

    print(f"=== LinkedIn health check ===")
    print(f"  account: {args.account}")
    print(f"  url:     {args.url}")
    print()

    # Force a reload to pick up the most-recent jar from disk (e.g.
    # after running the seed script in another shell).
    n = linkedin_session.reload()
    print(f"jar reload: {n} account(s) present in jar")

    snap = linkedin_session.pick_account()
    if snap is None:
        print("ERROR: no eligible account in jar.")
        print("       Common causes: cookie missing, account in cooldown,")
        print("       daily cap hit, status != 'active'.")
        print()
        print(f"jar stats: {json.dumps(linkedin_session.stats(), indent=2)}")
        return 2
    if snap.label != args.account:
        print(f"WARN: picker chose '{snap.label}' but you asked for '{args.account}'.")
        print("      The picker is LRU-healthy across all eligible accounts;")
        print("      seed only the account you want active, or wait for the")
        print("      others to fall out of eligibility.")

    print(f"picked account:   {snap.label}")
    print(f"cookie length:    {len(snap.li_at)} chars")
    print(f"cookie age:       {snap.cookie_age_days} days")
    print(f"pinned proxy:     {snap.pinned_proxy_id or '(none yet — will pin on first request)'}")
    print(f"daily count:      {snap.daily_request_count} / {snap.daily_cap}")
    print()

    # Fire the fetch through the real client. This bumps counters via
    # linkedin_session as a side effect.
    print(f"fetching... ", end="", flush=True)
    t0 = datetime.utcnow()
    body = scraper_client.fetch(args.url, max_bytes=500_000)
    dt = (datetime.utcnow() - t0).total_seconds()
    print(f"done in {dt:.1f}s")
    print()

    # Inspect outcome.
    print(f"=== Result ===")
    print(f"body returned:    {'YES' if body else 'NO'}")
    if body:
        print(f"body length:      {len(body)} bytes")
        markers = [m for m in LOGGED_IN_MARKERS if m in body]
        print(f"logged-in markers: {markers if markers else '(none — likely served logged-out HTML)'}")
        if "JSON-LD" or "ld+json" in body:
            has_jsonld = '"@type":"Person"' in body or '"@type": "Person"' in body
            print(f"JSON-LD Person:   {'YES' if has_jsonld else 'no'}")
        # Parse via the production parser to confirm end-to-end.
        try:
            check_result = scraper_linkedin.check(args.url)
            print(f"check() status:   {check_result['status']}")
            print(f"check() name:     {check_result.get('name')}")
            print(f"check() title:    {check_result.get('current_title')}")
            print(f"check() employer: {check_result.get('current_company')}")
        except Exception as e:
            print(f"check() raised:   {type(e).__name__}: {e}")

    print()
    print(f"=== Jar after run ===")
    print(json.dumps(linkedin_session.stats(), indent=2))

    # Re-read the chosen account to surface counter / cooldown delta.
    linkedin_session.reload()
    again = linkedin_session.pick_account()
    if again and again.label == snap.label:
        print(f"account '{snap.label}' still eligible after run:")
        print(f"  daily_request_count: {again.daily_request_count} / {again.daily_cap}")
        print(f"  pinned_proxy:        {again.pinned_proxy_id}")
    else:
        # If the picker no longer returns it, the run pushed it into cooldown.
        print(f"account '{snap.label}' is NOT eligible after run — likely cooldown.")
        print("  Check /etc/innovite/linkedin_accounts.json for cooldown_until + cooldown_reason.")
        return 1

    if body and any(m in body for m in LOGGED_IN_MARKERS):
        print()
        print("PASS — looks like an authenticated session.")
        return 0
    print()
    print("FAIL — fetch returned nothing or the response is not authenticated HTML.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
