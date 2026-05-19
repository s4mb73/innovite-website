#!/usr/bin/env bash
# Drop a Zoho (or any SMTP) mailbox password into the worker env file
# WITHOUT it landing in shell history, scroll-back, or the transcript.
#
# Usage:
#   ./scripts/seed_mailbox_password.sh MB_FOUNDER_INVGRP_PASS
#
# The argument is the env var name that crm.mailboxes.smtp_pass_env_name
# references — look up the mailbox row in the audit output to find it.
#
# The script is idempotent: re-running replaces an existing line for
# the same var. Restarts the worker so the new password takes effect.
set -euo pipefail

ENV_FILE=/etc/innovite/crm-worker.env

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 <ENV_VAR_NAME>" >&2
  echo "Example: $0 MB_FOUNDER_INVGRP_PASS" >&2
  exit 2
fi
TARGET_VAR="$1"

# Defensive: env var names are upper-case + underscore. Reject anything
# else so we can't be tricked into writing a shell-injection payload.
if ! [[ "$TARGET_VAR" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
  echo "ERROR: '$TARGET_VAR' is not a valid env var name (uppercase + underscore only)." >&2
  exit 2
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE does not exist on $(hostname). Wrong host?" >&2
  exit 1
fi

echo "About to set $TARGET_VAR in $ENV_FILE on $(hostname)."
read -rsp "Paste password (input hidden), then Enter: " pwd
echo
if [ -z "${pwd:-}" ]; then
  echo "ERROR: empty input — nothing captured. Aborting." >&2
  exit 1
fi
echo "Captured ${#pwd} chars."

# Remove any existing line for this var, then append the new one.
sudo sed -i "/^${TARGET_VAR}=/d" "$ENV_FILE"
printf '%s=%s\n' "$TARGET_VAR" "$pwd" | sudo tee -a "$ENV_FILE" > /dev/null
unset pwd

# Sanity-check the line landed.
if ! sudo grep -q "^${TARGET_VAR}=" "$ENV_FILE"; then
  echo "ERROR: write failed — ${TARGET_VAR} not present in $ENV_FILE." >&2
  exit 1
fi
sudo chmod 640 "$ENV_FILE"
echo "Wrote $TARGET_VAR to $ENV_FILE (mode 640)."

echo "Restarting crm-worker..."
sudo systemctl restart crm-worker
sudo systemctl is-active crm-worker
echo
echo "Done. Next: validate with"
echo "  scripts/validate_mailbox.py --mailbox-id <ID> --send-to <YOUR_INBOX>"
