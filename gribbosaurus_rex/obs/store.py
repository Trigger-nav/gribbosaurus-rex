"""SQLite store for observations, verifications and confidence scores.

Lives in the same gribbo.sqlite as the run store, three tables:

  obs           one row per observation (yacht, METAR, buoy, ...)
  verification  one row per (obs x model x run): forecast vs observed errors
  scores        confidence score time series per model (feeds the dashboard)

Stdlib-only (sqlite3), matching store/runs.py.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS obs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,          -- yacht | metar | ndbc | openmeteo
    station       TEXT NOT NULL,          -- ICAO id, buoy id, boat name
    lat           REAL NOT NULL,
    lon           REAL NOT NULL,
    time          TEXT NOT NULL,          -- ISO8601 UTC
    wind_speed_kn REAL,
    wind_dir_deg  REAL,
    gust_kn       REAL,
    pressure_hpa  REAL,
    created_at    TEXT NOT NULL,
    UNIQUE (source, station, time)
);
CREATE INDEX IF NOT EXISTS idx_obs_time ON obs (time DESC);
CREATE INDEX IF NOT EXISTS idx_obs_source_time ON obs (source, time DESC);

CREATE TABLE IF NOT EXISTS verification (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    obs_id        INTEGER NOT NULL REFERENCES obs(id),
    model         TEXT NOT NULL,
    cycle         TEXT NOT NULL,          -- run cycle ISO8601 UTC
    lead_hours    REAL NOT NULL,
    fc_wind_speed REAL,
    fc_wind_dir   REAL,
    fc_pressure   REAL,
    err_vector_kn REAL,                   -- |forecast - observed| wind vector
    err_speed_kn  REAL,                   -- signed: forecast - observed
    err_dir_deg   REAL,                   -- circular, unsigned
    err_press_hpa REAL,                   -- signed: forecast - observed
    created_at    TEXT NOT NULL,
    UNIQUE (obs_id, model, cycle)
);
CREATE INDEX IF NOT EXISTS idx_verif_model ON verification (model, created_at DESC);

CREATE TABLE IF NOT EXISTS scores (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    time          TEXT NOT NULL,          -- when the score was computed
    model         TEXT NOT NULL,
    score         REAL NOT NULL,          -- 0..1 confidence
    n_obs         INTEGER NOT NULL,
    rmse_vector_kn REAL,
    mean_dir_err  REAL,
    mean_press_bias REAL,
    UNIQUE (time, model)
);
CREATE INDEX IF NOT EXISTS idx_scores_model_time ON scores (model, time DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Obs:
    id: int
    source: str
    station: str
    lat: float
    lon: float
    time: str
    wind_speed_kn: float | None
    wind_dir_deg: float | None
    gust_kn: float | None
    pressure_hpa: float | None
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Obs":
        return cls(**{k: row[k] for k in row.keys()})


class ObsStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- observations ---------------------------------------------------

    def insert_obs(self, *, source: str, station: str, lat: float, lon: float,
                   time_iso: str, wind_speed_kn: float | None = None,
                   wind_dir_deg: float | None = None,
                   gust_kn: float | None = None,
                   pressure_hpa: float | None = None) -> bool:
        """Insert one observation. Returns True if new, False if duplicate."""
        cur = self._conn.execute(
            """INSERT OR IGNORE INTO obs
               (source, station, lat, lon, time, wind_speed_kn, wind_dir_deg,
                gust_kn, pressure_hpa, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (source, station, lat, lon, time_iso, wind_speed_kn, wind_dir_deg,
             gust_kn, pressure_hpa, _now()),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def recent_obs(self, window_h: float, source: str | None = None) -> list[Obs]:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=window_h)).isoformat(timespec="seconds")
        if source:
            rows = self._conn.execute(
                "SELECT * FROM obs WHERE time >= ? AND source = ? ORDER BY time DESC",
                (cutoff, source)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM obs WHERE time >= ? ORDER BY time DESC",
                (cutoff,)).fetchall()
        return [Obs.from_row(r) for r in rows]

    def yacht_latest(self, max_age_h: float = 6.0) -> Obs | None:
        """Freshest yacht observation (for the distance-weight anchor)."""
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=max_age_h)).isoformat(timespec="seconds")
        row = self._conn.execute(
            """SELECT * FROM obs WHERE source='yacht' AND time >= ?
               ORDER BY time DESC LIMIT 1""", (cutoff,)).fetchone()
        return Obs.from_row(row) if row else None

    # -- verification -----------------------------------------------------

    def has_verification(self, obs_id: int, model: str, cycle: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM verification WHERE obs_id=? AND model=? AND cycle=?",
            (obs_id, model, cycle)).fetchone()
        return row is not None

    def insert_verification(self, *, obs_id: int, model: str, cycle: str,
                            lead_hours: float, fc_wind_speed: float | None,
                            fc_wind_dir: float | None, fc_pressure: float | None,
                            err_vector_kn: float | None,
                            err_speed_kn: float | None,
                            err_dir_deg: float | None,
                            err_press_hpa: float | None) -> None:
        self._conn.execute(
            """INSERT OR IGNORE INTO verification
               (obs_id, model, cycle, lead_hours, fc_wind_speed, fc_wind_dir,
                fc_pressure, err_vector_kn, err_speed_kn, err_dir_deg,
                err_press_hpa, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (obs_id, model, cycle, lead_hours, fc_wind_speed, fc_wind_dir,
             fc_pressure, err_vector_kn, err_speed_kn, err_dir_deg,
             err_press_hpa, _now()),
        )
        self._conn.commit()

    def verifications_window(self, window_h: float) -> list[sqlite3.Row]:
        """Verification rows joined with their obs, inside the window."""
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=window_h)).isoformat(timespec="seconds")
        return self._conn.execute(
            """SELECT v.*, o.source, o.station, o.lat, o.lon, o.time AS obs_time
               FROM verification v JOIN obs o ON o.id = v.obs_id
               WHERE o.time >= ?""", (cutoff,)).fetchall()

    # -- scores -----------------------------------------------------------

    def insert_score(self, *, time_iso: str, model: str, score: float,
                     n_obs: int, rmse_vector_kn: float | None,
                     mean_dir_err: float | None,
                     mean_press_bias: float | None) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO scores
               (time, model, score, n_obs, rmse_vector_kn, mean_dir_err,
                mean_press_bias)
               VALUES (?,?,?,?,?,?,?)""",
            (time_iso, model, score, n_obs, rmse_vector_kn, mean_dir_err,
             mean_press_bias),
        )
        self._conn.commit()

    def latest_scores(self) -> dict[str, float]:
        rows = self._conn.execute(
            """SELECT model, score FROM scores s
               WHERE time = (SELECT MAX(time) FROM scores WHERE model = s.model)
            """).fetchall()
        return {r["model"]: r["score"] for r in rows}

    def score_history(self, model: str | None = None,
                      limit: int = 500) -> list[sqlite3.Row]:
        if model:
            return self._conn.execute(
                "SELECT * FROM scores WHERE model=? ORDER BY time DESC LIMIT ?",
                (model, limit)).fetchall()
        return self._conn.execute(
            "SELECT * FROM scores ORDER BY time DESC LIMIT ?", (limit,)).fetchall()

    def close(self) -> None:
        self._conn.close()
