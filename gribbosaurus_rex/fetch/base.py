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


class BaseFetcher(abc.ABC):
    #: registry key, e.g. "ifs"
    name: str = "?"
    #: True when downloads are bbox-subset server-side (files on disk only
    #: cover the fetch domain) — such runs refetch when the domain grows
    region_subset: bool = False
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
