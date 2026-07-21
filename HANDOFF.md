# 🦖 Gribbosaurus Rex — working notes / handoff

Context for anyone (human or Claude Code session) picking this up.

## What this is

Multi-model marine weather system for yacht racing. Fetches real GRIBs,
verifies every model against real observations (including the boat's own
instruments), converts skill into per-model confidence scores, blends a
probabilistic wind field with those earned weights, and will eventually
learn corrections that make GRIBs fit reality better (Phase 4).

Owner: Jack (boat: Stingray). Racing focus: Balearics / W Med
(configs/balearics.yaml), region is per-race configurable.

## State

- **Phase 1 (done, live-verified 2026-07-13):** GRIB fetching for
  ECMWF IFS + AIFS (open data, byte-range via `.index` sidecars — note
  the sidecar *replaces* `.grib2`, `…-fc.index`), GFS (NOMADS filter,
  server-side bbox subset), DWD ICON-EU (regular lat-lon bz2). Probe-based
  run detection, SQLite run store + pruning, extraction to points/grids
  via cfgrib/xarray, CLI, FastAPI, Streamlit dashboard.
- **Phase 2 (built, offline-tested; needs live verification):**
  observation store (obs/verification/scores tables in data/gribbo.sqlite),
  METAR bbox ingestion (aviationweather.gov JSON), NDBC buoys (optional),
  NMEA 0183 UDP/TCP listener (RMC/MWD/MDA/XDR/HDT/MWV, checksummed) for
  the live boat feed, Expedition CSV log importer (Excel-serial `Utc`
  supported), verification engine + rolling confidence scores, blend
  weights switch from priors to earned scores automatically.
- **Phase 3 (partial):** dashboard has run-freshness tiles, confidence
  chart, obs map. Wanted next: forecast-vs-observed overlay plots,
  per-lead-time skill curves, alerting on new runs.
- **Phase 4 (not started):** learned GRIB transforms — per-model bias
  corrections (speed scale, direction rotation, timing shift), condition-
  dependent (sea breeze vs gradient), exportable as corrected GRIBs for
  routing software. The `verification` table is deliberately rich
  (per-obs, per-run, per-lead errors) to feed this.

## Immediate next step

Run `python scripts/live_smoke_phase2.py` (needs runs on disk:
`python -m gribbosaurus_rex fetch-once`). Fix whatever the live METAR
API returns that the parser doesn't expect — the parsing in
`obs/sources.py:fetch_metar` was written blind against the documented
JSON format (`obsTime` epoch, `wspd` kn, `wdir` int|"VRB", `altim`/`slp`
hPa) and hasn't seen real payloads yet. Same caveat for NDBC.

## Verify/scoring design (the important bit)

- Every obs in the scoring window is compared against **every** complete
  run covering its time (not just the newest) → verification rows keyed
  (obs, model, cycle) with lead_hours. This gives lead-dependent skill for
  free (Phase 3 curves, Phase 4 training data).
- Primary error metric: **wind vector error** (kn) — penalizes speed and
  direction jointly. Direction-only and pressure errors stored too.
- Confidence = exp(-weighted_rmse_vector / err_scale_kn), weights =
  trust(source) × 0.5^(dist_nm/half_weight_nm) × 0.5^(lead_h/lead_half_h)
  × 0.5^(age_h/recency_half_h). Anchor = fresh yacht fix (< 6h) else
  config focus point else bbox centre. All knobs in config `scoring:`.
- `pipeline.current_weights()` returns earned weights when scores exist,
  else STATIC_WEIGHTS priors. Floor of 0.05 keeps no model at zero.

## Gotchas / conventions

- Wind: knots, direction = degrees FROM (meteorological), u/v in m/s
  inside GRIB-land; `extract.py` converts (MS_TO_KN). `core/wind.py`
  handles the circular maths — reuse it, don't reinvent.
- Times: ISO8601 UTC strings in SQLite; tz-naive UTC inside xarray.
- GFS longitudes are 0–360 in the GRIBs; extract normalizes to ±180.
- IFS 06z/18z runs (stream `scda`) only go to 90h — handled in fetcher.
- The old `ingest/` modules are deprecated shims (kept so stale imports
  fail loudly); `ingest/ecmwf.py` (Open-Meteo) is kept for future
  cross-checks. Real code paths: `fetch/`, `extract.py`, `obs/`, `verify.py`.
