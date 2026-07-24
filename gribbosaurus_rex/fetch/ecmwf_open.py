"""ECMWF open data fetcher — IFS and AIFS at 0.25°.

Source: https://data.ecmwf.int/forecasts/
Layout: /forecasts/{YYYYMMDD}/{HH}z/{product}/0p25/{stream}/
        {YYYYMMDDHH}0000-{step}h-{stream}-fc.grib2      (+ .index)

The .index sidecar lists byte offsets per GRIB message, so we download
only the fields we need (10u, 10v, msl) with HTTP Range requests instead
of the full global file. GRIB messages are self-contained, so the ranged
chunks are simply concatenated into one .grib2 per forecast step.

Cycles/streams:
  IFS   00z/12z -> stream "oper" (to 144h @3h, then @6h to 240h)
        06z/18z -> stream "scda" (to 90h @3h)
  AIFS  all four cycles -> product "aifs-single", stream "oper" (@6h)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from gribbosaurus_rex.config import RaceConfig
from gribbosaurus_rex.fetch.base import BaseFetcher, FetchResult

log = logging.getLogger("gribbo.fetch.ecmwf")

ROOT = "https://data.ecmwf.int/forecasts"
PARAMS = ("10u", "10v", "msl")


class EcmwfOpenFetcher(BaseFetcher):
    name = "ifs"
    product = "ifs"
    resolution = "0.25° · global · physics"
    crop_on_fetch = True  # global 0.25° messages -> crop to the race areas
    min_publish_lag = timedelta(hours=6, minutes=30)

    def stream(self, cycle: datetime) -> str:
        return "oper" if cycle.hour in (0, 12) else "scda"

    def steps(self, max_lead_hours: int) -> list[int]:
        out = []
        for s in range(0, min(max_lead_hours, 144) + 1, 3):
            out.append(s)
        for s in range(150, min(max_lead_hours, 240) + 1, 6):
            out.append(s)
        return out

    def _steps_for(self, cycle: datetime, max_lead_hours: int) -> list[int]:
        cap = 90 if self.stream(cycle) == "scda" else 240
        return [s for s in self.steps(min(max_lead_hours, cap))]

    # -- urls ------------------------------------------------------------

    def _base_url(self, cycle: datetime, step: int) -> str:
        d = cycle.strftime("%Y%m%d")
        h = cycle.strftime("%H")
        stream = self.stream(cycle)
        fname = f"{d}{h}0000-{step}h-{stream}-fc.grib2"
        return f"{ROOT}/{d}/{h}z/{self.product}/0p25/{stream}/{fname}"

    def _index_url(self, cycle: datetime, step: int) -> str:
        # The sidecar REPLACES .grib2 with .index (…-fc.index),
        # it is not appended (…-fc.grib2.index).
        return self._base_url(cycle, step)[: -len(".grib2")] + ".index"

    # -- probing ---------------------------------------------------------

    def is_available(self, cycle: datetime, max_lead_hours: int | None = None) -> bool:
        # Probe the last step we actually need, so a run counts as available
        # once it is published far enough for our horizon.
        last = self._steps_for(cycle, max_lead_hours or 10_000)[-1]
        return self.head_ok(self._index_url(cycle, last))

    # -- fetching ----------------------------------------------------------

    def _index_entries(self, url_index: str) -> list[dict]:
        r = self.http.get(url_index, timeout=60)
        r.raise_for_status()
        entries = []
        for line in r.text.strip().splitlines():
            line = line.strip().rstrip(",")
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries

    def fetch(self, cycle: datetime, cfg: RaceConfig, dest: Path) -> FetchResult:
        files: list[Path] = []
        nbytes = 0
        for step in self._steps_for(cycle, cfg.max_lead_hours):
            base = self._base_url(cycle, step)
            entries = self._index_entries(self._index_url(cycle, step))
            wanted = [e for e in entries
                      if e.get("param") in PARAMS and e.get("levtype") == "sfc"]
            if not wanted:
                # AIFS indexes have no levtype on some params; fall back to param-only
                wanted = [e for e in entries if e.get("param") in PARAMS]
            if not wanted:
                raise RuntimeError(
                    f"No wanted params in index {self._index_url(cycle, step)}")

            out = dest / f"{self.name}_{step:03d}.grib2"
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".grib2.part")
            with open(tmp, "wb") as f:
                for e in wanted:
                    off, ln = int(e["_offset"]), int(e["_length"])
                    r = self.http.get(
                        base, timeout=120,
                        headers={"Range": f"bytes={off}-{off + ln - 1}"},
                    )
                    r.raise_for_status()
                    f.write(r.content)
                    nbytes += len(r.content)
            tmp.rename(out)
            files.append(out)
        # crop the global messages to the race areas -> cheap decode later
        nbytes = self.slim_fetched(files, cfg)
        log.info("%s %s: %d files, %.1f MB", self.name, cycle, len(files), nbytes / 1e6)
        return FetchResult(files=files, nbytes=nbytes)


class AifsFetcher(EcmwfOpenFetcher):
    name = "aifs"
    product = "aifs-single"
    resolution = "0.25° · global · ML"
    min_publish_lag = timedelta(hours=7)

    def stream(self, cycle: datetime) -> str:
        return "oper"

    def steps(self, max_lead_hours: int) -> list[int]:
        return list(range(0, min(max_lead_hours, 360) + 1, 6))

    def _steps_for(self, cycle: datetime, max_lead_hours: int) -> list[int]:
        return self.steps(max_lead_hours)
