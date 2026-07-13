# Gribbosaurus Rex ↔ Stingray integration contract

**v0.1 draft · July 2026.** Stingray-side specification of what the planner's
ingest layer will consume from Gribbosaurus Rex (multi-model GRIB confidence
scoring against live observations). Copy of this file should live in the
Gribbosaurus repo; changes are negotiated, versioned, and reflected in both.

## Two consumers, one engine (July 2026)

Gribbosaurus serves two use cases from one scoring engine: (1) the
**Stingray arbiter** — the cron service publishing `scores.json` per this
contract; (2) a **personal racing tool** — a local CLI/app downloading all
models' raw GRIBs (routers consume GRIB directly — raw files are a
first-class output with per-consumer retention), showing the same scores,
and later a suggested per-model transform. Architectural consequences the
Gribbosaurus repo must honour: library-core + thin-consumers split (the
racing tool's feature gravity must not bloat the arbiter service);
arbitrary region bboxes from day one; an extensible model registry (adding
a model = data entry, not a fork — Stingray consumes only the identifiers
it names below); the engine runs fully local/offline. **IP note:** the
"suggested transform" is per-model bias correction — Stingray differentiator
4.3's maths — and must sit under the company umbrella (personal racing use
licensed from it, not the other way round).

## Integration shape (deliberately thin)

Data-only coupling. **Neither project imports the other.** Gribbosaurus runs
as its own service (own repo, own systemd unit, own subdomain) and publishes
one artefact; Stingray's cloud role consults it when choosing which already-
fetched model npz to serve to vessels. If Gribbosaurus is down or stale,
Stingray falls back to its configured default model — the planner never
depends on Gribbosaurus for liveness.

```
gribbosaurus (cron):  obs sources ──▶ score models ──▶ scores.json
stingray cloud (cron): fetch NOMADS + ECMWF npz ──▶ read scores.json
                       ──▶ copy winning npz to served path ──▶ existing
                       hot-swap + vessel sync + provenance do the rest
```

## The artefact: `scores.json`

Served over HTTP (same box, own subdomain or path) with `ETag`/
`Last-Modified`, and/or written to an agreed filesystem path.

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-07-12T06:10:00Z",
  "scores": [
    {
      "model": "ecmwf_ifs",
      "region": {"lon_min": 6.7, "lat_min": 40.75, "lon_max": 10.15, "lat_max": 44.0},
      "lead_h_bucket": [0, 12],
      "score": 0.86,
      "metrics": {"hs_rmse_m": 0.18, "wind_rmse_ms": 1.1, "dir_mae_deg": 14},
      "obs_count": 42,
      "obs_window_h": 24
    }
  ]
}
```

Hard requirements, each with its reason:

1. **Model identifiers must match Stingray ingest source names** —
   `ecmwf_ifs`, `nomads_gfs_ww3` (extend by agreement). These flow into
   Stingray's npz provenance and voyage debriefs verbatim.
2. **Scores resolved by region bbox AND lead-time bucket** (suggest 0–12 /
   12–24 / 24–48h). Model skill varies strongly by both; a single global
   score is useless for routing decisions.
3. **`generated_at` + per-score `obs_count` are mandatory** — Stingray
   treats scores older than a configured staleness threshold, or backed by
   too few observations, as absent (falls back to default). Low-obs scores
   must be publishable-but-ignorable, not hidden.
4. **Score semantics documented and stable**: higher = better, bounded
   [0,1], derivation written down in the Gribbosaurus repo. Stingray's
   "no invented numbers" principle extends across this seam — a debrief may
   one day cite "planned on ECMWF (confidence 0.86, 42 obs)" to a client.
5. **`schema_version`**, semver, breaking changes bump major.

## Convention alignment (critical, easy to get silently wrong)

Observations-vs-model comparison is only meaningful if both sides speak the
same conventions. Gribbosaurus must adopt Stingray's ingest-boundary rules
(see stingray `ingest/grib_common.py` and CORE_PORTING_NOTES.md B2):

- Directions: **"coming from"**, degrees true. (WW3/ECMWF from-convention
  was empirically verified in Stingray ticket 0.5 — reuse that result.)
- Longitudes: −180…180. Times: UTC everywhere.
- Units: SI internally (m, m/s); knots only at display boundaries.
- Land/missing model cells are **missing, never calm** — an observation
  compared against a zero-filled land cell poisons the score.
- GRIB parsing: same cfgrib/eccodes stack (shared system install on the
  cohosted VM). NOMADS bbox-subsetting and ECMWF `.index` Range-request
  techniques are already solved in Stingray's `ingest/fetch_grib_*.py` —
  copy the approach (or the code) rather than re-deriving it.

## Deployment (cohosted, separable)

Same Hetzner VM as the Stingray cloud role: own system user, own systemd
service + cron, own Caddy site block. Shares the system eccodes install.
Must implement GRIB retention/pruning (disk is shared, 80GB). Nothing may
assume colocation (all coupling via HTTP/agreed paths), so either service
can move boxes without the other noticing.

## Later phases (design headroom, don't build now)

- Per-lead-time **blending** weights, not just winner-take-all selection.
- Feeding Stingray's onboard correction layer (spec differentiator 4.3).
- Pilot-vessel telemetry as an observation source (the fleet network) —
  implies an obs-ingest interface someday; keep obs sources pluggable.

## Open questions for the Gribbosaurus side

(a) Observation sources and their weighting (buoys/NDBC, ships, coastal
stations?) — document per-source trust. (b) Scoring window length vs
responsiveness trade-off. (c) Whether wave and wind get separate scores
(Stingray would prefer separate — routing cares more about wave skill).
