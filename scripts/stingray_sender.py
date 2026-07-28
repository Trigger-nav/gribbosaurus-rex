#!/usr/bin/env python3
"""Stingray boat-side sender — NMEA in, HTTPS out. Runs on the Expedition PC.

Listens for NMEA 0183 on UDP (Expedition: Settings -> Instruments ->
add an output connection -> UDP to 127.0.0.1:10110, sentences RMC, MWD,
MDA; MWV+HDT also understood), condenses to one observation per minute,
queues it on disk, and POSTs batches to the gribbo server over TLS.

Built for flaky 4G: everything queues locally when offline and uploads
when the link returns; the server ignores duplicate (station, time) rows,
so retrying the whole queue is always safe. Stdlib only — no pip needed.

    python stingray_sender.py --token YOURTOKEN
    python stingray_sender.py --token YOURTOKEN --server https://... \
        --station stingray --listen-port 10110 --emit 60 --post 60

The token must match GRIBBO_YACHT_TOKEN in the server's /etc/gribbo/env.
Set it via --token or the GRIBBO_YACHT_TOKEN environment variable.
Windows: run in a console, or pythonw + Task Scheduler for headless.

The NMEA parsing below mirrors gribbosaurus_rex/obs/nmea.py — keep the
two in sync if sentence handling changes.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MS_TO_KN = 1.943844
KN_TO_MS = 0.514444
KMH_TO_KN = 0.539957
QUEUE_MAX = 20000        # ~2 weeks at 1/min — cap the disk queue
BATCH = 400              # obs per POST (server cap is 500)


# ---------------------------------------------------------------- NMEA parse
# (mirrors gribbosaurus_rex/obs/nmea.py)

def checksum_ok(sentence: str) -> bool:
    sentence = sentence.strip()
    if not sentence.startswith(("$", "!")) or "*" not in sentence:
        return False
    body, _, cs = sentence[1:].partition("*")
    try:
        want = int(cs[:2], 16)
    except ValueError:
        return False
    got = 0
    for ch in body:
        got ^= ord(ch)
    return got == want


def _dm_to_deg(dm: str, hemi: str) -> float | None:
    if not dm:
        return None
    try:
        dot = dm.index(".") if "." in dm else len(dm)
        deg = float(dm[: dot - 2])
        minutes = float(dm[dot - 2:])
        val = deg + minutes / 60.0
        return -val if hemi in ("S", "W") else val
    except (ValueError, IndexError):
        return None


class NmeaState:
    def __init__(self):
        self.lat = self.lon = None
        self.pos_at = 0.0
        self.twd = self.tws_kn = None
        self.wind_at = 0.0
        self.press_hpa = None
        self.press_at = 0.0
        self.hdg = None

    def handle(self, line: str) -> None:
        if not checksum_ok(line):
            return
        body = line.strip()[1:].split("*")[0]
        f = body.split(",")
        typ = f[0][-3:]
        now = time.monotonic()
        try:
            if typ == "RMC" and len(f) >= 7 and f[2] == "A":
                lat = _dm_to_deg(f[3], f[4])
                lon = _dm_to_deg(f[5], f[6])
                if lat is not None and lon is not None:
                    self.lat, self.lon, self.pos_at = lat, lon, now
            elif typ == "MWD" and len(f) >= 6:
                if f[1] and f[2] == "T":
                    self.twd = float(f[1]) % 360
                if len(f) >= 7 and f[5] and f[6] == "N":
                    self.tws_kn = float(f[5])
                self.wind_at = now
            elif typ == "HDT" and len(f) >= 3 and f[1] and f[2] == "T":
                self.hdg = float(f[1]) % 360
            elif typ == "MWV" and len(f) >= 6 and f[5].startswith("A"):
                if f[2] == "T" and f[1] and self.hdg is not None:
                    self.twd = (self.hdg + float(f[1])) % 360
                    spd = float(f[3]) if f[3] else None
                    if spd is not None:
                        unit = f[4]
                        if unit == "N":
                            self.tws_kn = spd
                        elif unit == "M":
                            self.tws_kn = spd * MS_TO_KN
                        elif unit == "K":
                            self.tws_kn = spd * KMH_TO_KN
                    self.wind_at = now
            elif typ == "MDA" and len(f) >= 5 and f[3] and f[4] == "B":
                self.press_hpa = float(f[3]) * 1000.0
                self.press_at = now
            elif typ == "XDR":
                for i in range(1, len(f) - 3, 4):
                    if f[i] == "P" and f[i + 2] == "B" and f[i + 1]:
                        self.press_hpa = float(f[i + 1]) * 1000.0
                        self.press_at = now
        except (ValueError, IndexError):
            pass

    def snapshot(self, max_age_s: float = 120.0):
        """Position-fresh obs dict, or None. Wind/baro included if fresh —
        a position-only fix is still worth sending (it re-anchors the
        server's distance weighting even without wind)."""
        now = time.monotonic()
        if self.lat is None or now - self.pos_at > max_age_s:
            return None
        ob = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "lat": round(self.lat, 6), "lon": round(self.lon, 6)}
        if self.tws_kn is not None and now - self.wind_at <= max_age_s:
            ob["tws_kn"] = round(self.tws_kn, 2)
            if self.twd is not None:
                ob["twd_deg"] = round(self.twd, 1)
        if self.press_hpa is not None and now - self.press_at <= max_age_s * 3:
            ob["baro_hpa"] = round(self.press_hpa, 1)
        return ob