- NOMADS rate limit: fetcher sleeps 1.5s between step requests. Don't
  parallelize GFS fetching without keeping under ~50 req/min.
- NDBC ingestion re-fetches the station table per station per pass —
  fine for 1–2 buoys, cache it if the list grows.
- Yacht wind data is masthead-derived: upwash/heel corrections are
  deliberately NOT applied yet (Phase 4 territory; Expedition cal is
  assumed decent).
- Sandbox note: this repo was built from a Cowork cloud session with no
  outbound network; all live-touching code gets verified via
  scripts/live_smoke*.py on Jack's Mac. Claude Code sessions on the Mac
  don't have that restriction.

## Test map

- `tests/test_offline.py` — Phase 1: URL builders, cycle math, run store,
  wind maths (11 tests, no network).
- `tests/test_phase2.py` — obs store, NMEA parsing, Expedition import,
  haversine, confidence ordering + distance weighting (10 tests).
- `scripts/live_smoke.py` — Phase 1 end-to-end against real servers.
- `scripts/live_smoke_phase2.py` — Phase 2 end-to-end (METAR + verify +
  scores + NMEA UDP loopback).
- `scripts/nmea_sim.py` — fake yacht broadcasting NMEA over UDP for
  manual dashboard/listener testing (`python -m gribbosaurus_rex serve`
  with nmea.enabled: true, then run the sim).

## Stingray integration (added 2026-07-13 — READ FIRST)

Gribbosaurus is now the confirmed model-selection arbiter for the
Stingray planner. The contract is `docs/integration/gribbosaurus-contract.md`
(canonical copy lives in the Stingray repo; changes are negotiated and
versioned). Our adopted decisions — including answers to the contract's
three open questions, the SI-units migration, model-name mapping, the
`scores.json` publisher design, and the arbiter-vs-racing weighting
profiles — are in `docs/integration/DECISIONS.md`. Read both before any
architectural change. Headline constraints: the product is a versioned
`scores.json` per (model, region, lead bucket) served with ETag; data-only
coupling (no imports either way); Stingray's ingest conventions exactly
(SI internally, from-directions, ±180, UTC, land-never-calm); model ids
`ecmwf_ifs` / `nomads_gfs_ww3`; cohosted-but-separable systemd/Caddy
deployment; design headroom for blending, separate wave scores, and
vessel-telemetry obs — don't build those yet.

## Fleet mode (added 2026-07-13)

Multiple race areas run in one service: `config.load_fleet()` loads every
`enabled: true` config in `configs/` (currently balearics-summer, solent,
fastnet-2027 — the 2027 season plan). Races must share one `data_dir`.
Fetching uses `fetch_config(fleet)` — union bbox, union models, longest
horizon — so each model cycle is downloaded ONCE; extraction crops to the
race bbox (always pass `bbox=` to open_run/point_timeseries/value_at).
Obs/verify/scores run per race (scores table has a `race` column;
verification rows are shared and dedup by (obs, model, cycle) across
overlapping regions — Solent obs legitimately feed both solent and
fastnet-2027 scores). `scores.json` carries entries for every region;
Stingray picks by bbox. API: `/races` lists areas; `race=` param on
point/grid/scores endpoints; dashboard has a race selector in the sidebar.
Gotcha: ICON-EU's domain check runs against the UNION bbox — a future
race outside Europe (Caribbean 2028?) must drop `icon_eu` from ALL
configs' shared fetch or the icon fetch pass fails (per-model, others
unaffected). Migration: `scripts/migrate_to_si.py` also adds the
`scores.race` column (existing rows -> balearics-summer).

## GRIB export (added 2026-07-13)

`export.py` + `GET /grib/{model}?race=` + dashboard download buttons:
one multi-message GRIB2 per model's newest run, cropped to the race bbox
via eccodes (regular_ll grids; global 0..360 axes are roll-unwrapped;
anything unexpected passes through uncropped rather than risking a
corrupt file). Verification design note: obs are "pinged" against the
LOCAL area GRIBs (no per-obs network) — point-only fetching is
impossible (GRIB packs whole fields) and undesirable (contract: raw
files are first-class; transforms need fields). The eccodes re-encode
path needs a live check: `python scripts/live_smoke_export.py`, then
load an exported file into a router (Expedition) as the real acceptance
test.

