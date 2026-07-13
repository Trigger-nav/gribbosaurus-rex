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
class RaceConfig:
    name: str
    bbox: BBox
    models: tuple[str, ...] = ("ifs", "gfs")
    max_lead_hours: int = 96
    keep_runs: int = 8
    data_dir: Path = REPO_ROOT / "data"
    poll_minutes: int = 10
    description: str = ""

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

    return RaceConfig(
        name=raw["name"],
        bbox=bbox,
        models=tuple(raw.get("models", ["ifs", "gfs"])),
        max_lead_hours=int(raw.get("max_lead_hours", 96)),
        keep_runs=int(raw.get("keep_runs", 8)),
        data_dir=data_dir,
        poll_minutes=int(raw.get("poll_minutes", 10)),
        description=raw.get("description", ""),
    )
