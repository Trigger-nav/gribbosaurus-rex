"""NMEA 0183 listener — live yacht observations over UDP/TCP.

Point Expedition (or any nav software / multiplexer) at this machine:
UDP broadcast to port 10110 is the zero-config default. We parse:

  RMC  position + UTC time            MWD  true wind direction + speed
  MDA  barometric pressure            XDR  pressure transducer (fallback)
  HDT  true heading                   MWV  (T) wind angle -> direction via HDT

A yacht obs row is written at most once per `emit_interval_s`, whenever
we hold a fresh position + true wind. Sentences with bad checksums are
dropped. Masthead speeds are taken as-is for now — calibration/upwash
corrections belong to the Phase 4 transform work.

Test ashore with:  python scripts/nmea_sim.py
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from datetime import datetime, timezone

from gribbosaurus_rex.config import RaceConfig
from gribbosaurus_rex.obs.store import ObsStore

log = logging.getLogger("gribbo.nmea")

MS_TO_KN = 1.943844
KN_TO_MS = 0.514444
KMH_TO_KN = 0.539957


def checksum_ok(sentence: str) -> bool:
    """Validate `$...*hh`. Sentences without a checksum are rejected."""
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
    """ddmm.mmm / dddmm.mmm + N/S/E/W -> signed decimal degrees."""
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
    """Latest decoded values + their wall-clock arrival times."""

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
        typ = f[0][-3:]  # talker-agnostic: GPRMC/IIRMC/... -> RMC
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
                # true-referenced MWV + known heading -> direction
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
                # $--XDR,P,1.0132,B,Baro,...  (groups of 4)
                for i in range(1, len(f) - 3, 4):
                    if f[i] == "P" and f[i + 2] == "B" and f[i + 1]:
                        self.press_hpa = float(f[i + 1]) * 1000.0
                        self.press_at = now
        except (ValueError, IndexError):
            log.debug("unparseable NMEA: %r", line)

    def snapshot(self, max_age_s: float = 120.0):
        """(lat, lon, tws, twd, press) if position+wind are fresh, else None."""
        now = time.monotonic()
        if self.lat is None or now - self.pos_at > max_age_s:
            return None
        if self.tws_kn is None or now - self.wind_at > max_age_s:
            return None
        press = self.press_hpa if now - self.press_at <= max_age_s * 3 else None
        return (self.lat, self.lon, self.tws_kn, self.twd, press)


class NmeaListener(threading.Thread):
    def __init__(self, cfg: RaceConfig, store: ObsStore,
                 emit_interval_s: float = 60.0, boat: str = "yacht",
                 source: str = "yacht"):
        super().__init__(daemon=True, name="gribbo-nmea")
        self.cfg = cfg
        self.store = store
        self.emit_interval_s = emit_interval_s
        self.boat = boat
        # Smoke/loopback tests pass source="test": stored and visible, but
        # excluded from verification and scoring (never pollutes confidence).
        self.source = source
        self.state = NmeaState()
        self._stop = threading.Event()
        self._last_emit = 0.0

    def _maybe_emit(self) -> None:
        now = time.monotonic()
        if now - self._last_emit < self.emit_interval_s:
            return
        snap = self.state.snapshot()
        if snap is None:
            return
        lat, lon, tws_kn, twd, press = snap
        self._last_emit = now
        self.store.insert_obs(
            source=self.source, station=self.boat, lat=lat, lon=lon,
            time_iso=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            wind_speed_ms=tws_kn * KN_TO_MS,  # NMEA is knots; store SI
            wind_dir_deg=twd, pressure_hpa=press)
        log.info("yacht obs: %.4f,%.4f %skn @%s %s",
                 lat, lon, round(tws_kn, 1), twd, press or "-")

    def run(self) -> None:
        transport = self.cfg.obs.nmea.transport
        port = self.cfg.obs.nmea.port
        log.info("NMEA listener starting: %s :%d", transport, port)
        if transport == "udp":
            self._run_udp(port)
        else:
            self._run_tcp(port)

    def _run_udp(self, port: int) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        sock.settimeout(1.0)
        while not self._stop.is_set():
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            for line in data.decode("ascii", errors="ignore").splitlines():
                self.state.handle(line)
            self._maybe_emit()
        sock.close()

    def _run_tcp(self, port: int) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(1)
        srv.settimeout(1.0)
        buf = b""
        while not self._stop.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            log.info("NMEA TCP client connected: %s", addr)
            conn.settimeout(1.0)
            with conn:
                while not self._stop.is_set():
                    try:
                        chunk = conn.recv(4096)
                    except socket.timeout:
                        self._maybe_emit()
                        continue
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self.state.handle(line.decode("ascii", errors="ignore"))
                    self._maybe_emit()
        srv.close()

    def stop(self) -> None:
        self._stop.set()
