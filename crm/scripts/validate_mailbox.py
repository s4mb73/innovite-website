#!/usr/bin/env python3
"""Validate a mailbox end-to-end before flipping OUTREACH_MODE=live.

Tests, per stage, with explicit failure surfacing:

  1. DB row loads cleanly with smtp_* + imap_* + smtp_pass_env_name
  2. Password env var present (length only — never printed)
  3. SMTP connect to smtp_host:smtp_port
  4. STARTTLS handshake
  5. SMTP login
  6. SMTP send-to-self (or --send-to <addr>) — real send, body marked TEST
  7. IMAP connect to imap_host:imap_port
  8. IMAP login
  9. IMAP fetch INBOX message count

Pass criteria: all 9 green. ANY failure → exit code 1, clear stage flag.

Usage:
  scripts/validate_mailbox.py --mailbox-id 15 --send-to sammybimpson@gmail.com

The recipient is REQUIRED to be explicit — never defaults to anything.
This is the safety gate that stops the script from accidentally sending
to leads. The recipient should be your own inbox so you can confirm the
test email arrived.
"""
from __future__ import annotations

import argparse
import imaplib
import os
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage

# Local import — run from crm/ root with PYTHONPATH set.
sys.path.insert(0, "/srv/innovite/innovite-website/crm")
import db


def _green(s): return f"\033[32m✓\033[0m {s}"
def _red(s):   return f"\033[31m✗\033[0m {s}"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mailbox-id", type=int, required=True,
                   help="crm.mailboxes.id of the mailbox to validate")
    p.add_argument("--send-to", required=True,
                   help="recipient address for the test send "
                        "(must be your own inbox — don't send to leads)")
    p.add_argument("--skip-send", action="store_true",
                   help="auth-only: skip the real test send. Use to verify "
                        "credentials without delivering anything.")
    args = p.parse_args()

    # Stage 1 — DB row
    row = db.fetch_one(
        """select id, address, from_name, smtp_host, smtp_port, smtp_user,
                  smtp_pass_env_name, imap_host, imap_port, paused, health_state
           from crm.mailboxes where id = %s""",
        (args.mailbox_id,)
    )
    if not row:
        print(_red(f"Stage 1: mailbox id {args.mailbox_id} not found"))
        return 1
    print(_green(f"Stage 1: row loaded — {row['address']}"))
    print(f"         smtp={row['smtp_host']}:{row['smtp_port']} user={row['smtp_user']}")
    print(f"         imap={row['imap_host']}:{row['imap_port']}")
    print(f"         paused={row['paused']} health={row['health_state']}")

    pass_env_name = row['smtp_pass_env_name']
    if not pass_env_name:
        print(_red("Stage 1: smtp_pass_env_name is NULL on this row"))
        return 1

    # Stage 2 — env var
    pwd = os.environ.get(pass_env_name)
    if not pwd:
        print(_red(f"Stage 2: env var {pass_env_name} is missing"))
        print(f"         set it via: scripts/seed_mailbox_password.sh {pass_env_name}")
        return 1
    print(_green(f"Stage 2: env var {pass_env_name} present (len={len(pwd)})"))

    # Stage 3+4+5 — SMTP connect / STARTTLS / login
    try:
        smtp = smtplib.SMTP(row['smtp_host'], row['smtp_port'], timeout=15)
    except Exception as e:
        print(_red(f"Stage 3: SMTP connect failed — {type(e).__name__}: {e}"))
        return 1
    print(_green(f"Stage 3: SMTP connected to {row['smtp_host']}:{row['smtp_port']}"))

    try:
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
    except Exception as e:
        print(_red(f"Stage 4: STARTTLS failed — {type(e).__name__}: {e}"))
        smtp.close()
        return 1
    print(_green("Stage 4: STARTTLS handshake OK"))

    try:
        smtp.login(row['smtp_user'], pwd)
    except smtplib.SMTPAuthenticationError as e:
        print(_red(f"Stage 5: SMTP auth REJECTED — {e.smtp_code} {e.smtp_error!r}"))
        print("         common causes: wrong password, app-password not generated,")
        print("                       Zoho 'IMAP/SMTP access' disabled on the mailbox")
        smtp.close()
        return 1
    except Exception as e:
        print(_red(f"Stage 5: SMTP login failed — {type(e).__name__}: {e}"))
        smtp.close()
        return 1
    print(_green(f"Stage 5: SMTP login OK as {row['smtp_user']}"))

    # Stage 6 — real test send
    if args.skip_send:
        print("Stage 6: SKIPPED (--skip-send)")
    else:
        msg = EmailMessage()
        msg["From"] = f"{row['from_name'] or row['address']} <{row['address']}>"
        msg["To"] = args.send_to
        ts = int(time.time())
        msg["Subject"] = f"[Innovite mailbox-validate] mailbox#{args.mailbox_id} {ts}"
        msg.set_content(
            f"This is an automated mailbox-validation send from "
            f"scripts/validate_mailbox.py at {time.strftime('%Y-%m-%d %H:%M:%S')}.\n\n"
            f"Mailbox id : {args.mailbox_id}\n"
            f"Address    : {row['address']}\n"
            f"Recipient  : {args.send_to}\n"
            f"SMTP host  : {row['smtp_host']}:{row['smtp_port']}\n\n"
            f"If you see this in your inbox, SMTP is fully working.\n"
            f"Now check IMAP detected the message in Stage 9 below.\n"
        )
        try:
            smtp.send_message(msg)
        except smtplib.SMTPRecipientsRefused as e:
            print(_red(f"Stage 6: recipient refused — {e.recipients}"))
            smtp.close()
            return 1
        except Exception as e:
            print(_red(f"Stage 6: send failed — {type(e).__name__}: {e}"))
            smtp.close()
            return 1
        print(_green(f"Stage 6: SMTP send OK — test message dispatched to {args.send_to}"))
    smtp.quit()

    # Stage 7+8+9 — IMAP connect / login / fetch
    try:
        imap = imaplib.IMAP4_SSL(row['imap_host'], row['imap_port'], timeout=15)
    except Exception as e:
        print(_red(f"Stage 7: IMAP connect failed — {type(e).__name__}: {e}"))
        return 1
    print(_green(f"Stage 7: IMAP connected to {row['imap_host']}:{row['imap_port']}"))

    try:
        # Zoho uses the same password for IMAP as SMTP. The engine
        # reuses smtp_pass_env_name for both — mirror that here.
        imap.login(row['smtp_user'], pwd)
    except imaplib.IMAP4.error as e:
        print(_red(f"Stage 8: IMAP auth REJECTED — {e}"))
        try: imap.logout()
        except Exception: pass
        return 1
    print(_green(f"Stage 8: IMAP login OK as {row['smtp_user']}"))

    try:
        typ, data = imap.select("INBOX", readonly=True)
        if typ != "OK":
            print(_red(f"Stage 9: SELECT INBOX failed — {typ} {data}"))
            imap.logout()
            return 1
        count = int(data[0]) if data and data[0] else 0
    except Exception as e:
        print(_red(f"Stage 9: INBOX fetch failed — {type(e).__name__}: {e}"))
        imap.logout()
        return 1
    print(_green(f"Stage 9: IMAP INBOX OK — {count} messages currently visible"))
    imap.logout()

    print()
    print(f"\033[32m=== Mailbox #{args.mailbox_id} ({row['address']}) — ALL STAGES PASS ===\033[0m")
    if not args.skip_send:
        print(f"Now check {args.send_to} for the test email "
              f"with subject containing 'mailbox#{args.mailbox_id}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