# ---------------------------------------------------------------- disk queue

class Queue:
    """Append-only jsonl on disk; survives restarts and offline stretches."""

    def __init__(self, path: Path):
        self.path = path
        self.items: list[dict] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    self.items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            self.items = self.items[-QUEUE_MAX:]

    def push(self, ob: dict) -> None:
        self.items.append(ob)
        if len(self.items) > QUEUE_MAX:
            self.items = self.items[-QUEUE_MAX:]
            self._rewrite()
        else:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(ob) + "\n")

    def pop_batch(self, n: int) -> list[dict]:
        return self.items[:n]

    def ack(self, n: int) -> None:
        self.items = self.items[n:]
        self._rewrite()

    def _rewrite(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for ob in self.items:
                fh.write(json.dumps(ob) + "\n")
        tmp.replace(self.path)


# ---------------------------------------------------------------- uploader

def post_batch(server: str, token: str, station: str,
               batch: list[dict], timeout: float = 30.0) -> dict:
    req = urllib.request.Request(
        server.rstrip("/") + "/obs/yacht",
        data=json.dumps({"station": station, "obs": batch}).encode(),
        headers={"Content-Type": "application/json",
                 "X-Gribbo-Token": token},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--server",
                    default="https://gribbo.stingraymarinetechnology.com")
    ap.add_argument("--token", default=os.environ.get("GRIBBO_YACHT_TOKEN"))
    ap.add_argument("--station", default="stingray")
    ap.add_argument("--listen-port", type=int, default=10110)
    ap.add_argument("--emit", type=float, default=60.0,
                    help="seconds between queued observations")
    ap.add_argument("--post", type=float, default=60.0,
                    help="seconds between upload attempts")
    ap.add_argument("--queue-file", default="stingray_queue.jsonl")
    args = ap.parse_args()
    if not args.token:
        print("no token: pass --token or set GRIBBO_YACHT_TOKEN",
              file=sys.stderr)
        return 2

    state = NmeaState()
    queue = Queue(Path(args.queue_file))
    print(f"listening UDP :{args.listen_port} -> {args.server} "
          f"(queued: {len(queue.items)})")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", args.listen_port))
    sock.settimeout(1.0)

    last_emit = last_post = 0.0
    backoff = args.post
    while True:
        try:
            data, _ = sock.recvfrom(4096)
            for line in data.decode("ascii", errors="ignore").splitlines():
                state.handle(line)
        except socket.timeout:
            pass
        except KeyboardInterrupt:
            print("bye")
            return 0

        now = time.monotonic()
        if now - last_emit >= args.emit:
            ob = state.snapshot()
            if ob is not None:
                queue.push(ob)
                last_emit = now
                print(f"queued {ob['time']} "
                      f"{ob.get('tws_kn', '-')}kn @{ob.get('twd_deg', '-')} "
                      f"(queue: {len(queue.items)})")

        if queue.items and now - last_post >= backoff:
            last_post = now
            batch = queue.pop_batch(BATCH)
            try:
                r = post_batch(args.server, args.token, args.station, batch)
                queue.ack(len(batch))
                backoff = args.post
                print(f"sent {len(batch)}: +{r.get('accepted', 0)} new, "
                      f"{r.get('duplicates', 0)} dup, "
                      f"{r.get('n_rejected', 0)} rejected "
                      f"(queue: {len(queue.items)})")
            except urllib.error.HTTPError as e:
                if e.code in (401, 422, 503):
                    print(f"server refused ({e.code}): {e.read().decode()!r}",
                          file=sys.stderr)
                    if e.code == 422:
                        queue.ack(len(batch))   # bad batch: drop, don't loop
                else:
                    print(f"HTTP {e.code}; retrying", file=sys.stderr)
                backoff = min(backoff * 2, 600)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                print(f"offline ({e}); queue holds {len(queue.items)}",
                      file=sys.stderr)
                backoff = min(backoff * 2, 600)


if __name__ == "__main__":
    raise SystemExit(main())
