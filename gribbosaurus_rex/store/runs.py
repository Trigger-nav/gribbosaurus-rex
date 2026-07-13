"""SQLite store for model-run metadata.

One row per (model, cycle). This is what the front end reads to show
"new run available" and what the extractor uses to find the newest
complete GRIB set on disk.

Stdlib-only (sqlite3) so it works everywhere and is trivially testable.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    model       TEXT NOT NULL,
    cycle       TEXT NOT NULL,          -- ISO8601 UTC, e.g. 2026-07-13T00:00:00+00:00
    status      TEXT NOT NULL,          -- fetching | complete | failed
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    n_files     INTEGER DEFAULT 0,
    bytes       INTEGER DEFAULT 0,
    path        TEXT,
    message     TEXT,
    UNIQUE (model, cycle)
);
CREATE INDEX IF NOT EXISTS idx_runs_model_cycle ON runs (model, cycle DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RunRecord:
    id: int
    model: str
    cycle: str
    status: str
    started_at: str
    finished_at: str | None
    n_files: int
    bytes: int
    path: str | None
    message: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "RunRecord":
        return cls(**{k: row[k] for k in row.keys()})


class RunStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- lifecycle of a fetch -------------------------------------------------

    def start(self, model: str, cycle: str, path: str) -> None:
        """Register a fetch attempt (idempotent: re-attempts reset the row)."""
        self._conn.execute(
            """INSERT INTO runs (model, cycle, status, started_at, path)
               VALUES (?, ?, 'fetching', ?, ?)
               ON CONFLICT (model, cycle) DO UPDATE SET
                 status='fetching', started_at=excluded.started_at,
                 finished_at=NULL, message=NULL, path=excluded.path""",
            (model, cycle, _now(), path),
        )
        self._conn.commit()

    def complete(self, model: str, cycle: str, n_files: int, nbytes: int) -> None:
        self._conn.execute(
            """UPDATE runs SET status='complete', finished_at=?, n_files=?, bytes=?
               WHERE model=? AND cycle=?""",
            (_now(), n_files, nbytes, model, cycle),
        )
        self._conn.commit()

    def fail(self, model: str, cycle: str, message: str) -> None:
        self._conn.execute(
            """UPDATE runs SET status='failed', finished_at=?, message=?
               WHERE model=? AND cycle=?""",
            (_now(), message[:500], model, cycle),
        )
        self._conn.commit()

    # -- queries --------------------------------------------------------------

    def get(self, model: str, cycle: str) -> RunRecord | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE model=? AND cycle=?", (model, cycle)
        ).fetchone()
        return RunRecord.from_row(row) if row else None

    def has_complete(self, model: str, cycle: str) -> bool:
        rec = self.get(model, cycle)
        return rec is not None and rec.status == "complete"

    def latest_complete(self, model: str) -> RunRecord | None:
        row = self._conn.execute(
            """SELECT * FROM runs WHERE model=? AND status='complete'
               ORDER BY cycle DESC LIMIT 1""",
            (model,),
        ).fetchone()
        return RunRecord.from_row(row) if row else None

    def list_runs(self, model: str | None = None, limit: int = 50) -> list[RunRecord]:
        if model:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE model=? ORDER BY cycle DESC LIMIT ?",
                (model, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY cycle DESC, model LIMIT ?", (limit,)
            ).fetchall()
        return [RunRecord.from_row(r) for r in rows]

    # -- housekeeping ---------------------------------------------------------

    def prune(self, model: str, keep: int) -> list[str]:
        """Delete rows + GRIB directories beyond the newest `keep` complete runs.

        Returns the cycles that were pruned.
        """
        rows = self._conn.execute(
            """SELECT cycle, path FROM runs WHERE model=? AND status='complete'
               ORDER BY cycle DESC""",
            (model,),
        ).fetchall()
        pruned = []
        for row in rows[keep:]:
            if row["path"]:
                shutil.rmtree(row["path"], ignore_errors=True)
            self._conn.execute(
                "DELETE FROM runs WHERE model=? AND cycle=?", (model, row["cycle"])
            )
            pruned.append(row["cycle"])
        self._conn.commit()
        return pruned

    def close(self) -> None:
        self._conn.close()
