"""Common machinery for model fetchers.

Each fetcher knows, for one weather model:
  * which cycles exist (00/06/12/18 ...)
  * how to check whether a cycle has been published yet (`is_available`)
  * how to download the GRIB files for a cycle (`fetch`)

Run detection is deliberately probe-based rather than schedule-based:
publication times jitter, so we simply ask the server "is cycle X there
yet?" from newest candidate backwards. The poller (scheduler.py) does the
asking on an interval.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from gribbosaurus_rex.config import RaceConfig

log = logging.getLogger("gribbo.fetch")

USER_AGENT = "gribbosaurus-rex/0.2 (marine model verification; personal use)"


def http_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    adapter = requests.adapters.HTTPAdapter(
        max_retries=requests.adapters.Retry(
            total=4, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504)
        )
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def cycle_iso(dt: datetime) -> str:
    """Directory/DB-friendly cycle id: 20260713T00Z."""
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%HZ")


def cycle_db(dt: datetime) -> str:
    """Sortable ISO8601 for the run store."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass
class FetchResult:
    files: list[Path]
    nbytes: int


def model_crop_bbox(name: str, cfg, pad: float = 1.0):
    """Per-model crop bbox = union of the races that use this model (from the
    fleet fetch config), padded. Falls back to the fetch bbox, or None if
    neither is available. Used to slim full-domain downloads to just the race
    areas at fetch time so the per-race decode later is cheap."""
    mb = getattr(cfg, "model_bboxes", None) or {}
    bb = mb.get(name, getattr(cfg, "bbox", None))
    return bb.padded(pad) if bb is not None else None


class BaseFetcher(abc.ABC):
    #: registry key, e.g. "ifs"
    name: str = "?"
    #: human-readable grid resolution, shown in the UI
    resolution: str = "?"
    #: coverage {"lat_min","lat_max","lon_min","lon_max"} or None = global
    domain: dict | None = None
    #: True when downloads are bbox-subset server-side (files on disk only
    #: cover the fetch domain) — such runs refetch when the domain grows
    region_subset: bool = False
    #: full-domain downloads get slimmed + cropped to the race areas at fetch
    #: (regular_ll only; huge decode saving for e.g. ICON-EU's 93 files/run)
    crop_on_fetch: bool = False
    #: hours of the day at which cycles run
    cycle_hours: tuple[int, ...] = (0, 6, 12, 18)
    #: rough minimum delay between cycle time and publication (probe filter)
    min_publish_lag = timedelta(hours=3)
    #: how far back to look for the newest published cycle
    max_lookback = timedelta(hours=30)

    def __init__(self):
        self.http = http_session()

    # -- cycle arithmetic -----------------------------------------------------

    def candidate_cycles(self, now: datetime | None = None) -> list[datetime]:
        """Cycle datetimes that *might* be published, newest first."""
        now = now or datetime.now(timezone.utc)
        newest_possible = now - self.min_publish_lag
        out: list[datetime] = []
        day = newest_possible.date()
        for d in range(3):  # today, yesterday, day before — bounded by max_lookback
            base = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
            for h in sorted(self.cycle_hours, reverse=True):
                c = base + timedelta(hours=h)
                if c <= newest_possible and now - c <= self.max_lookback:
                    out.append(c)
            day -= timedelta(days=1)
        return out

    def steps(self, max_lead_hours: int) -> list[int]:
        """Forecast lead times (hours) to download. Override per model."""
        return [s for s in range(0, max_lead_hours + 1, 3)]

    def next_expected(self, now: datetime | None = None
                      ) -> tuple[datetime, datetime]:
        """(next_cycle, when_we_expect_it_published) — for the UI.

        The next cycle is the first one after the newest possibly-published
        cycle; availability estimate = cycle time + typical publish lag.
        """
        now = now or datetime.now(timezone.utc)
        cands = self.candidate_cycles(now)
        newest = cands[0] if cands else now
        day = newest.date()
        for d in range(3):
            base = datetime.combine(day, datetime.min.time(),
                                    tzinfo=timezone.utc) + timedelta(days=d)
            for h in sorted(self.cycle_hours):
                c = base + timedelta(hours=h)
                if c > newest:
                    return c, c + self.min_publish_lag
        # unreachable with sane cycle_hours; satisfy the type checker
        c = newest + timedelta(hours=6)
        return c, c + self.min_publish_lag

    # -- to implement per model ----------------------------------------------

    @abc.abstractmethod
    def is_available(self, cycle: datetime, max_lead_hours: int | None = None) -> bool:
        """True if this cycle is published out to the horizon we need
        (cheap probe, e.g. HEAD request on the last required step)."""

    @abc.abstractmethod
    def fetch(self, cycle: datetime, cfg: RaceConfig, dest: Path) -> FetchResult:
        """Download all GRIB files for `cycle` into `dest`."""

    # -- shared download helper ----------------------------------------------

    def download(self, url: str, dest: Path, *, headers: dict | None = None,
                 timeout: int = 120) -> int:
        """Stream a URL to `dest` atomically. Returns bytes written."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        with self.http.get(url, stream=True, timeout=timeout, headers=headers) as r:
            r.raise_for_status()
            n = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
                    n += len(chunk)
        tmp.rename(dest)
        log.debug("downloaded %s (%d bytes)", dest.name, n)
        return n

    def head_ok(self, url: str, timeout: int = 20) -> bool:
        try:
            r = self.http.head(url, timeout=timeout, allow_redirects=True)
            if r.status_code == 405:  # some servers dislike HEAD
                r = self.http.get(url, timeout=timeout, stream=True,
                                  headers={"Range": "bytes=0-0"})
            return r.status_code in (200, 206)
        except requests.RequestException:
            return False

    # -- fetch-side slim + crop ----------------------------------------------

    def _crop_keep(self):
        """Optional field filter (eccodes msg_id -> bool) for the fetch crop.
        None keeps every message; override to drop unused fields (e.g. the
        Météo-France packages keep only 10 m wind + MSL)."""
        return None

    def slim_fetched(self, files: list[Path], cfg) -> int:
        """Crop each fetched file to this model's race-area bbox (regular_ll),
        applying `_crop_keep`. Big decode saving for full-domain models.

        No-op unless `crop_on_fetch` is set and a per-model bbox is available.
        Safe: `slim_crop_file` keeps the original file on any error, so a crop
        problem degrades to "slower", never "broken". Returns bytes on disk
        after cropping (so callers can report an honest size)."""
        total = 0
        if not self.crop_on_fetch:
            return sum(f.stat().st_size for f in files)
        bbox = model_crop_bbox(self.name, cfg)
        if bbox is None:
            return sum(f.stat().st_size for f in files)
        from gribbosaurus_rex.export import slim_crop_file
        keep = self._crop_keep()
        before = sum(f.stat().st_size for f in files)
        for f in files:
            try:
                slim_crop_file(f, bbox, keep=keep)
            except Exception:  # noqa: BLE001
                log.warning("crop failed for %s — keeping full file", f.name)
            total += f.stat().st_size
        if before:
            log.info("%s: cropped %d files %.1f -> %.1f MB", self.name,
                     len(files), before / 1e6, total / 1e6)
        return total
