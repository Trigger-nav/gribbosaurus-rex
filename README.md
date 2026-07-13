# 🦖 Gribbosaurus Rex

Multi-model marine weather system for offshore racing: automated GRIB
fetching, observation-based model confidence scoring, blended wind fields,
and (eventually) learned corrections that make GRIBs fit reality better.

## Status

| Phase | What | State |
|-------|------|-------|
| 1 | Real GRIB fetching + run detection (IFS, AIFS, GFS, ICON-EU) | ✅ live-verified |
| 2 | Observations (METAR/buoys/yacht NMEA) + confidence scoring | ✅ built, needs live verify |
| 3 | Front end: run freshness, confidence over time, forecast vs observed | partial |
| 4 | Learned GRIB transforms (bias/rotation/timing corrections) | planned |

See `HANDOFF.md` for architecture notes, gotchas and the roadmap.

## Data sources (all free, no keys)

- **ECMWF IFS** — 0.25° open data (`data.ecmwf.int`), byte-range subsetting via `.index`
- **ECMWF AIFS** — the ML model, same source (`aifs-single`)
- **NOAA GFS** — 0.25° via NOMADS grib filter (server-side bbox subset)
- **DWD ICON-EU** — 0.0625° regular lat-lon, Europe domain

Only 10m u/v wind + MSL pressure are downloaded, so runs are small
(a few MB per model per cycle for a race-sized area).

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

Everything is driven by a race config (`configs/*.yaml`) — bbox, models,
horizon. Default is `configs/balearics.yaml`; select with `--config` or
`GRIBBO_CONFIG`.

```bash
python -m gribbosaurus_rex fetch-once      # grab newest published runs
python -m gribbosaurus_rex status          # what's on disk
python -m gribbosaurus_rex point 39.5 2.6  # all-model forecast at a point
python -m gribbosaurus_rex verify-once     # fetch obs, verify, score models
python -m gribbosaurus_rex scores          # latest confidence per model
python -m gribbosaurus_rex import-log x.csv  # backtest an Expedition log
python -m gribbosaurus_rex watch           # poll runs+obs+verify forever
python -m gribbosaurus_rex serve           # API on :8000 (+ poller + NMEA)
streamlit run dashboard/app.py             # dashboard on :8501
```

Live yacht feed: set `observations.nmea.enabled: true` in the race config
and point Expedition's network output (NMEA 0183, UDP port 10110) at the
machine running `serve`. Test ashore with `python scripts/nmea_sim.py`.

## Layout

```
configs/            race/venue configs (bbox, models, horizon)
gribbosaurus_rex/
  config.py         config loader
  fetch/            per-model GRIB fetchers + probe-based run detection
  scheduler.py      polling: detect new cycles, fetch, prune
  store/runs.py     SQLite run metadata (feeds "new GRIB available")
  extract.py        GRIB -> xarray -> point/grid time series (kn, °, hPa)
  pipeline.py       confidence-weighted blended wind field
  core/wind.py      u/v vector maths, circular errors
  core/scoring.py   verification metrics (Phase 2 interface)
  api/main.py       FastAPI: /models/status /runs /point /grid /fetch
dashboard/app.py    Streamlit UI
data/               downloaded GRIBs + gribbo.sqlite (gitignored)
```

## Tests

```bash
python -m pytest tests/          # offline unit tests
python scripts/live_smoke.py     # hits real data servers (network needed)
```
