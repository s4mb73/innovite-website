#!/usr/bin/env bash
# Install a LinkedIn `li_at` session cookie into the account jar without
# the value landing in shell history, scroll-back, or this transcript.
#
# Usage:
#   ./scripts/seed_linkedin_account.sh <label>
#
# <label> is the account identifier we use in logs + DB — pick something
# short (e.g. "sammy-primary"). Re-running with the same label REPLACES
# the cookie on that account (use when the cookie expires and you've
# re-logged-in via the browser).
#
# The jar lives at /etc/innovite/linkedin_accounts.json, owned by
# deploy:deploy mode 640 — same security shape as crm-worker.env.
set -euo pipefail

JAR=/etc/innovite/linkedin_accounts.json

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 <account_label>" >&2
  echo "Example: $0 sammy-primary" >&2
  exit 2
fi
LABEL="$1"

# Defensive: label is used as a JSON string and a log key. Restrict to
# alphanumeric + underscore + hyphen so it can't smuggle JSON or shell
# special characters.
if ! [[ "$LABEL" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "ERROR: '$LABEL' is not a valid label (a-zA-Z0-9_- only)." >&2
  exit 2
fi

# Bootstrap the jar if missing — empty accounts list, version 1.
if [ ! -f "$JAR" ]; then
  echo "Bootstrapping $JAR (does not exist yet)."
  echo '{"version": 1, "accounts": []}' | sudo tee "$JAR" > /dev/null
  sudo chown deploy:deploy "$JAR"
  sudo chmod 640 "$JAR"
fi

echo "About to set li_at cookie for account '$LABEL' in $JAR on $(hostname)."
echo "Get the value from Firefox: F12 -> Storage -> Cookies ->"
echo "  https://www.linkedin.com -> copy the 'li_at' row value."
echo "It starts with 'AQE' and is typically 250-400 chars long."
echo
read -rsp "Paste li_at cookie (input hidden), then Enter: " li_at
echo
if [ -z "${li_at:-}" ]; then
  echo "ERROR: empty input — nothing captured. Aborting." >&2
  exit 1
fi
echo "Captured ${#li_at} chars."

# Sanity: li_at values are URL-safe-ish; reject obviously bad input
# without ever echoing the value itself.
if ! [[ "$li_at" =~ ^[A-Za-z0-9_.=/+-]+$ ]]; then
  echo "ERROR: cookie contains unexpected characters. Did you paste the"  >&2
  echo "       whole Cookie header by mistake? You want only the VALUE"   >&2
  echo "       of the li_at row, no 'li_at=' prefix and no quotes."       >&2
  unset li_at
  exit 1
fi

# Delegate the JSON merge to Python — atomic temp-file write, jq-free.
# Pipe the cookie via stdin (env var) so it never appears as an argv.
sudo -E env LI_AT="$li_at" LI_LABEL="$LABEL" LI_JAR="$JAR" \
  /usr/bin/env python3 - <<'PY'
import datetime, json, os, sys, tempfile

jar_path = os.environ["LI_JAR"]
label    = os.environ["LI_LABEL"]
li_at    = os.environ["LI_AT"]

with open(jar_path, "r", encoding="utf-8") as f:
    jar = json.load(f)

# Locate (or create) the account row by label.
accounts = jar.setdefault("accounts", [])
acct = next((a for a in accounts if a.get("label") == label), None)
created = False
if acct is None:
    acct = {
        "label": label,
        "daily_cap": 15,                 # conservative ramp start
        "daily_request_count": 0,
        "daily_count_date": None,
        "last_used_at": None,
        "cooldown_until": None,
        "cooldown_reason": None,
        "pinned_proxy_id": None,
        "total_lifetime_requests": 0,
        "total_lifetime_failures": 0,
        "status": "active",
        "notes": "",
    }
    accounts.append(acct)
    created = True

# Update the cookie + clear cooldown (new cookie => fresh session).
acct["li_at"]            = li_at
acct["cooldown_until"]   = None
acct["cooldown_reason"]  = None
acct["status"]           = "active"
acct["cookie_installed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

# Atomic write: temp file in same directory, then rename.
dirpath = os.path.dirname(jar_path)
fd, tmp = tempfile.mkstemp(dir=dirpath, prefix=".linkedin_accounts.", suffix=".tmp")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(jar, f, indent=2, sort_keys=True)
        f.write("\n")
    os.chmod(tmp, 0o640)
    os.replace(tmp, jar_path)
except Exception:
    try: os.unlink(tmp)
    except FileNotFoundError: pass
    raise

import pwd, grp
try:
    uid = pwd.getpwnam("deploy").pw_uid
    gid = grp.getgrnam("deploy").gr_gid
    os.chown(jar_path, uid, gid)
except (KeyError, PermissionError):
    pass

print(f"{'Created' if created else 'Updated'} account '{label}' in {jar_path}")
print(f"  cookie length: {len(li_at)} chars")
print(f"  daily_cap:     {acct['daily_cap']}")
print(f"  status:        {acct['status']}")
PY

unset li_at

echo
echo "Done. Next: validate with"
echo "  scripts/linkedin_health_check.py --account '$LABEL'"
