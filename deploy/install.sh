#!/usr/bin/env bash
# Gribbosaurus Rex server install — idempotent, run as root from deploy/.
# Target: Debian/Ubuntu. See deploy/README.md.
set -euo pipefail

APP=/opt/gribbo/app
VENV=/opt/gribbo/venv
ENVFILE=/etc/gribbo/env
HERE="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
[ -f "$HERE/../requirements.txt" ] || { echo "run from the repo's deploy/ dir"; exit 1; }

echo "==> system user"
id gribbo >/dev/null 2>&1 || useradd --system --home /opt/gribbo --shell /usr/sbin/nologin gribbo

echo "==> system packages (python venv + shared eccodes)"
apt-get install -y --no-install-recommends python3-venv libeccodes0 >/dev/null

echo "==> directories"
mkdir -p /opt/gribbo "$APP/data" /etc/gribbo
# app dir is expected to be the git checkout already (see README)

echo "==> python venv + requirements"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$APP/requirements.txt"

echo "==> environment file"
if [ ! -f "$ENVFILE" ]; then
    cp "$HERE/env.example" "$ENVFILE"
    chown root:gribbo "$ENVFILE"
    chmod 640 "$ENVFILE"
    echo "    seeded $ENVFILE from env.example (add API keys there later)"
fi

echo "==> ownership"
chown -R gribbo:gribbo /opt/gribbo

echo "==> systemd units"
install -m 644 "$HERE/gribbo-api.service" \
               "$HERE/gribbo-arbiter.service" \
               "$HERE/gribbo-arbiter.timer" \
               "$HERE/gribbo-dashboard.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now gribbo-api.service gribbo-arbiter.timer gribbo-dashboard.service

echo "==> first arbiter pass (foreground, may take a few minutes)"
systemctl start gribbo-arbiter.service || true

echo
echo "Done. Checks:"
echo "  systemctl status gribbo-api gribbo-arbiter.timer gribbo-dashboard"
echo "  journalctl -u gribbo-arbiter -n 40"
echo "  curl -s localhost:8010/scores.json | head"
echo "Then wire Caddy: deploy/Caddyfile.snippet"
