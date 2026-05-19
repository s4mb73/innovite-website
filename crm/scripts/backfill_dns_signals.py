"""One-shot backfill: populate email_provider / website_host /
spf_present / dmarc_present for every existing lead that has a website
but no DNS signals yet.

Why a separate script: the pipeline runner only enriches NEW leads as
they're discovered. Existing leads from prior runs already sit in the
DB with the new columns null. Running them through the live pipeline
would be wasteful (and would re-run unrelated paid enrichers). This
script targets only the missing columns.

Usage:
  cd /srv/innovite/innovite-website/crm
  .venv/bin/python -m scripts.backfill_dns_signals          # dry-run, prints what would change
  .venv/bin/python -m scripts.backfill_dns_signals --apply  # actually write back

Idempotent — re-running is safe (skips rows already populated).
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

# Allow running as `python -m scripts.backfill_dns_signals` from crm root.
sys.path.insert(0, "/srv/innovite/innovite-website/crm")

import db
from scraper import dns_signals

logger = logging.getLogger("backfill_dns_signals")


SELECT_SQL = """
    select id, website
    from crm.leads
    where website is not null
      and website <> ''
      and (email_provider is null
           and website_host is null
           and dmarc_present is null
           and spf_present is null)
    order by id
"""

UPDATE_SQL = """
    update crm.leads
    set email_provider = %s,
        website_host   = %s,
        dmarc_present  = %s,
        spf_present    = %s
    where id = %s
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually write back; default is dry-run.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process at most N leads (0 = no limit).")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    rows = db.fetch_all(SELECT_SQL)
    if args.limit:
        rows = rows[: args.limit]
    total = len(rows)
    logger.info("backfill candidates: %d (%s)", total,
                "apply" if args.apply else "dry-run")

    stats = {"updated": 0, "no_signal": 0, "errored": 0}
    for i, row in enumerate(rows, 1):
        site = (row["website"] or "").strip()
        try:
            r = dns_signals.check(site)
        except Exception as e:
            logger.exception("DNS check failed for lead %s (%s)", row["id"], site)
            stats["errored"] += 1
            continue

        ep, wh, dm, sp = r["email_provider"], r["website_host"], r["dmarc_present"], r["spf_present"]

        # Skip rows where every signal is None — would be a no-op write.
        if ep is None and wh is None and dm is None and sp is None:
            stats["no_signal"] += 1
            if i % 25 == 0:
                logger.info("[%d/%d] processed (%d updated, %d no-signal)",
                            i, total, stats["updated"], stats["no_signal"])
            continue

        if args.apply:
            db.execute(UPDATE_SQL, (ep, wh, dm, sp, row["id"]))
        stats["updated"] += 1
        logger.info("[%d/%d] lead %s (%s) → provider=%s host=%s spf=%s dmarc=%s",
                    i, total, row["id"], site[:60], ep, wh, sp, dm)

        # Light pacing — public resolvers don't like a single client
        # hammering them. 50ms between leads keeps us well below any
        # rate-limit threshold while still completing a 1000-lead
        # backfill in under a minute.
        time.sleep(0.05)

    logger.info("done: %d updated, %d no-signal, %d errored (%s)",
                stats["updated"], stats["no_signal"], stats["errored"],
                "applied" if args.apply else "dry-run — re-run with --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
