"""ICON-2I fetcher — Italy's 2.2 km ICON via MeteoHub OPEN DATA (no auth).

Discovered live 2026-08-17 (scripts/icon2i_discover.py): MeteoHub publishes
ICON-2I as a plain open directory — no account, no API key (the old
MISTRAL_API_KEY plan is obsolete):

  {BASE}/{YYYYMMDDHH}/{VAR}/ICON_2I_SURFACE_PRESSURE_LEVELS_{YYYYMMDDHH}_{level}.grib

  runs:   00Z and 12Z (12Z observed complete ~13:45-14:25 UTC)
  files:  one multi-step GRIB per variable — 73 steps (0-72 h hourly),
          regular lat-lon 761x761 @ 0.02deg, 33.7-48.9N 3.0-22.0E, ~85 MB
  vars:   U_10M / V_10M -> level "heightAboveGround-10";
          PMSL -> "meanSea-0" (not fetched: scoring is wind-based, and
          skipping it halves the download — same call as UKV)

Regular grid + multi-step files => the existing extract/crop/verify
pipeline handles it unchanged; crop_on_fetch slims the full-Italy files
to the race union at fetch. NB this is a DIFFERENT model from dwd_icon_eu
(6.5 km ICON-EU) — both run side by side on Palermo-Montecarlo.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from gribbosaurus_rex.config import RaceConfig
from gribbosaurus_rex.fetch.base import BaseFetcher, FetchResult

log = logging.getLogger("gribbo.fetch.icon2i")

BASE = os.environ.get(
    "ICON2I_BASE",
    "https://meteohub.agenziaitaliameteo.it/nwp/ICON-2I_SURFACE_PRESSURE_LEVELS")

ICON2I_DOMAIN = dict(lat_min=33.7, lat_max=48.9, lon_min=3.0, lon_max=22.0)

# variable dir -> filename level suffix (from live discovery)
WIND_VARS = {"U_10M": "heightAboveGround-10", "V_10M": "heightAboveGround-10"}


class Icon2iFetcher(BaseFetcher):
    name = "icon_2i"
    resolution = "2.2 km · Italy · hourly to 72h"
    domain = ICON2I_DOMAIN
    crop_on_fetch = True
    cycle_hours = (0, 12)
    min_publish_lag = timedelta(hours=2)

    @staticmethod
    def _run_id(cycle: datetime) -> str:
        return cycle.strftime("%Y%m%d%H")

    @staticmethod
    def _url(base: str, run_id: str, var: str, level: str) -> str:
        return (f"{base}/{run_id}/{var}/"
                f"ICON_2I_SURFACE_PRESSURE_LEVELS_{run_id}_{level}.grib")

    def steps(self, max_lead_hours: int) -> list[int]:
        return list(range(0, min(max_lead_hours, 72) + 1))

    # -- run detection -------------------------------------------------------

    def is_available(self, cycle: datetime, max_lead_hours: int | None = None) -> bool:
        # the run dir appears before its files finish uploading, so probe
        # the actual wind files, not the directory
        rid = self._run_id(cycle)
        return all(self.head_ok(self._url(BASE, rid, v, lvl))
                   for v, lvl in WIND_VARS.items())

    # -- domain guard --------------------------------------------------------

    def _check_domain(self, cfg: RaceConfig) -> None:
        b, d = cfg.bbox, self.domain
        if (b.lat_max < d["lat_min"] or b.lat_min > d["lat_max"]
                or b.lon_max < d["lon_min"] or b.lon_min > d["lon_max"]):
            raise RuntimeError(
                f"Fetch bbox {b} has no overlap with the ICON-2I domain {d}; "
                "remove icon_2i from configs outside Italian waters.")

    # -- fetching ------------------------------------------------------------

    def fetch(self, cycle: datetime, cfg: RaceConfig, dest: Path) -> FetchResult:
        self._check_domain(cfg)
        rid = self._run_id(cycle)
        files: list[Path] = []
        for var, lvl in WIND_VARS.items():
            out = dest / f"{self.name}_{var.lower()}.grib2"
            self.download(self._url(BASE, rid, var, lvl), out, timeout=600)
            files.append(out)
        nbytes = self.slim_fetched(files, cfg)
        log.info("icon_2i %s: %d files, %.1f MB", cycle, len(files),
                 nbytes / 1e6)
        return FetchResult(files=files, nbytes=nbytes)
