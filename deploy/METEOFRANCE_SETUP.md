# Météo-France API setup (AROME / ARPEGE)

Gribbosaurus can score the Météo-France high-res models, but they need a
free account on the Météo-France API portal. ~10 minutes, no cost.

## 1. Create the account

Go to <https://portail-api.meteofrance.fr/> and register (email
confirmation). The portal is in French; browser translate helps.

## 2. Subscribe to the model-package APIs (all free)

From the catalogue ("Découvrir les APIs" / Discover APIs), subscribe to
each of these — open the API, click **S'abonner / Subscribe**, pick the
free plan:

- **Paquets AROME** — AROME 2.5 km over France (english-channel, fastnet,
  central-med).
- **Paquets ARPEGE** — ARPEGE 0.1° Europe + 0.25° global (every race,
  including the only Météo-France reach to the Caribbean).
- **Paquets AROME Outre-mer** — AROME-Antilles 2.5 km (caribbean-600).

If you only sail one area for now, you can subscribe to fewer — the
scoring just skips models you're not subscribed to.

## 3. Copy your APPLICATION_ID (the permanent credential)

Go to **Mes applications / Mes APIs**. Your application has an
**APPLICATION_ID** — a long base64 string. This is permanent and is what
the server should use: Gribbosaurus exchanges it for short-lived tokens
automatically, so it keeps working unattended (a raw token would expire
within the hour and break the 10-minute arbiter).

> Prefer the permanent APPLICATION_ID over "Générer token". The generated
> token is handy for a one-off manual test, but it expires hourly.

## 4. Put it on the server

On the box, edit the secrets file (root-owned, chmod 640, never in git):

```
sudo nano /etc/gribbo/env
```

Add:

```
METEOFRANCE_APPLICATION_ID=<paste the base64 application id>
```

Save. (Optional extras are documented in `deploy/env.example`.)

## 5. Smoke-test before trusting it

Still on the server, as a quick check with the same credential:

```
sudo -u gribbo METEOFRANCE_APPLICATION_ID="$(sudo sed -n 's/^METEOFRANCE_APPLICATION_ID=//p' /etc/gribbo/env)" \
  /opt/gribbo/venv/bin/python /opt/gribbo/app/scripts/live_smoke_meteofrance.py
```

This confirms the token exchange, lists the catalogue, probes the package
URLs, and downloads + decodes one AROME package. **Paste the whole output
back to me** — a couple of URL tokens (the `productARO/ARP` suffix, the
exact `time=` range groupings, and the overseas AROME ids) are documented
best-guesses that I pin from this real output. The fetcher already skips a
rejected time-range rather than failing, so partial data flows even before
they're pinned.

## 6. Go live

Once the smoke output looks right:

```
sudo bash /opt/gribbo/app/deploy/update.sh
```

The next arbiter pass will start fetching AROME/ARPEGE, verifying them
against live obs, and publishing their confidence scores into
`scores.json` alongside the global models.

---

**Quick local test instead?** On your Mac, in the repo with the venv
active: `export METEOFRANCE_APPLICATION_ID=...` then
`python scripts/live_smoke_meteofrance.py`.
