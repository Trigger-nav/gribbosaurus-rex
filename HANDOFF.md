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

## Roadmap next steps (in rough order)

1. Live-verify Phase 2 (smoke script above), fix parser drift.
2. Turn on `observations.nmea.enabled` + test with `scripts/nmea_sim.py`.
3. Import a real Expedition log, backtest scores over a past race.
4. Phase 3 dashboard: forecast-vs-observed overlay per station/boat,
   skill-by-lead-time chart, run-arrival notifications.
5. Deployment: launchd/systemd service or small VPS (design is portable;
   `serve` is the single long-running process).
6. Phase 4: fit per-model corrections from the verification table
   (start linear: speed gain + direction rotation per model per lead
   bucket), apply at extraction, export corrected GRIBs.