## Windycator obs source (added 2026-07-14)

`obs/windycator.py` ingests weather.ianmillard.com/windycator — UK
coastal stations incl. Bramble Post/Calshot/Nab Tower (Southampton VTS),
Lymington/Hurst platforms, CCO piers, club anemometers. Enabled per race
(`observations.windycator: true` on solent + fastnet-2027; useless for
the Med). Trust 0.8. Feed schema + quirks documented in the module;
tests/fixtures/windycator_sample.json is a REAL captured payload — if
the feed format drifts, recapture from the browser and update it.
Courtesy note: it's Ian Millard's personal service — polling is one
bbox request per enabled race per poll cycle with an identifying UA;
consider dropping him a note if this becomes load-bearing for racing.

## Fleet roster (renamed/expanded 2026-07-14)

Races: central-med (ex balearics-summer), english-channel (ex solent,
bbox widened to the Channel racing area), fastnet (ex fastnet-2027),
middle-sea-race (Malta/Sicily), caribbean-600 (Antigua/Leewards —
**icon_eu excluded**: outside the ICON-EU domain). The old three config
files are disabled tombstones — `git rm configs/balearics.yaml
configs/solent.yaml configs/fastnet-2027.yaml` whenever convenient.
ICON's fetch domain check now only fails on ZERO overlap with Europe
(the fleet union bbox legitimately spans the Caribbean). Score history
under the old race names remains in the scores table untouched.
Consequence of the Caribbean joining the fleet: the GFS fetch domain is
now trans-Atlantic (still small — bbox-subset) and existing GFS runs
auto-refetch once via the bbox-coverage rule.

## Marine buoy obs via NDBC/GTS (added 2026-07-14)

NDBC's realtime2 feed relays the global GTS marine network. Live-confirmed
via radial search: UK lightvessels (62103 Channel, 62305 Greenwich, 62107
Seven Stones, 62304 Sandettie, 62170 F3) and W Channel buoys (62030,
62050) — now configured for english-channel + fastnet (Irish M3 62092 /
M5 62094 configured from documentation, verify with the finder). The
english-channel bbox widened to -5.2..2.1 to include them. Caribbean:
42060 in the C600 box. `scripts/find_ndbc_stations.py` automates
discovery: table -> bbox filter -> freshness probe -> paste-ready lists;
run it when adding a race. NDBC fetcher fixed 2026-07-14: station-table
LOCATION parsing was wrong (hemisphere token), and the table now
downloads once per pass.

**Med gap — CONFIRMED (finder returned zero for central-med and
middle-sea-race) — and scoped:**

- Puertos del Estado (Spain): the Portus web app's API is open, no auth
  (verified in-browser 2026-07-20). Base `https://portus.puertos.es/
  portussvr/api/`; `ubicaciones/` lists all stations (Balearics buoys
  incl.), `puntosMalla/hist/WIND|WAVE|CURRENTS` etc. exist. TODO for the
  integrator: click a buoy on portus.puertos.es with devtools open to
  capture the real-time-station-data endpoint shape, then write
  `obs/portus.py` mirroring `obs/windycator.py` (bbox filter from the
  ubicaciones list, SI conversion, trust ~0.9 — these are proper moored
  buoys, wave-capable for the Stingray wave scoring).
- Italy (middle-sea-race): ISPRA's RON wave buoys + mareographic
  network (dati.isprambiente.it) and Sicily's SIAS — unverified, scope
  the same way. Fallback covering the whole Med: Copernicus Marine
  INSITU TAC (free registration, NetCDF, heavier).

## Deployment kit (added 2026-07-20)

