"""Race/venue configuration.

A RaceConfig describes *where* and *what* to fetch: bounding box, models,
forecast horizon. One YAML file per race/venue lives in configs/.

Resolution order for the active config:
  1. explicit path argument
  2. $GRIBBO_CONFIG environment variable
  3. configs/balearics.yaml (repo default)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "configs" / "balearics.yaml"


@dataclass(frozen=True)
class BBox:
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    def __post_init__(self):
        if not (-90 <= self.lat_min < self.lat_max <= 90):
            raise ValueError(f"Bad latitude range: {self.lat_min}..{self.lat_max}")
        if not (-180 <= self.lon_min < self.lon_max <= 180):
            raise ValueError(f"Bad longitude range: {self.lon_min}..{self.lon_max}")

    def contains(self, lat: float, lon: float) -> bool:
        return (self.lat_min <= lat <= self.lat_max
                and self.lon_min <= lon <= self.lon_max)

    def padded(self, deg: float) -> "BBox":
        """Slightly larger box (used so interpolation near the edge works)."""
        return BBox(
            max(-90.0, self.lat_min - deg),
            min(90.0, self.lat_max + deg),
            max(-180.0, self.lon_min - deg),
            min(180.0, self.lon_max + deg),
        )


@dataclass(frozen=True)
class NmeaConfig:
    enabled: bool = False
    transport: str = "udp"   # udp | tcp
    port: int = 10110


@dataclass(frozen=True)
class ObsConfig:
    metar: bool = True
    ndbc_stations: tuple[str, ...] = ()
    openmeteo: bool = False
    focus_lat: float | None = None   # distance-weight anchor when no yacht fix
    focus_lon: float | None = None
    nmea: NmeaConfig = NmeaConfig()


@dataclass(frozen=True)
class ScoringConfig:
    window_h: float = 48.0
    half_weight_nm: float = 30.0
    lead_half_h: float = 24.0
    recency_half_h: float = 12.0
    err_scale_kn: float = 5.0


DEFAULT_TRUST = {"yacht": 1.0, "metar": 0.85, "ndbc": 0.9, "openmeteo": 0.4}


@dataclass(frozen=True)
class RaceConfig:
    name: str
    bbox: BBox
    models: tuple[str, ...] = ("ifs", "gfs")
    max_lead_hours: int = 96
    keep_runs: int = 8
    data_dir: Path = REPO_ROOT / "data"
    poll_minutes: int = 10
    description: str = ""
    obs: ObsConfig = ObsConfig()
    scoring: ScoringConfig = ScoringConfig()
    trust: tuple[tuple[str, float], ...] = tuple(DEFAULT_TRUST.items())

    def trust_for(self, source: str) -> float:
        return dict(self.trust).get(source, 0.5)

    def anchor(self) -> tuple[float, float]:
        """Distance-weight anchor: configured focus point, else bbox centre."""
        if self.obs.focus_lat is not None and self.obs.focus_lon is not None:
            return (self.obs.focus_lat, self.obs.focus_lon)
        return ((self.bbox.lat_min + self.bbox.lat_max) / 2,
                (self.bbox.lon_min + self.bbox.lon_max) / 2)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "gribbo.sqlite"

    def grib_dir(self, model: str, cycle_iso: str) -> Path:
        """Directory layout: data/grib/<model>/<cycle>/ (cycle like 20260713T00Z)."""
        return self.data_dir / "grib" / model / cycle_iso


def load_config(path: str | os.PathLike | None = None) -> RaceConfig:
    import yaml  # local import so pure-logic tests don't need PyYAML

    cfg_path = Path(path or os.environ.get("GRIBBO_CONFIG") or DEFAULT_CONFIG)
    if not cfg_path.exists():
        raise FileNotFoundError(f"Race config not found: {cfg_path}")

    raw = yaml.safe_load(cfg_path.read_text())

    bbox = BBox(**{k: float(v) for k, v in raw["bbox"].items()})

    data_dir = Path(raw.get("data_dir", "data"))
    if not data_dir.is_absolute():
        data_dir = REPO_ROOT / data_dir

    obs_raw = raw.get("observations", {}) or {}
    nmea_raw = obs_raw.get("nmea", {}) or {}
    focus = obs_raw.get("focus") or {}
    obs = ObsConfig(
        metar=bool(obs_raw.get("metar", True)),
        ndbc_stations=tuple(str(s) for s in obs_raw.get("ndbc_stations", []) or []),
        openmeteo=bool(obs_raw.get("openmeteo", False)),
        focus_lat=float(focus["lat"]) if "lat" in focus else None,
        focus_lon=float(focus["lon"]) if "lon" in focus else None,
        nmea=NmeaConfig(
            enabled=bool(nmea_raw.get("enabled", False)),
            transport=str(nmea_raw.get("transport", "udp")).lower(),
            port=int(nmea_raw.get("port", 10110)),
        ),
    )

    sc_raw = raw.get("scoring", {}) or {}
    scoring = ScoringConfig(
        window_h=float(sc_raw.get("window_h", 48)),
        half_weight_nm=float(sc_raw.get("half_weight_nm", 30)),
        lead_half_h=float(sc_raw.get("lead_half_h", 24)),
        recency_half_h=float(sc_raw.get("recency_half_h", 12)),
        err_scale_kn=float(sc_raw.get("err_scale_kn", 5.0)),
    )

    trust = dict(DEFAULT_TRUST)
    trust.update({str(k): float(v) for k, v in (raw.get("trust") or {}).items()})

    return RaceConfig(
        name=raw["name"],
        bbox=bbox,
        models=tuple(raw.get("models", ["ifs", "gfs"])),
        max_lead_hours=int(raw.get("max_lead_hours", 96)),
        keep_runs=int(raw.get("keep_runs", 8)),
        data_dir=data_dir,
        poll_minutes=int(raw.get("poll_minutes", 10)),
        description=raw.get("description", ""),
        obs=obs,
        scoring=scoring,
        trust=tuple(sorted(trust.items())),
    )
