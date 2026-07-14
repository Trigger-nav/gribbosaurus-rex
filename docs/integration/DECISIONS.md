# Integration decisions — Gribbosaurus side (response to contract v0.1)

Status: adopted 2026-07-13. Each decision below shapes architecture from
now on. Where the contract requires negotiation, the proposal is marked
**(proposed to Stingray)**.

## Answers to the contract's open questions

### (a) Observation sources and per-source trust

One scoring engine, pluggable sources, all writing to the same obs store
(`obs/store.py`). Trust is a config value (`trust:` in the race config);
defaults:

| source      | trust | variables            | status |
|-------------|-------|----------------------|--------|
| `yacht` / vessel telemetry | 1.00 | wind, pressure (waves n/a) | built — NMEA 0183 UDP/TCP listener + Expedition log importer. The future Stingray fleet/pilot-vessel feed enters here: it's the same ObsStore interface, one new writer. |
| `buoy` (moored: NDBC, Puertos del Estado, EMODnet) | 0.90 | wind, pressure, **waves (hs, period, dir)** | NDBC built (untested live — no Med stations); Spanish/EMODnet buoys are the priority wave-obs source, to build with wave scoring |
| `metar` (coastal airports) | 0.85 | wind, pressure | built, live-verified. Land-sited: usable for wind/pressure, never for waves; coastal-effect caveat priced into trust |
| `windycator` (UK aggregator, weather.ianmillard.com) | 0.80 | wind, some pressure | built 2026-07-14. Aggregates Southampton VTS portlog (Bramble/Calshot/Nab), weatherfile platforms (Lymington/Hurst), CCO piers, NCI + club stations — the obs UK sailors actually use. Mixed provenance hence 0.8; polite polling (identified UA, fleet cadence). Feed: `latest.php?coords=bbox`, knots -> m/s at boundary, local-tz timestamps -> UTC, stale/sentinel stations filtered |
| `ship` (VOS/synop reports) | 0.70 | wind, pressure, waves | future, by agreement |
| `openmeteo` (served API) | 0.40 | wind, pressure | built, **disabled by default** — model-derived, not ground truth; excluded from published scores entirely (circularity) |

Weighting of each verification sample:
`w = trust × 0.5^(distance_nm / half_weight_nm) × 0.5^(lead_h / lead_half_h) × 0.5^(age_h / recency_half_h)`

Two weighting profiles (same engine, different anchor):

- **Arbiter profile** (publishes `scores.json`): no distance anchor —
  `half_weight_nm = ∞`; every obs in the region bbox counts equally by
  location. A region score must represent the region, not proximity to
  any vessel.
- **Racing profile** (personal tool): anchor = freshest yacht fix
  (< 6h) else configured focus point; `half_weight_nm = 30` default.

### (b) Scoring window vs responsiveness

Published scores use a **24h obs window** (`obs_window_h: 24`, as in the
contract example) with a 12h recency half-life inside it, evaluated
separately per lead bucket. Rationale: at 4 cycles/day/model, 24h gives
~4 runs' worth of comparisons per bucket — enough to be stable, short
enough to track regime changes (sea-breeze days vs gradient days) within
a day. The racing profile keeps its longer 48h window (config
`scoring.window_h`) since a human reads the trend chart, not a single
number. Both are config values; the window used is always published as
`obs_window_h` so Stingray can judge.

### (c) Separate wave and wind scores — yes

Wind and wave skill are scored and published **separately**; no blended
"overall" number (Stingray routes wave-led; a good-wind/bad-wave model
must be visible as exactly that). **(proposed to Stingray)**: score
entries gain `"variable": "wind" | "wave"` in schema `1.1`; until agreed,
we publish wind-only entries with `metrics: {wind_rmse_ms, dir_mae_deg}`
and omit wave fields rather than inventing them ("no invented numbers").
Wave metrics when built: `hs_rmse_m` primary, `dir_mae_deg`,
`period_mae_s` secondary. Wave obs come from moored buoys (see (a));
wave forecasts from ECMWF wave stream (`swh`/`mwd`/`pp1d`/`mwp` — same
`.index` Range technique, stream `wave`) and NOMADS `gfswave`
(`filter_gfswave.pl`, `HTSGW`/`PERPW`/`DIRPW`, global.0p16), reusing
Stingray's verified approaches including
`WW3_DIRECTION_IS_TO_CONVENTION = False` (ticket 0.5, 2026-07-07).

