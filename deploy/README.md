# Deploying Gribbosaurus Rex (Hetzner cohost, per the Stingray contract)

Target: the Stingray Hetzner VM (Debian/Ubuntu) — but nothing here
assumes colocation; any small VPS works. Own system user, own systemd
units, own Caddy site block, shared system eccodes. Contract:
`docs/integration/gribbosaurus-contract.md` §Deployment.

## Architecture on the box

```
/opt/gribbo/app        git checkout (this repo)
/opt/gribbo/venv       python venv
/opt/gribbo/app/data   GRIBs + gribbo.sqlite + scores.json (pruned)
/etc/gribbo/env        secrets + env (API keys land here, NOT in git)

gribbo-api.service      uvicorn API on 127.0.0.1:8010 (serving /scores.json)
gribbo-arbiter.timer    every 10 min -> arbiter-once (fetch, verify, publish)
gribbo-dashboard.service  streamlit on 127.0.0.1:8511 (optional)
Caddy                   reverse-proxies both under your (sub)domains
```

The arbiter runs as a systemd **timer**, not inside the API process
(`GRIBBO_WATCH` stays unset in production): fetch crashes can't take the
API down, runs can't overlap (systemd serializes), and every pass gets a
journal entry.

## Install (as root)

```bash
apt-get update && apt-get install -y git python3-venv libeccodes0 caddy
git clone <your-remote> /opt/gribbo/app
cd /opt/gribbo/app/deploy
./install.sh
```

`install.sh` is idempotent: creates the `gribbo` user, venv, installs
requirements, seeds `/etc/gribbo/env` from `env.example` (first run
only), installs+enables the units, and starts everything. Check health:

```bash
systemctl status gribbo-api gribbo-arbiter.timer
journalctl -u gribbo-arbiter -n 50        # last arbiter pass
curl -s localhost:8010/scores.json | head
```

## Caddy

Append `deploy/Caddyfile.snippet` to `/etc/caddy/Caddyfile` (edit the
hostnames), then `systemctl reload caddy`. Caddy handles TLS
automatically. Stingray consumes `https://<your-host>/scores.json`
(ETag/Last-Modified supported).

## Updating

```bash
cd /opt/gribbo/app/deploy && ./update.sh
```

(git pull, reinstall requirements, restart units. The arbiter timer keeps
its schedule; one missed tick at most.)

## Notes

- Disk: GRIB retention is enforced (`keep_runs` per race config, pruning
  after every fetch). Current fleet uses ~3–6 GB steady-state. The
  contract's 80 GB shared disk is fine; `du -sh /opt/gribbo/app/data`
  to check.
- The dashboard service binds localhost only; expose it via Caddy or an
  SSH tunnel (`ssh -L 8511:localhost:8511 <host>`), your choice.
- Secrets: future model API keys (Météo-France, DataHub, Mistral) go in
  `/etc/gribbo/env`; units load it via `EnvironmentFile`.
- Migrating your Mac's verification history: stop local `serve`, copy
  `data/gribbo.sqlite` to `/opt/gribbo/app/data/`, restart the arbiter
  timer. GRIB files need not migrate (they refetch).
