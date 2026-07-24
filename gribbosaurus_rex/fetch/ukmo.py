"""Met Office DataHub UKV fetcher — order-based, regular lat-lon (2 km).

Unlike the URL-per-cycle fetchers, DataHub serves data from a pre-defined
"order" (model + params + area + timesteps) via an order API:

  base:  https://data.hub.api.metoffice.gov.uk/atmospheric-models/1.0.0
  list:  GET /orders/{orderId}/latest      -> orderDetails.files[]
  data:  GET /orders/{orderId}/latest/{fileId}/data
         (Accept: application/x-grib; fileId URL-encoded; follows 302s)
  auth:  header  apikey: <DATAHUB_API_KEY>

The `ukv-channel` order (model mo-uk-latlon) delivers 10 m wind (u/v) + MSL
over the Solent/Channel, 0-48 h hourly, as **multi-step regular lat-lon
GRIB2** — one file per surface per run with all 49 steps inside. Regular
grid => the whole extract/crop/verify pipeline handles it unchanged, and
extract._to_time_indexed assembles the multi-step files. We fetch only the
`agl` wind file (u10+v10); scoring is wind-based, so MSL isn't needed and
skipping it keeps the download to ~8 MB/run. Each run's file is cropped to
the race area at fetch (crop_on_fetch) like the rest of the fleet.

Structure of a /latest file entry (from live discovery 2026-07-24):
  {"fileId": "agl_u-component-of-wind+v-component-of-wind_10_2026072400",
   "surfaceId": "agl", "levels": ["10"], "run": "0",
   "runDateTime": "2026-07-24T00:00:00Z",
   "parameters": ["u-component-of-wind","v-component-of-wind"], ...}
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import requests

from gribbosaurus_rex.config import RaceConfig
from gribbosaurus_rex.fetch.base import BaseFetcher, FetchResult

log = logging.getLogger("gribbo.fetch.ukmo")

BASE = os.environ.get(
    "DATAHUB_BASE",
    "https://data.hub.api.metoffice.gov.uk/atmospheric-models/1.0.0")

# the ukv-channel order region (edit the order in the portal to change this)
UKV_DOMAIN = dict(lat_min=49.0, lat_max=52.0, lon_min=-6.5, lon_max=2.5)


class UkvFetcher(BaseFetcher):
    name = "ukmo_ukv"
    resolution = "2 km · UK lat-lon · hourly to 48h"
    domain = UKV_DOMAIN
    crop_on_fetch = True
    cycle_hours = (0, 6, 12, 18)
    min_publish_lag = timedelta(hours=2)

    # -- config / auth -------------------------------------------------------

    def _order_id(self) -> str:
        return os.environ.get("DATAHUB_ORDER_ID", "ukv-channel")

    def _auth_headers(self) -> dict:
        key = os.environ.get("DATAHUB_API_KEY")
        if not key:
            raise RuntimeError(
                "DATAHUB_API_KEY not set — add the Met Office DataHub apikey "
                "to /etc/gribbo/env")
        return {"apikey": key}

    def _get_latest(self) -> dict:
        url = f"{BASE}/orders/{self._order_id()}/latest"
        r = self.http.get(url, headers=self._auth_headers(), timeout=60)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _files(data: dict) -> list[dict]:
        return data.get("orderDetails", {}).get("files", []) or []

    @staticmethod
    def _newest_run(files: list[dict]) -> str | None:
        dts = [f.get("runDateTime") for f in files if f.get("runDateTime")]
        return max(dts) if dts else None

    @staticmethod
    def _pick_wind(files: list[dict], run_iso: str) -> dict | None:
        """The 10 m wind (surfaceId 'agl') file for the given run. The order
        lists each run twice (relative +HH and absolute-datetime forms) — the
        first match is fine, they address the same data."""
        return next((f for f in files
                     if f.get("runDateTime") == run_iso
                     and f.get("surfaceId") == "agl"), None)

    @staticmethod
    def _data_url(base: str, order_id: str, file_id: str) -> str:
        return f"{base}/orders/{order_id}/latest/{quote(file_id, safe='')}/data"

    def steps(self, max_lead_hours: int) -> list[int]:
        return list(range(0, min(max_lead_hours, 48) + 1))

    # -- run detection -------------------------------------------------------

    def is_available(self, cycle: datetime, max_lead_hours: int | None = None) -> bool:
        # Only the single newest run in the order counts as available, so the
        # scheduler fetches just it (not every run the order retains).
        try:
            files = self._files(self._get_latest())
        except (requests.RequestException, RuntimeError):
            return False
        return self._newest_run(files) == cycle.strftime("%Y-%m-%dT%H:00:00Z")

    # -- domain guard --------------------------------------------------------

    def _check_domain(self, cfg: RaceConfig) -> None:
        b, d = cfg.bbox, self.domain
        if (b.lat_max < d["lat_min"] or b.lat_min > d["lat_max"]
                or b.lon_max < d["lon_min"] or b.lon_min > d["lon_max"]):
            raise RuntimeError(
                f"Fetch bbox {b} has no overlap with the UKV order region {d}; "
                "remove ukmo_ukv from configs that don't reach it.")

    # -- fetching ------------------------------------------------------------

    def fetch(self, cycle: datetime, cfg: RaceConfig, dest: Path) -> FetchResult:
        self._check_domain(cfg)
        headers = self._auth_headers()
        files = self._files(self._get_latest())
        target = cycle.strftime("%Y-%m-%dT%H:00:00Z")
        wind = self._pick_wind(files, target)
        if wind is None:
            raise RuntimeError(f"ukmo_ukv: no agl wind file for run {target}")

        url = self._data_url(BASE, self._order_id(), wind["fileId"])
        out = dest / f"{self.name}_wind.grib2"
        out.parent.mkdir(parents=True, exist_ok=True)
        self.download(url, out,
                      headers={**headers, "Accept": "application/x-grib"},
                      timeout=300)
        nbytes = self.slim_fetched([out], cfg)
        log.info("ukmo_ukv %s: 1 file, %.1f MB", cycle, nbytes / 1e6)
        return FetchResult(files=[out], nbytes=nbytes)
