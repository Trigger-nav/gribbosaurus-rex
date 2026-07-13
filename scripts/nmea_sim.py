#!/usr/bin/env python3
"""NMEA 0183 simulator — pretend to be the yacht, ashore.

Sends realistic RMC/MWD/MDA sentences over UDP so the listener can be
tested end-to-end without a boat. The simulated yacht reaches around
Palma Bay in a ~12kn ENE breeze with slow wind oscillations.

    python scripts/nmea_sim.py [--port 10110] [--host 127.0.0.1] [--hz 1]
"""

from __future__ import annotations

import argparse
import math
import socket
import time
from datetime import datetime, timezone


def nmea(body: str) -> bytes:
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"${body}*{cs:02X}\r\n".encode("ascii")


def deg_to_dm(value: float, is_lat: bool) -> tuple[str, str]:
    hemi = ("N" if value >= 0 else "S") if is_lat else ("E" if value >= 0 else "W")
    v = abs(value)
    deg = int(v)
    minutes = (v - deg) * 60
    width = 2 if is_lat else 3
    return f"{deg:0{width}d}{minutes:07.4f}", hemi


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=10110)
    ap.add_argument("--hz", type=float, default=1.0)
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    lat, lon = 39.45, 2.60
    heading = 60.0
    t0 = time.time()
    print(f"simulating yacht -> udp://{args.host}:{args.port}  (Ctrl-C to stop)")

    while True:
        el = time.time() - t0
        # gentle reaching track, tack every 5 minutes
        if int(el) % 600 < 300:
            heading = 60.0
        else:
            heading = 240.0
        speed_kn = 7.5 + 0.5 * math.sin(el / 30)
        lat += speed_kn / 3600 / 60 * math.cos(math.radians(heading))
        lon += (speed_kn / 3600 / 60 * math.sin(math.radians(heading))
                / math.cos(math.radians(lat)))

        twd = (70 + 12 * math.sin(el / 180)) % 360          # oscillating ENE
        tws = 12 + 2.5 * math.sin(el / 240) + 0.8 * math.sin(el / 17)
        baro = 1.0126 + 0.0002 * math.sin(el / 1800)         # bars

        now = datetime.now(timezone.utc)
        hhmmss = now.strftime("%H%M%S.00")
        ddmmyy = now.strftime("%d%m%y")
        lat_dm, ns = deg_to_dm(lat, True)
        lon_dm, ew = deg_to_dm(lon, False)

        sock.sendto(nmea(
            f"GPRMC,{hhmmss},A,{lat_dm},{ns},{lon_dm},{ew},"
            f"{speed_kn:.1f},{heading:.1f},{ddmmyy},2.1,E,A"),
            (args.host, args.port))
        sock.sendto(nmea(
            f"WIMWD,{twd:.1f},T,{(twd - 2.1) % 360:.1f},M,"
            f"{tws:.1f},N,{tws * 0.514444:.1f},M"),
            (args.host, args.port))
        sock.sendto(nmea(
            f"WIMDA,{baro * 29.5301:.4f},I,{baro:.4f},B,21.5,C,,,,,,,,,,,,,,"),
            (args.host, args.port))

        time.sleep(1.0 / args.hz)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")
