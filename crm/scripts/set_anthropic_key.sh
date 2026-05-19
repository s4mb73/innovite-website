#!/usr/bin/env bash
# One-shot helper: prompt for ANTHROPIC_API_KEY, write to the worker
# env file, restart the worker, and run the scraper smoke test. The
# key is read with `read -s` so it never echoes and never lands in
# shell history.
set -euo pipefail

ENV_FILE=/etc/innovite/crm-worker.env
TARGET_VAR=ANTHROPIC_API_KEY

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE does not exist on this host ($(hostname))." >&2
  echo "Are you on the right machine?" >&2
  exit 1
fi

echo "About to set $TARGET_VAR in $ENV_FILE on $(hostname)."
read -rsp "Paste key (input hidden), then Enter: " key
echo
if [ -z "${key:-}" ]; then
  echo "ERROR: empty input — nothing captured. Aborting." >&2
  exit 1
fi
if [ "${#key}" -lt 40 ]; then
  echo "ERROR: captured value is only ${#key} chars — that's not a full Anthropic key. Aborting." >&2
  unset key
  exit 1
fi
echo "Captured ${#key} chars."

# Remove any existing line for this var, then append the new one.
sudo sed -i "/^${TARGET_VAR}=/d" "$ENV_FILE"
printf '%s=%s\n' "$TARGET_VAR" "$key" | sudo tee -a "$ENV_FILE" > /dev/null
unset key

# Sanity-check the line landed.
if ! sudo grep -q "^${TARGET_VAR}=" "$ENV_FILE"; then
  echo "ERROR: write failed — ${TARGET_VAR} not present in $ENV_FILE." >&2
  exit 1
fi
echo "Wrote $TARGET_VAR to $ENV_FILE."

echo "Restarting crm-worker..."
sudo systemctl restart crm-worker
sudo systemctl is-active crm-worker

echo
echo "Running ROCA smoke test under the worker env..."
sudo -u deploy bash -c '
  set -a; source '"$ENV_FILE"'; set +a
  cd /srv/innovite/innovite-website/crm
  .venv/bin/python -c "
from scraper import extractor
import json
r = extractor.extract(\"https://rocaaccountants.co.uk/\")
print(\"STATUS:\", r[\"status\"])
print(\"PAGES :\", r[\"pages_fetched\"])
print(\"ERRS  :\", r[\"errors\"])
print(\"SIGNALS:\", json.dumps(r[\"signals\"], indent=2))
"
'
