"""Météo-France AROME / ARPEGE fetchers — the racing-grade high-res tier.

Source: the Météo-France "Paquets Modèles" (model packages) open-data API
on the public portal.

  base:  https://public-api.meteofrance.fr/previnum/DPPaquet{SERVICE}/v1
  path:  /models/{MODEL}/grids/{GRID}/packages/{PACKAGE}/{PRODUCT}
  query: ?referencetime=<ISO8601Z>&time=<RANGE>&format=grib2

Unlike the native models, the packaged data is delivered as **regular
lat-lon GRIB2** (so extract.py handles it directly) but grouped as
**multi-step** files: one package request returns every forecast hour in
a time range (e.g. `00H06H`) as many messages in one .grib2. extract.py's
`_to_time_indexed` assembles those into the time axis.

Authentication — two supported schemes, in preference order:

  METEOFRANCE_APPLICATION_ID  (RECOMMENDED for the server)
      The portal's permanent base64 "application id". Exchanged here for a
      short-lived OAuth2 Bearer token, refreshed automatically. This is the
      only scheme that survives the arbiter running every 10 min, because
      raw tokens expire hourly.

  METEOFRANCE_API_KEY         (simple / manual)
      A key or a freshly-minted token used directly. Header scheme picked
      by METEOFRANCE_AUTH = `apikey` (default, `apikey: <key>`) or `bearer`
      (`Authorization: Bearer <token>`). Fine for a one-off smoke; a raw
      token here will expire mid-day on the server.

╔═══════════════════════════════════════════════════════════════════════╗
║ FIRST-LIVE-SMOKE CAVEAT                                                ║
║ A few path/query tokens below (the PRODUCT suffix and the exact TIME  ║
║ range groupings) are Météo-France's documented conventions but cannot ║
║ be exercised from the dev sandbox (no outbound network, no key yet).  ║
║ scripts/live_smoke_meteofrance.py queries the catalogue and prints    ║
║ the real reference times + time tokens so these constants get pinned  ║
║ against actual API output. fetch() skips (does not fail on) a 404'd   ║
║ time range so a token mismatch degrades to partial data, not a crash. ║
╚═══════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

from gribbosaurus_rex.config import RaceConfig
from gribbosaurus_rex.fetch.base import BaseFetcher, FetchResult

log = logging.getLogger("gribbo.fetch.meteofrance")

ROOT = "https://public-api.meteofrance.fr/previnum"
TOKEN_URL = "https://portail-api.meteofrance.fr/token"


def _ranges(bounds: tuple[int, ...], width: int = 2) -> list[tuple[int, str]]:
    """Cumulative hour bounds -> (range_end, "aaHbbH") tokens.

    e.g. bounds (0, 6, 12, 18, 24) at width 2 -> [(6,"00H06H"),
    (12,"07H12H"), (18,"13H18H"), (24,"19H24H")]. The first window is
    inclusive of 0; subsequent windows start at previous_end + 1
    (Météo-France convention). `width` is the zero-pad digit count —
    AROME uses 2 (\\d{2}H\\d{2}H), ARPEGE uses 3 (\\d{3}H\\d{3}H).
    """
    out: list[tuple[int, str]] = []
    for i in range(1, len(bounds)):
        lo = bounds[i - 1] + (1 if i > 1 else 0)
        hi = bounds[i]
        out.append((hi, f"{lo:0{width}d}H{hi:0{width}d}H"))
    return out


def _hours(max_h: int, step: int = 1, width: int = 3) -> list[tuple[int, str]]:
    """Single-échéance tokens, e.g. [(0,"000H"),(1,"001H"),...] — the form
    AROME-Outre-mer requires (\\d{3}H), one forecast hour per request."""
    return [(h, f"{h:0{width}d}H") for h in range(0, max_h + 1, step)]


# Time tokens PINNED against the live API 2026-07-23 (the 400 error bodies
# report the required regex per model):
#   AROME France  -> \d{2}H\d{2}H   6-hour ranges to 48h   (00H06H …)
#   ARPEGE 0.1    -> \d{3}H\d{3}H   12-hour ranges to 102h (000H012H …)
#   ARPEGE 0.25   -> \d{3}H\d{3}H   24-hour ranges to 102h (000H024H …)
#   AROME-OM      -> \d{3}H         single hours to 48h    (000H, 001H …)
# (ARPEGE window widths enumerated live per grid — 0.1 is finer/heavier so
#  it splits into 12h chunks; 0.25 global into 24h.)
AROME_RANGES = _ranges((0, 6, 12, 18, 24, 30, 36, 42, 48), width=2)
ARPEGE_EU_RANGES = _ranges((0, 12, 24, 36, 48, 60, 72, 84, 96, 102), width=3)
ARPEGE_GLOBAL_RANGES = _ranges((0, 24, 48, 72, 102), width=3)
# AROME-OM is one file per échéance; 3-hourly (17 files) instead of hourly
# (49) keeps the per-pass fetch/decode load sane on the shared box while
# still giving Caribbean planning ample resolution.
AROMEOM_HOURS = _hours(48, step=3, width=3)


def _mf_keep(msg_id) -> bool:
    """Field filter for slimming: keep only 10 m wind + mean-sea-level
    pressure messages, drop the ~12 other SP1 surface fields (2 m temp/
    humidity, cloud, precip, radiation, fluxes…) we never score. Biased to
    KEEP on any uncertainty — a bigger file is slow, a missing wind field
    is broken. 10 m wind is heightAboveGround/level=10; MSL is meanSea."""
    import eccodes as ec
    try:
        tol = ec.codes_get(msg_id, "typeOfLevel")
    except Exception:  # noqa: BLE001
        return True
    if tol == "meanSea":
        return True
    if tol == "heightAboveGround":
        try:
            return int(ec.codes_get(msg_id, "level")) == 10
        except Exception:  # noqa: BLE001
            return True
    return False


class MeteoFrancePackageFetcher(BaseFetcher):
    """Shared machinery for the Météo-France packages API."""

    #: DPPaquet service path segment, e.g. "DPPaquetAROME"
    service: str = "DPPaquetAROME"
    #: model id in the path, e.g. "AROME" / "ARPEGE"
    model_id: str = "AROME"
    #: grid string as the API expects it, e.g. "0.025"
    grid: str = "0.025"
    #: product endpoint suffix, e.g. "productARO" / "productARP"
    product: str = "productARO"
    #: package holding 10m wind + MSL pressure (surface params set 1)
    package: str = "SP1"
    #: (range_end_hours, token) groupings to request
    ranges: list[tuple[int, str]] = AROME_RANGES

    cycle_hours = (0, 6, 12, 18)
    min_publish_lag = timedelta(hours=3)

    def __init__(self):
        super().__init__()
        self._token: str | None = None
        self._token_exp: float = 0.0  # epoch seconds

    def _service_url(self) -> str:
        return f"{ROOT}/{self.service}/v1"

    def _package_url(self, cycle: datetime, time_token: str) -> str:
        ref = cycle.strftime("%Y-%m-%dT%H:00:00Z")
        return (f"{self._service_url()}/models/{self.model_id}"
                f"/grids/{self.grid}/packages/{self.package}/{self.product}"
                f"?referencetime={ref}&time={time_token}&format=grib2")

    def _bearer_from_application_id(self) -> str:
        """Exchange the permanent APPLICATION_ID for a cached OAuth2 token."""
        app_id = os.environ["METEOFRANCE_APPLICATION_ID"]
        now = time.time()
        if self._token and now < self._token_exp - 60:
            return self._token
        r = self.http.post(
            TOKEN_URL, data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {app_id}"}, timeout=30)
        r.raise_for_status()
        j = r.json()
        self._token = j["access_token"]
        self._token_exp = now + float(j.get("expires_in", 3600))
        log.info("meteofrance: refreshed token (expires in %ss)",
                 j.get("expires_in", "?"))
        return self._token

    def _auth_headers(self) -> dict:
        if os.environ.get("METEOFRANCE_APPLICATION_ID"):
            return {"Authorization": f"Bearer {self._bearer_from_application_id()}"}
        key = os.environ.get("METEOFRANCE_API_KEY", "")
        if not key:
            raise RuntimeError(
                "No Météo-France credential set. Register on the portal and "
                "set METEOFRANCE_APPLICATION_ID (recommended) or "
                "METEOFRANCE_API_KEY in /etc/gribbo/env.")
        mode = os.environ.get("METEOFRANCE_AUTH", "apikey").lower()
        if mode == "bearer":
            return {"Authorization": f"Bearer {key}"}
        return {"apikey": key}

    # -- which ranges do we need for this horizon ---------------------------

    def _needed_ranges(self, max_lead_hours: int) -> list[tuple[int, str]]:
        # include every window whose START hour is within the horizon —
        # width-agnostic, so AROME's 6h and ARPEGE's 12/24h windows both work
        needed: list[tuple[int, str]] = []
        prev_end = -1
        for end, token in self.ranges:
            start = 0 if prev_end < 0 else prev_end + 1
            if start <= max_lead_hours:
                needed.append((end, token))
            prev_end = end
        return needed or self.ranges[:1]  # always keep at least the first

    def steps(self, max_lead_hours: int) -> list[int]:
        cap = min(max_lead_hours, self.ranges[-1][0])
        return list(range(0, cap + 1, 1))

    # -- probing ------------------------------------------------------------

    def is_available(self, cycle: datetime, max_lead_hours: int | None = None) -> bool:
        # No credential yet (before Jack registers) -> quietly "not available"
        # rather than raising a traceback into the journal every 10 minutes.
        try:
            headers = self._auth_headers()
        except RuntimeError:
            log.debug("%s: no Météo-France credential set — skipping", self.name)
            return False
        last = self._needed_ranges(max_lead_hours or self.ranges[-1][0])[-1]
        try:
            return self.head_ok_auth(self._package_url(cycle, last[1]), headers)
        except requests.RequestException:
            return False

    def head_ok_auth(self, url: str, headers: dict, timeout: int = 25) -> bool:
        """HEAD (with auth) falling back to a 1-byte ranged GET."""
        try:
            r = self.http.head(url, timeout=timeout, allow_redirects=True,
                               headers=headers)
            if r.status_code in (400, 403, 405, 501):  # HEAD unloved / needs GET
                r = self.http.get(url, timeout=timeout, stream=True,
                                  headers={**headers, "Range": "bytes=0-0"})
            return r.status_code in (200, 206)
        except requests.RequestException:
            return False

    # -- domain guard (full-domain files; partial overlap is fine) ----------

    def _check_domain(self, cfg: RaceConfig) -> None:
        if self.domain is None:
            return
        b = cfg.bbox
        d = self.domain
        no_overlap = (b.lat_max < d["lat_min"] or b.lat_min > d["lat_max"]
                      or b.lon_max < d["lon_min"] or b.lon_min > d["lon_max"])
        if no_overlap:
            raise RuntimeError(
                f"Fetch bbox {b} has no overlap with the {self.name} domain "
                f"{d}; remove {self.name} from the configs requesting it.")

    # -- fetching -----------------------------------------------------------

    def _crop_bbox(self, cfg: RaceConfig):
        """The bbox to slim each download to: the union of races using THIS
        model (small — e.g. the Channel), padded generously. Falls back to
        the fetch bbox if per-model bboxes aren't available (single-race mode)."""
        mb = getattr(cfg, "model_bboxes", None) or {}
        return mb.get(self.name, cfg.bbox).padded(1.0)

    def fetch(self, cycle: datetime, cfg: RaceConfig, dest: Path) -> FetchResult:
        self._check_domain(cfg)
        headers = self._auth_headers()
        crop_bbox = self._crop_bbox(cfg)
        files: list[Path] = []
        nbytes = 0
        for end_h, token in self._needed_ranges(cfg.max_lead_hours):
            url = self._package_url(cycle, token)
            out = dest / f"{self.name}_{token}.grib2"
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".grib2.part")
            try:
                self.download(url, out, headers=headers, timeout=300)
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else "?"
                # a rejected/not-yet-published échéance shouldn't nuke the run;
                # 400 = token the API doesn't recognise for this model
                if str(code) in ("400", "404", "500"):
                    log.warning("%s %s: échéance %s unavailable (HTTP %s) — skipping",
                                self.name, cycle, token, code)
                    tmp.unlink(missing_ok=True)
                    continue
                raise
            # slim to 10m wind + MSL and crop to this model's race areas, so
            # the downstream decode is cheap. Safe: keeps the full file on any
            # error. This is what makes high-res viable on a small box.
            try:
                from gribbosaurus_rex.export import slim_crop_file
                kept, total = slim_crop_file(out, crop_bbox, keep=_mf_keep)
                if kept:
                    log.info("%s %s: slimmed %d/%d msgs -> %.1f MB", self.name,
                             token, kept, total, out.stat().st_size / 1e6)
            except Exception:  # noqa: BLE001
                log.warning("%s %s: slim/crop error — keeping full file",
                            self.name, token)
            files.append(out)
            nbytes += out.stat().st_size
        if not files:
            raise RuntimeError(
                f"{self.name} {cycle}: no time ranges downloaded — check the "
                "time-range tokens against live_smoke_meteofrance.py output")
        log.info("%s %s: %d files, %.1f MB", self.name, cycle, len(files),
                 nbytes / 1e6)
        return FetchResult(files=files, nbytes=nbytes)


