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


def _bbox_tag(cfg: RaceConfig) -> str:
    b = cfg.bbox
    return f"{b.lat_min},{b.lat_max},{b.lon_min},{b.lon_max}"


def check_model(model: str, cfg: RaceConfig, store: RunStore,
                now: datetime | None = None) -> str | None:
    """One polling pass for one model.

    Returns the cycle id fetched this pass, or None if nothing new.
    """
    fetcher = get_fetcher(model)

    for cycle in fetcher.candidate_cycles(now):
        cyc_db = cycle_db(cycle)

        if store.has_complete(model, cyc_db):
            rec = store.get(model, cyc_db)
            if not fetcher.region_subset or rec.bbox_covers(
                    cfg.bbox.lat_min, cfg.bbox.lat_max,
                    cfg.bbox.lon_min, cfg.bbox.lon_max):
                return None  # newest published cycle is on disk and covers us
            # bbox-subset run fetched for a smaller domain (fleet grew):
            # fall through and refetch this cycle with the current domain
            log.info("%s %s on disk covers %s but fleet needs %s — refetching",
                     model, cyc_db, rec.bbox, _bbox_tag(cfg))
        else:
            rec = store.get(model, cyc_db)
            if rec and rec.status == "fetching":
                return None  # another pass is (or died) fetching; see below

            if not fetcher.is_available(cycle, cfg.max_lead_hours):
                continue  # not published yet — try the previous cycle

        dest = cfg.grib_dir(model, cycle_iso(cycle))
        log.info("new %s run detected: %s — fetching", model, cyc_db)
        store.start(model, cyc_db, str(dest), bbox=_bbox_tag(cfg))
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


def obs_and_verify_pass(races: RaceConfig | list[RaceConfig],
                        run_store: RunStore) -> dict:
    """One Phase-2 pass over the fleet: per race, pull shore obs, verify
    the window, refresh confidence scores; then publish one scores.json
    covering every region."""
    from gribbosaurus_rex.obs.sources import fetch_all
    from gribbosaurus_rex.obs.store import ObsStore
    from gribbosaurus_rex.verify import compute_scores, verify_pass

    if isinstance(races, RaceConfig):
        races = [races]
    obs_store = ObsStore(races[0].db_path)
    result = {"new_obs": 0, "new_verifications": 0, "scores": {}}
    for cfg in races:
        try:
            result["new_obs"] += fetch_all(cfg, obs_store)
        except Exception:  # noqa: BLE001
            log.exception("[%s] obs fetch failed", cfg.name)
        try:
            result["new_verifications"] += verify_pass(cfg, run_store, obs_store)
            result["scores"][cfg.name] = compute_scores(cfg, obs_store)
        except Exception:  # noqa: BLE001
            log.exception("[%s] verification pass failed", cfg.name)
    try:
        from gribbosaurus_rex.publish import publish

        result["published"] = str(publish(races, obs_store))
    except Exception:  # noqa: BLE001
        log.exception("scores.json publish failed")
    return result


class Poller(threading.Thread):
    """Background poller used by the API process.

    Fleet-aware: fetches once over the union bbox, then runs obs/verify/
    score per race and publishes one combined scores.json.
    """

    def __init__(self, races: RaceConfig | list[RaceConfig]):
        super().__init__(daemon=True, name="gribbo-poller")
        from gribbosaurus_rex.config import fetch_config

        self.races = [races] if isinstance(races, RaceConfig) else races
        self.fetch_cfg = fetch_config(self.races)
        self.store = RunStore(self.fetch_cfg.db_path)
        self._stop = threading.Event()

    def run(self) -> None:
        log.info("poller started: races=%s models=%s every %d min",
                 [r.name for r in self.races], self.fetch_cfg.models,
                 self.fetch_cfg.poll_minutes)
        while not self._stop.is_set():
            check_all(self.fetch_cfg, self.store)
            obs_and_verify_pass(self.races, self.store)
            self._stop.wait(self.fetch_cfg.poll_minutes * 60)

    def stop(self) -> None:
        self._stop.set()


def watch(races: RaceConfig | list[RaceConfig]) -> None:
    """Foreground polling loop (CLI: python -m gribbosaurus_rex watch)."""
    from gribbosaurus_rex.config import fetch_config

    if isinstance(races, RaceConfig):
        races = [races]
    fetch_cfg = fetch_config(races)
    store = RunStore(fetch_cfg.db_path)
    while True:
        fetched = check_all(fetch_cfg, store)
        new = {m: c for m, c in fetched.items() if c}
        if new:
            log.info("fetched: %s", new)
        obs_and_verify_pass(races, store)
        time.sleep(fetch_cfg.poll_minutes * 60)
