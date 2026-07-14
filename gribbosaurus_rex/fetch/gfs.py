"""NOAA GFS 0.25° fetcher via the NOMADS grib filter.

The filter CGI does server-side subsetting by variable, level AND bounding
box, so each step downloads only a few kB for a race-sized area.

  https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl

NOMADS asks users to stay under ~50 requests/minute; the fetch loop
throttles itself accordingly.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from gribbosaurus_rex.config import RaceConfig
from gribbosaurus_rex.fetch.base import BaseFetcher, FetchResult

log = logging.getLogger("gribbo.fetch.gfs")

FILTER = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
THROTTLE_S = 1.5  # keep well under NOMADS rate limits


class GfsFetcher(BaseFetcher):
    name = "gfs"
    region_subset = True   # NOMADS filter crops server-side to the fetch bbox
    min_publish_lag = timedelta(hours=3, minutes=30)

    def steps(self, max_lead_hours: int) -> list[int]:
        out = list(range(0, min(max_lead_hours, 120) + 1, 1))
        out += list(range(123, min(max_lead_hours, 384) + 1, 3))
        return out

    def _params(self, cycle: datetime, step: int, cfg: RaceConfig) -> dict:
        return {
            "file": f"gfs.t{cycle:%H}z.pgrb2.0p25.f{step:03d}",
            "dir": f"/gfs.{cycle:%Y%m%d}/{cycle:%H}/atmos",
            "var_UGRD": "on",
            "var_VGRD": "on",
            "var_PRMSL": "on",
            "var_GUST": "on",
            "lev_10_m_above_ground": "on",
            "lev_mean_sea_level": "on",
            "lev_surface": "on",
            "subregion": "",
            "leftlon": cfg.bbox.lon_min,
            "rightlon": cfg.bbox.lon_max,
            "toplat": cfg.bbox.lat_max,
            "bottomlat": cfg.bbox.lat_min,
        }

    def is_available(self, cycle: datetime, max_lead_hours: int | None = None) -> bool:
        # Probe the last step we need on the plain HTTPS dir (cheap HEAD).
        last = self.steps(max_lead_hours or 120)[-1]
        url = (f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/"
               f"gfs.{cycle:%Y%m%d}/{cycle:%H}/atmos/"
               f"gfs.t{cycle:%H}z.pgrb2.0p25.f{last:03d}")
        return self.head_ok(url)

    def fetch(self, cycle: datetime, cfg: RaceConfig, dest: Path) -> FetchResult:
        files: list[Path] = []
        nbytes = 0
        for step in self.steps(cfg.max_lead_hours):
            out = dest / f"{self.name}_{step:03d}.grib2"
            out.parent.mkdir(parents=True, exist_ok=True)
            r = self.http.get(FILTER, params=self._params(cycle, step, cfg),
                              timeout=120)
            if r.status_code == 404:
                # step not published (shouldn't happen once run is complete)
                raise RuntimeError(f"GFS step {step} missing for {cycle}")
            r.raise_for_status()
            if not r.content.startswith(b"GRIB"):
                raise RuntimeError(
                    f"GFS filter returned non-GRIB for step {step} "
                    f"(len={len(r.content)}): {r.content[:120]!r}")
            out.write_bytes(r.content)
            nbytes += len(r.content)
            files.append(out)
            time.sleep(THROTTLE_S)
        log.info("gfs %s: %d files, %.1f MB", cycle, len(files), nbytes / 1e6)
        return FetchResult(files=files, nbytes=nbytes)