# --------------------------------------------------------------------------
# Concrete models
# --------------------------------------------------------------------------

class AromeFranceFetcher(MeteoFrancePackageFetcher):
    """AROME 2.5 km over metropolitan France + near seas.

    Default grid 0.025° (2.5 km) — the 0.01° (1.3 km) grid is ~4x the data
    volume; opt in per race with GRIBBO_AROME_GRID=0.01 if the box has room.
    """
    name = "mf_arome"
    resolution = "2.5 km · France & near seas · hourly"
    service = "DPPaquetAROME"
    model_id = "AROME"
    grid = os.environ.get("GRIBBO_AROME_GRID", "0.025")
    product = "productARO"
    ranges = AROME_RANGES
    # AROME France domain (generous; downloads are full-domain regardless)
    domain = dict(lat_min=37.5, lat_max=55.4, lon_min=-12.0, lon_max=16.0)
    min_publish_lag = timedelta(hours=3)


class ArpegeFetcher(MeteoFrancePackageFetcher):
    """ARPEGE 0.1° over Europe (global 0.25° variant is ArpegeGlobalFetcher)."""
    name = "mf_arpege"
    resolution = "0.1° · Europe · to 102 h"
    service = "DPPaquetARPEGE"
    model_id = "ARPEGE"
    grid = "0.1"
    product = "productARP"
    ranges = ARPEGE_EU_RANGES
    # ARPEGE Europe packaged domain
    domain = dict(lat_min=20.0, lat_max=72.0, lon_min=-32.0, lon_max=42.0)
    min_publish_lag = timedelta(hours=4)