`deploy/` contains the full Hetzner-cohost kit per contract §Deployment:
`install.sh` (idempotent; gribbo user, venv, shared libeccodes, env
seed, units), `gribbo-api.service` (uvicorn :8010, API only),
`gribbo-arbiter.timer`+`.service` (arbiter-once every 10 min — fetching
runs OUTSIDE the API process in production; `GRIBBO_WATCH` stays unset),
`gribbo-dashboard.service` (streamlit :8511, localhost-bound),
`Caddyfile.snippet`, `env.example` (-> `/etc/gribbo/env`, where model
API keys will live), `update.sh`. Dashboard reads `GRIBBO_API_URL`.
See `deploy/README.md` incl. how to migrate the Mac's verification DB.
Not yet run on a real box — first install is the live test.

## High-res model roadmap (specced 2026-07-20 — the racing-grade tier)

Registry pattern means each is a fetcher module + registry entry +
PUBLISH_NAMES entry + race-config listing. Per-race `models:` lists
already handle domains. In priority order:

1. **AROME (Météo-France, 1.3km)** — open data via the Météo-France
   portal API, free key required (`METEOFRANCE_API_KEY` in env).
   Regular lat-lon GRIB2 packages -> existing extractor works as-is.
   Domain covers english-channel, most of fastnet, and central-med.
   Publish name proposal: `mf_arome`.
2. **ARPEGE (Météo-France)** — same key/API. 0.1° Europe + 0.25°
   global (global covers caribbean-600). `mf_arpege`.
3. **AROME-Antilles (2.5km)** — same key/API, overseas domain around
   the Leewards: THE high-res model for caribbean-600. `mf_arome_antilles`.
4. **ICON-2i (ItaliaMeteo/ARPAE, 2.2km Italy)** — via the Mistral/
   Meteo-Hub open platform (registration). middle-sea-race's high-res.
   Regular lat-lon. `im_icon_2i`.
5. **UKV (Met Office DataHub, 1.5km)** — free-tier key
   (`DATAHUB_API_KEY`). CAUTION: native Lambert azimuthal grid — the
   extractor needs a 2D-coordinate interpolation path (extract.py
   currently assumes 1D regular lat/lon). Budget real work. `ukmo_ukv`.
6. **LaMMA WRF (Tuscany)** — scope only if ICON-2i + AROME leave a
   Med gap; raw GRIB availability unconfirmed.

Model-count consequences to watch: verification cost per pass grows
linearly (fine), dashboard tiles wrap beyond ~6 models (switch to two
rows), and STATIC_WEIGHTS priors in pipeline.py need entries for new
models (or they get the 0.1 default until scores take over).

## Roadmap next steps (in rough order)

1. ~~Cleanup + guard~~ Done 2026-07-13: smoke loopback writes
   `source="test"` (excluded from verify/scoring) and purges after itself;
   `scripts/migrate_to_si.py` purges pre-guard `smoke-boat` rows.
2. ~~SI migration~~ Done 2026-07-13 (code side): internals are m/s;
   knots only at display/NMEA/METAR boundaries. **Run
   `python scripts/migrate_to_si.py` once on any pre-SI database**, then
   `verify-once` to rescore.
3. ~~Model-id mapping~~ Done: `publish.PUBLISH_NAMES` (`ifs → ecmwf_ifs`,
   `gfs → nomads_gfs_ww3`, ...); a test asserts every registry model has
   a publish name.
4. ~~`publish.py` + arbiter~~ Done: schema-1.0 `scores.json` per
   (model, region, lead bucket), arbiter weighting profile, atomic write,
   `GET /scores.json` with ETag/If-None-Match/Last-Modified,
   `arbiter-once` CLI, auto-published every poller pass.
   Not yet live-tested against a real accumulated verification DB.
5. Turn on `observations.nmea.enabled` + test with `scripts/nmea_sim.py`;
   import a real Expedition log, backtest a past race.
6. Wave scoring: ECMWF wave stream + NOMADS gfswave fetchers (reuse
   Stingray's verified endpoints/conventions), buoy wave obs (Puertos del
   Estado/EMODnet), separate wave scores (schema 1.1 proposal).
7. Phase 3 dashboard: forecast-vs-observed overlays, skill-by-lead-time,
   run-arrival notifications.
8. Deployment to the Hetzner cohost per contract §Deployment (`deploy/`
   systemd + Caddy + cron; own `gribbo` user; shared eccodes).
9. Phase 4: per-model corrections (company-umbrella IP per contract) —
   fit from the verification table, apply at extraction, export
   corrected GRIBs.
