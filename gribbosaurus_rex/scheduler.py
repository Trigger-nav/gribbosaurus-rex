"""Run detection and fetching.

`check_model` / `check_all` implement one polling pass:
  for each model, walk candidate cycles newest-first, and fetch the newest
  published cycle we don't already have. `Poller` runs passes on an
  interval in a background thread (used by the API), and `watch()` runs
  them in the foreground (CLI).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from gribbosaurus_rex.config import RaceConfig
from gribbosaurus_rex.fetch.base import cycle_db, cycle_iso
from gribbosaurus_rex.fetch.registry import get_fetcher
from gribbosaurus_rex.store.runs import RunStore

log = logging.getLogger("gribbo.scheduler")


def check_model(model: str, cfg: RaceConfig, store: RunStore,
                now: datetime | None = None) -> str | None:
    """One polling pass for one model.

    Returns the cycle id fetched this pass, or None if nothing new.
    """
    fetcher = get_fetcher(model)

    for cycle in fetcher.candidate_cycles(now):
        cyc_db = cycle_db(cycle)

        if store.has_complete(model, cyc_db):
            return None  # newest published cycle is already on disk

        rec = store.get(model, cyc_db)
        if rec and rec.status == "fetching":
            return None  # another pass is (or died while) fetching; see note below

        if not fetcher.is_available(cycle, cfg.max_lead_hours):
            continue  # not published yet — try the previous cycle

        dest = cfg.grib_dir(model, cycle_iso(cycle))
        log.info("new %s run detected: %s — fetching", model, cyc_db)
        store.start(model, cyc_db, str(dest))
        try:
            result = fetcher.fetch(cycle, cfg, dest)
        except Exception as e:  # noqa: BLE001 — record and move on
            log.exception("fetch failed: %s %s", model, cyc_db)
            store.fail(model, cyc_db, f"{type(e).__name__}: {e}")
            return None
        store.complete(model, cyc_db, len(result.files), result.nbytes)
        store.prune(model, cfg.keep_runs)
        return cyc_db

    return None


def recover_stale(store: RunStore, max_age_s: int = 3600) -> None:
    """Mark 'fetching' rows older than max_age_s as failed (crashed fetches)."""
    for rec in store.list_runs(limit=200):
        if rec.status != "fetching":
            continue
        started = datetime.fromisoformat(rec.started_at)
        age = (datetime.now(timezone.utc) - started).total_seconds()
        if age > max_age_s:
            store.fail(rec.model, rec.cycle, "stale fetch recovered by poller")


def check_all(cfg: RaceConfig, store: RunStore) -> dict[str, str | None]:
    recover_stale(store)
    results = {}
    for model in cfg.models:
        try:
            results[model] = check_model(model, cfg, store)
        except Exception:  # noqa: BLE001
            log.exception("poll pass failed for %s", model)
            results[model] = None
    return results


class Poller(threading.Thread):
    """Background poller used by the API process."""

    def __init__(self, cfg: RaceConfig):
        super().__init__(daemon=True, name="gribbo-poller")
        self.cfg = cfg
        self.store = RunStore(cfg.db_path)
        self._stop = threading.Event()

    def run(self) -> None:
        log.info("poller started: models=%s every %d min",
                 self.cfg.models, self.cfg.poll_minutes)
        while not self._stop.is_set():
            check_all(self.cfg, self.store)
            self._stop.wait(self.cfg.poll_minutes * 60)

    def stop(self) -> None:
        self._stop.set()


def watch(cfg: RaceConfig) -> None:
    """Foreground polling loop (CLI: python -m gribbosaurus_rex watch)."""
    store = RunStore(cfg.db_path)
    while True:
        fetched = check_all(cfg, store)
        new = {m: c for m, c in fetched.items() if c}
        if new:
            log.info("fetched: %s", new)
        time.sleep(cfg.poll_minutes * 60)