class ArpegeGlobalFetcher(MeteoFrancePackageFetcher):
    """ARPEGE 0.25° global — the only Météo-France reach to the Caribbean."""
    name = "mf_arpege_global"
    resolution = "0.25° · global · to 102 h"
    service = "DPPaquetARPEGE"
    model_id = "ARPEGE"
    grid = "0.25"
    product = "productARP"
    ranges = ARPEGE_GLOBAL_RANGES
    domain = None  # global
    min_publish_lag = timedelta(hours=4)


class AromeAntillesFetcher(MeteoFrancePackageFetcher):
    """AROME-Outre-mer 2.5 km, Antilles domain — the high-res for Caribbean 600.

    Service/model ids for the overseas AROME are the FIRST thing the smoke
    script confirms — the overseas packages sit under their own DPPaquet
    service and the model id carries a domain suffix. Defaults below are the
    documented convention; override via env if the catalogue differs.
    """
    # Service/model/product pinned from the live portal 2026-07-23:
    #   base .../previnum/DPPaquetAROME-OM/v1, model AROME-OM-ANTIL,
    #   endpoint .../packages/{package}/productOMAN. (The AROME-OM API also
    #   serves GUYANE/INDIEN/POLYN/NCALED models we don't use.)
    name = "mf_arome_antilles"
    resolution = "2.5 km · Antilles · hourly"
    service = os.environ.get("GRIBBO_AROMEOM_SERVICE", "DPPaquetAROME-OM")
    model_id = os.environ.get("GRIBBO_AROMEOM_MODEL", "AROME-OM-ANTIL")
    grid = "0.025"
    product = "productOMAN"
    ranges = AROMEOM_HOURS  # single-hour tokens (\d{3}H), one step per file
    domain = dict(lat_min=11.0, lat_max=20.5, lon_min=-66.0, lon_max=-56.0)
    min_publish_lag = timedelta(hours=3, minutes=30)
