#!/usr/bin/env bash
# Update the deployed Gribbosaurus Rex to the latest git revision.
set -euo pipefail

APP=/opt/gribbo/app
VENV=/opt/gribbo/venv

[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

echo "==> git pull"
sudo -u gribbo git -C "$APP" pull --ff-only

echo "==> requirements"
"$VENV/bin/pip" install --quiet -r "$APP/requirements.txt"

echo "==> reinstall units (in case they changed)"
install -m 644 "$APP"/deploy/gribbo-*.service "$APP"/deploy/gribbo-*.timer \
    /etc/systemd/system/
systemctl daemon-reload

echo "==> restart"
systemctl restart gribbo-api.service gribbo-dashboard.service
systemctl restart gribbo-arbiter.timer

echo "Done: $(sudo -u gribbo git -C "$APP" log -1 --oneline)"
