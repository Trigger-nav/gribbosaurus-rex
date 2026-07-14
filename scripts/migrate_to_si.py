#!/usr/bin/env python3
"""One-time DB migration: knots -> SI (m/s) + smoke-test cleanup.

Contract alignment (docs/integration/DECISIONS.md): internal units are
SI from 2026-07-13. This converts an existing data/gribbo.sqlite in
place — idempotent, safe to run twice.

    python scripts/migrate_to_si.py [path/to/gribbo.sqlite]

Does, when the old columns are present:
  obs:           wind_speed_kn -> wind_speed_ms   (value * 0.514444)
                 gust_kn       -> gust_ms         (value * 0.514444)
  verification:  err_vector_kn -> err_vector_ms   (value * 0.514444)
                 err_speed_kn  -> err_speed_ms    (value * 0.514444)
                 fc_wind_speed                    (value * 0.514444)
  scores:        rmse_vector_kn -> rmse_vector_ms (value * 0.514444)
And always: deletes 'smoke-boat' test rows (pre-guard smoke data) and
their verifications.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

KN_TO_MS = 0.514444


def has_col(conn: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r[1] == col for r in
               conn.execute(f"PRAGMA table_info({table})").fetchall())


def main() -> int:
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        Path(__file__).resolve().parent.parent / "data" / "gribbo.sqlite"
    if not db.exists():
        print(f"no database at {db} — nothing to migrate")
        return 0

    conn = sqlite3.connect(str(db))
    changed = []

    renames = [
        ("obs", "wind_speed_kn", "wind_speed_ms", True),
        ("obs", "gust_kn", "gust_ms", True),
        ("verification", "err_vector_kn", "err_vector_ms", True),
        ("verification", "err_speed_kn", "err_speed_ms", True),
        ("scores", "rmse_vector_kn", "rmse_vector_ms", True),
    ]
    for table, old, new, convert in renames:
        if has_col(conn, table, old):
            conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
            if convert:
                conn.execute(
                    f"UPDATE {table} SET {new} = {new} * ? WHERE {new} IS NOT NULL",
                    (KN_TO_MS,))
            changed.append(f"{table}.{old} -> {new} (converted)")

    # fc_wind_speed kept its name but was knots pre-migration: convert it
    # exactly once, keyed on whether err_vector_kn existed this run.
    if any("err_vector_kn" in c for c in changed):
        conn.execute(
            "UPDATE verification SET fc_wind_speed = fc_wind_speed * ? "
            "WHERE fc_wind_speed IS NOT NULL", (KN_TO_MS,))
        changed.append("verification.fc_wind_speed (converted)")

    # fleet support: scores table gains a race column (idempotent)
    if not has_col(conn, "scores", "race"):
        conn.execute(
            "ALTER TABLE scores ADD COLUMN race TEXT NOT NULL DEFAULT ''")
        # pre-fleet scores belonged to the balearics config
        conn.execute("UPDATE scores SET race='balearics-summer' WHERE race=''")
        changed.append("scores.race column added (existing rows -> "
                       "balearics-summer)")

    # smoke-test pollution cleanup (idempotent)
    n_v = conn.execute(
        """DELETE FROM verification WHERE obs_id IN
           (SELECT id FROM obs WHERE station='smoke-boat')""").rowcount
    n_o = conn.execute("DELETE FROM obs WHERE station='smoke-boat'").rowcount
    if n_o or n_v:
        changed.append(f"purged smoke-boat: {n_o} obs, {n_v} verifications")

    conn.commit()
    conn.close()

    if changed:
        print(f"migrated {db}:")
        for c in changed:
            print(f"  - {c}")
        print("\nNow re-score in SI: python -m gribbosaurus_rex verify-once")
    else:
        print(f"{db} already SI — nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main())
