#!/usr/bin/env python3
"""Phase 2 live smoke test — obs, verification and confidence end-to-end.

Uses the REAL data directory (not a temp dir) so it builds on runs you
already have. Run `python -m gribbosaurus_rex fetch-once` first if the
run store is empty.

    python scripts/live_smoke_phase2.py

Steps:
  1. fetch live METAR observations for the race bbox
  2. verify them against every model run on disk
  3. compute confidence scores + blend weights
  4. (optional) NMEA loopback: feeds simulated sentences through the
     listener and checks a yacht obs lands in the store
"""

from __future__ import annotations

import logging
import socket
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gribbosaurus_rex.config import load_config  # noqa: E402
from gribbosaurus_rex.obs.sources import fetch_metar  # noqa: E402
from gribbosaurus_rex.obs.store import ObsStore  # noqa: E402
from gribbosaurus_rex.store.runs import RunStore  # noqa: E402
from gribbosaurus_rex.verify import blend_weights, compute_scores, verify_pass  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


def nmea(body: str) -> bytes:
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"${body}*{cs:02X}\r\n".encode()


def nmea_loopback(cfg, obs_store) -> bool:
    """Start the listener on a scratch port, fire sentences, expect an obs.

    Uses source="test" (excluded from verification/scoring by design) and
    purges its rows afterwards — smoke data must never pollute confidence.
    """
    from dataclasses import replace

    from gribbosaurus_rex.config import NmeaConfig
    from gribbosaurus_rex.obs.nmea import NmeaListener

    port = 10199
    cfg2 = replace(cfg, obs=replace(cfg.obs, nmea=NmeaConfig(True, "udp", port)))
    listener = NmeaListener(cfg2, obs_store, emit_interval_s=0.0,
                            boat="smoke-boat", source="test")
    listener.start()
    time.sleep(0.5)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(nmea("GPRMC,120000.00,A,3927.0000,N,00236.0000,E,7.5,60.0,130726,2.1,E,A"),
                ("127.0.0.1", port))
    sock.sendto(nmea("WIMWD,70.0,T,67.9,M,12.5,N,6.4,M"), ("127.0.0.1", port))
    sock.sendto(nmea("WIMDA,29.9110,I,1.0128,B,21.5,C,,,,,,,,,,,,,,"),
                ("127.0.0.1", port))
    time.sleep(1.5)
    listener.stop()

    got = [o for o in obs_store.recent_obs(0.1, source="test")
           if o.station == "smoke-boat"]
    obs_store.purge_station("smoke-boat")
    return bool(got)


def main() -> int:
    failures = []
    cfg = load_config()
    run_store = RunStore(cfg.db_path)
    obs_store = ObsStore(cfg.db_path)

    complete = [m for m in cfg.models if run_store.latest_complete(m)]
    print(f"models with runs on disk: {complete or 'NONE'}")
    if not complete:
        print("-> run `python -m gribbosaurus_rex fetch-once` first")
        return 1

    print("\n=== 1. live METAR fetch ===")
    try:
        n = fetch_metar(cfg, obs_store)
        print(f"  {n} new METAR obs")
        recent = obs_store.recent_obs(3, source="metar")
        print(f"  {len(recent)} METAR obs in the last 3h")
        for o in recent[:5]:
            print(f"    {o.station} {o.time} {o.wind_speed_ms}m/s/"
                  f"{o.wind_dir_deg}° {o.pressure_hpa}hPa "
                  f"@({o.lat:.2f},{o.lon:.2f})")
        if not recent:
            failures.append("no METAR obs in window (empty bbox? API change?)")
    except Exception as e:  # noqa: BLE001
        failures.append(f"metar: {e}")

    print("\n=== 2. verification ===")
    try:
        added = verify_pass(cfg, run_store, obs_store)
        print(f"  {added} new forecast-vs-obs comparisons")
        if added == 0 and not obs_store.verifications_window(cfg.scoring.window_h):
            failures.append("no verifications produced")
    except Exception as e:  # noqa: BLE001
        failures.append(f"verify: {e}")

    print("\n=== 3. confidence scores ===")
    try:
        scores = compute_scores(cfg, obs_store)
        for m, s in sorted(scores.items()):
            print(f"  {m:8s} {s:.3f}")
        w = blend_weights(cfg, obs_store)
        if w:
            print(f"  blend weights: " +
                  ", ".join(f"{m}={v:.0%}" for m, v in sorted(w.items())))
        if not scores:
            failures.append("no scores computed")
    except Exception as e:  # noqa: BLE001
        failures.append(f"scores: {e}")

    print("\n=== 3b. scores.json (Stingray artefact) ===")
    try:
        import json

        from gribbosaurus_rex.publish import publish

        path = publish(cfg, obs_store)
        doc = json.loads(path.read_text())
        print(f"  {path} — schema {doc['schema_version']}, "
              f"{len(doc['scores'])} entries")
        for e in doc["scores"][:6]:
            print(f"    {e['model']:16s} lead {e['lead_h_bucket']} "
                  f"score {e['score']} ({e['obs_count']} obs, "
                  f"rmse {e['metrics'].get('wind_rmse_ms')} m/s)")
        if doc["schema_version"] != "1.0":
            failures.append("scores.json wrong schema_version")
        bad_names = [e["model"] for e in doc["scores"]
                     if e["model"] in ("ifs", "gfs", "aifs", "icon_eu")]
        if bad_names:
            failures.append(f"unmapped internal model names published: {bad_names}")
    except Exception as e:  # noqa: BLE001
        failures.append(f"publish: {e}")

    print("\n=== 4. NMEA loopback ===")
    try:
        if nmea_loopback(cfg, obs_store):
            print("  yacht obs received through UDP listener ✓")
        else:
            failures.append("NMEA loopback produced no yacht obs")
    except Exception as e:  # noqa: BLE001
        failures.append(f"nmea: {e}")

    print("\n" + ("💥 FAILURES:\n  " + "\n  ".join(failures) if failures
                  else "🦖 Phase 2 smoke test passed end-to-end."))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