## Convention adoption (contract §Convention alignment)

- **SI internally — adopted, requires migration.** Current internals are
  knots (racing heritage). Decision: obs store, verification errors and
  score metrics move to m/s (`wind_speed_ms`, `err_vector_ms`,
  `wind_rmse_ms`); knots appear only in dashboard/CLI display and NMEA
  parsing boundaries. One-time SQLite migration script converts existing
  rows (`scripts/migrate_to_si.py`, to write). Until the migration lands,
  nothing is published to Stingray.
- Directions "coming from", degrees true: **already compliant** (core
  convention since Phase 1; `core/wind.py`).
- Longitudes −180…180: **already compliant** (normalized at extraction).
- UTC everywhere: **already compliant** (ISO8601 UTC in stores).
- Land = missing, never calm: adopted. Both sides mask **wave** fields
  only (wind fields carry valid values over land; METAR verification
  relies on that). Wave scoring will need a land checker — reuse
  Stingray's GSHHG approach or subset to buoy positions (buoys are never
  on land), decision deferred to the wave build.
- Model identifiers: registry keys stay internal; **published names map
  to Stingray's source names**: `ifs → ecmwf_ifs`, and when the combined
  wind+wave GFS product exists, `gfs/ww3 → nomads_gfs_ww3`. Additional
  internal models publish as `ecmwf_aifs`, `dwd_icon_eu` (extend by
  agreement — Stingray ignores identifiers it doesn't consume).
- Fetch techniques: our NOMADS-filter and ECMWF `.index` Range fetchers
  independently converged on Stingray's (verified live 2026-07-13).
  Adopt from theirs: the `filter_gfs_0p25_1hr.pl` hourly script and the
  wave endpoints above. cfgrib/eccodes stack shared on the VM.

## The product: `scores.json` (next build)

New `publish.py` (library) + arbiter consumer: computed from the
verification table per (model, region, lead bucket, variable); buckets
0–12 / 12–24 / 24–48h per contract suggestion. Written atomically to
`data/scores.json` AND served at `GET /scores.json` with `ETag` (content
hash) + `Last-Modified`. `schema_version: "1.0"`. Low-obs rows are
published with honest `obs_count`, never suppressed; a model with zero
verifications in the window is absent (absence is Stingray's fallback
signal). Score derivation (documented, stable):
`score = exp(-weighted_rmse_vector_ms / err_scale_ms)`, bounded [0,1],
higher better; `err_scale_ms` in config, default 2.5 m/s (≈ old 5kn).

## Architecture split (contract §Two consumers)

`gribbosaurus_rex/` stays the library core (fetch / extract / obs /
verify / publish). Thin consumers on top: **arbiter** = cron entrypoint
(`python -m gribbosaurus_rex arbiter-once`) that fetches → verifies →
publishes, and the API service that serves `scores.json`; **racing
tool** = existing dashboard/CLI (+ future app). Racing-tool features
never enter the arbiter path. Raw GRIBs are a first-class output with
per-consumer retention (`keep_runs` today; per-consumer knobs when the
racing tool needs deeper history). No imports either way with Stingray;
region bboxes arbitrary from day one (already true — race configs);
model registry is data-entry extensible (already true).

Phase 4 transform/bias-correction maths sits under the company umbrella
per the contract's IP note — it will live in a separately-licensable
module, not tangled through the core.

## Deployment (contract §Deployment)

Hetzner cohost, separable: system user `gribbo`, systemd service for the
API (+ timer/cron for the arbiter pass), own Caddy site block serving
`/scores.json` and the dashboard, shared system eccodes, GRIB
retention/pruning mandatory (exists: `keep_runs` + `RunStore.prune`).
All coupling via HTTP/agreed paths — either service can move boxes.
Unit files land in `deploy/` when we do this step. Local dev keeps
`python -m gribbosaurus_rex serve`.

## Known issues opened by this review

1. **Test pollution**: the Phase 2 smoke test wrote a fake `smoke-boat`
   yacht obs into the production DB; with yacht trust 1.0 and
   anchor-at-yacht it dominates current confidence scores (the 0.04–0.06
   flatline on the dashboard). Cleanup + guard (smoke tests must use a
   scratch data dir or a reserved `test` source that scoring excludes).
2. Verification quality: METAR-vs-model comparisons should prefer
   sea/coastal grid context; investigate systematic land-cell bias for
   coastal stations before trusting absolute score levels.
