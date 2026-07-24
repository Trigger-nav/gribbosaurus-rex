"""scores.json publisher — the one artefact Stingray consumes.

Contract: docs/integration/gribbosaurus-contract.md (schema 1.0).
Decisions: docs/integration/DECISIONS.md.

Score derivation (documented and stable, per contract requirement 4):
    score = exp(-weighted_rmse_vector_ms / err_scale_ms)
bounded [0,1], higher is better. Weighted RMSE of the wind vector error
(m/s) over all verifications of that (model, lead bucket) inside the
publish window, with the ARBITER weighting profile: per-source trust ×
recency half-life. No distance anchor — a region score represents the
region, not proximity to any vessel (DECISIONS.md (a)).

Low-obs entries are published with their honest obs_count, never
suppressed (Stingray applies its own thresholds). A (model, bucket) with
zero verifications is simply absent. `openmeteo` (model-derived, circular)
and `test` (smoke data) sources are excluded entirely.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from gribbosaurus_rex.config import RaceConfig
from gribbosaurus_rex.obs.store import ObsStore

log = logging.getLogger("gribbo.publish")

SCHEMA_VERSION = "1.0"

# Contract requirement 1: published identifiers match Stingray ingest
# source names. GFS publishes as nomads_gfs_ww3 — the wind fields are the
# same GFS 0.25° data inside Stingray's combined wind+wave npz source
# (wave metrics join the entries when wave scoring lands, DECISIONS.md (c)).
PUBLISH_NAMES = {
    "ifs": "ecmwf_ifs",
    "gfs": "nomads_gfs_ww3",
    "aifs": "ecmwf_aifs",
    "icon_eu": "dwd_icon_eu",
    # High-res tier — published under their own ids. Stingray currently
    # blends only ecmwf_ifs / nomads_gfs_ww3 and ignores models it doesn't
    # know, so surfacing these is contract-safe (design headroom, per the
    # contract) and lets the standalone racing tool rank them today.
    "mf_arome": "mf_arome",
    "mf_arpege": "mf_arpege",
    "mf_arpege_global": "mf_arpege_global",
    "mf_arome_antilles": "mf_arome_antilles",
    "ukmo_ukv": "ukmo_ukv",
}

LEAD_BUCKETS = ((0, 12), (12, 24), (24, 48))
EXCLUDED_SOURCES = {"openmeteo", "test"}


def _half_life(x: float, half: float) -> float:
    return 0.5 ** (max(0.0, x) / half) if half > 0 else 1.0


def _region_entries(cfg: RaceConfig, rows, now: datetime) -> list[dict]:
    """Score entries for one race's region. Verification rows are filtered
    to observations inside this race's bbox — fleet mode shares one
    verification table across regions."""
    region = {
        "lon_min": cfg.bbox.lon_min, "lat_min": cfg.bbox.lat_min,
        "lon_max": cfg.bbox.lon_max, "lat_max": cfg.bbox.lat_max,
    }
    in_region = [r for r in rows
                 if cfg.bbox.padded(0.5).contains(r["lat"], r["lon"])]

    entries = []
    for model in cfg.models:
        for lo, hi in LEAD_BUCKETS:
            samples = [
                r for r in in_region
                if r["model"] == model
                and r["source"] not in EXCLUDED_SOURCES
                and r["err_vector_ms"] is not None
                and lo <= r["lead_hours"] < hi
            ]
            if not samples:
                continue  # absence, not a fake zero — Stingray falls back

            w = np.array([
                cfg.trust_for(r["source"]) * _half_life(
                    (now - datetime.fromisoformat(r["obs_time"]))
                    .total_seconds() / 3600.0,
                    cfg.scoring.recency_half_h)
                for r in samples])
            ev = np.array([r["err_vector_ms"] for r in samples])
            ed = np.array([r["err_dir_deg"] if r["err_dir_deg"] is not None
                           else np.nan for r in samples])
            if w.sum() <= 0:
                continue

            rmse_ms = float(np.sqrt(np.sum(w * ev ** 2) / w.sum()))
            score = float(np.exp(-rmse_ms / cfg.scoring.err_scale_ms))
            metrics = {"wind_rmse_ms": round(rmse_ms, 3)}
            if not np.all(np.isnan(ed)):
                dmask = ~np.isnan(ed)
                metrics["dir_mae_deg"] = round(
                    float(np.sum(w[dmask] * ed[dmask]) / w[dmask].sum()), 1)

            entries.append({
                "model": PUBLISH_NAMES.get(model, model),
                "region": region,
                "lead_h_bucket": [lo, hi],
                "score": round(score, 4),
                "metrics": metrics,
                "obs_count": len({r["obs_id"] for r in samples}),
                "obs_window_h": cfg.scoring.publish_window_h,
            })
    return entries


def build_scores(races: RaceConfig | list[RaceConfig], obs_store: ObsStore,
                 now: datetime | None = None) -> dict:
    """Build the scores.json document (schema 1.0) — one entry per
    (model, region, lead bucket) across every race in the fleet."""
    if isinstance(races, RaceConfig):
        races = [races]
    now = now or datetime.now(timezone.utc)
    window_h = max(r.scoring.publish_window_h for r in races)
    rows = obs_store.verifications_window(window_h)

    entries = []
    for cfg in races:
        entries.extend(_region_entries(cfg, rows, now))

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(timespec="seconds")
                           .replace("+00:00", "Z"),
        "scores": entries,
    }


def scores_path(cfg: RaceConfig) -> Path:
    return cfg.data_dir / "scores.json"


def write_scores(cfg: RaceConfig, doc: dict) -> Path:
    """Atomic write (also the agreed-filesystem-path delivery mode)."""
    path = scores_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(doc, indent=1) + "\n")
    tmp.rename(path)
    log.info("published %s (%d score entries)", path, len(doc["scores"]))
    return path


def etag_for(content: bytes) -> str:
    """Strong ETag from content hash — stable across restarts."""
    return '"' + hashlib.md5(content).hexdigest() + '"'


def publish(races: RaceConfig | list[RaceConfig],
            obs_store: ObsStore | None = None) -> Path:
    """Build + write in one call (the arbiter's last step)."""
    if isinstance(races, RaceConfig):
        races = [races]
    store = obs_store or ObsStore(races[0].db_path)
    return write_scores(races[0], build_scores(races, store))
