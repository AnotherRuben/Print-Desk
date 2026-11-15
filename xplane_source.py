"""Reads the aircraft from X-Plane 11 or 12, for landing reports.

Nothing to install in X-Plane. Two ways in, tried in this order:

1. UDP datarefs (X-Plane 11 and 12): Print Desk asks X-Plane for each value ("RREF")
   and for the position ("RPOS", with full-precision latitude/longitude), 50 times a
   second. On by default in X-Plane 11; in X-Plane 12 it needs
   Settings > Network > "Accept incoming connections".
2. The Web API (X-Plane 12.1.1 and newer), only if UDP gets no answer. It sends
   updates 10 times a second, so touchdown values are less exact; the report says so.

Runways come from X-Plane's own airport files (apt.dat: Custom Scenery first, in the
order of scenery_packs.ini, then the global airports), so they match the scenery.

Readings have the same keys as the MSFS reader (see landing.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import socket
import struct
import time
from pathlib import Path

import aiohttp

log = logging.getLogger("xplane")

RATE = 50                      # UDP readings per second asked for
QUIET = 5.0                    # seconds without data: ask again
UDP_GIVE_UP = 12.0             # seconds without any UDP answer before trying the Web API
KT = 1.943844                  # m/s to knots

# (dataref, key, conversion)
DATAREFS = [
    ("sim/flightmodel/failures/onground_any", "on_ground", lambda v: v >= 0.5),
    ("sim/flightmodel/position/y_agl", "agl", lambda v: v / 0.3048),
    ("sim/flightmodel/position/vh_ind_fpm", "vs", float),
    ("sim/flightmodel/position/indicated_airspeed", "ias", float),
    ("sim/flightmodel/position/groundspeed", "gs", lambda v: v * KT),
    ("sim/flightmodel/position/theta", "pitch", float),
    ("sim/flightmodel/position/phi", "bank", float),
    ("sim/flightmodel/position/psi", "hdg", lambda v: v % 360),
    ("sim/flightmodel/position/hpath", "trk", lambda v: v % 360),
    ("sim/flightmodel/forces/g_nrml", "g", float),
]
TAILNUM = "sim/aircraft/view/acf_tailnum"      # byte array: the registration
TAIL_LEN = 10
ID_BASE = 100                                  # RREF index of the first tail number byte

WEB_PORT = 8086


def _pos_ok(lat, lon):
    return -90 <= lat <= 90 and -180 <= lon <= 180 and (lat or lon)


class _UDP(asyncio.DatagramProtocol):
    def __init__(self, owner):
        self.owner = owner

    def datagram_received(self, data, addr):
        self.owner._udp_packet(data)


class XPlaneSource:
    """Same calls as simconnect_source.SimSource:
       on_reading(dict), on_status(level, text), on_info({"acid": ...})"""

    def __init__(self, loop, on_reading, on_status, on_info=None, cfg: dict | None = None, http=None):
        self.loop = loop
        self.on_reading, self.on_status, self.on_info = on_reading, on_status, on_info
        self.cfg = cfg or {}
        self.http = http
        self.task = None
        self.connected = False
        self.mode = ""                     # "udp" or "web"
        self.values: dict[str, float] = {}
        self.pos: tuple[float, float] | None = None
        self.tail = [0] * TAIL_LEN
        self.last_packet = 0.0
        self.last_sent = 0.0
        self.transport = None
        self.root: Path | None = None
        self.facilities = True             # runways from apt.dat (see runway_ends)
        self.apt_cache: dict[str, list] = {}

    # ------------------------------------------------------------ start / stop

    def start(self):
        self.task = asyncio.ensure_future(self._run())

    def stop(self):
        if self.task:
            self.task.cancel()
        if self.transport:
            try:
                self._subscribe(0)                         # tell X-Plane to stop sending
            except Exception:
                pass
        self._close_udp()

    def _status(self, level, text):
        if self.on_status:
            self.on_status(level, text)

    async def _run(self):
        said = None
        try:
            while True:
                if await self._udp_session():
                    said = None
                    continue
                if await self._web_session():
                    said = None
                    continue
                if said != "waiting":
                    self._status("warn", "X-Plane: waiting for the sim")
                    said = "waiting"
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass
        finally:
            self._close_udp()
            self.connected = False

    # ------------------------------------------------------------ UDP

    def _target(self):
        return (self.cfg.get("xplane_host") or "127.0.0.1", int(self.cfg.get("xplane_port") or 49000))

    def _close_udp(self):
        if self.transport:
            self.transport.close()
            self.transport = None

    def _subscribe(self, rate):
        tgt = self._target()
        send = self.transport.sendto
        for i, (name, _, _) in enumerate(DATAREFS):
            send(struct.pack("<4sxii400s", b"RREF", rate, i, name.encode()), tgt)
        for j in range(TAIL_LEN):
            send(struct.pack("<4sxii400s", b"RREF", 1 if rate else 0, ID_BASE + j, f"{TAILNUM}[{j}]".encode()), tgt)
        send(b"RPOS\0" + (str(rate).encode() + b"\0"), tgt)

    async def _udp_session(self) -> bool:
        """True after a session that had data (then try again straight away)."""
        try:
            self.transport, _ = await self.loop.create_datagram_endpoint(
                lambda: _UDP(self), local_addr=("0.0.0.0", 0), family=socket.AF_INET)
        except OSError as e:
            log.info("X-Plane UDP: %s", e)
            return False
        self.mode = "udp"
        self.last_packet = 0.0
        started = time.monotonic()
        asked = 0.0
        had_data = False
        try:
            while True:
                now = time.monotonic()
                if now - self.last_packet > QUIET and now - asked > QUIET:
                    self._subscribe(RATE)                  # first time, and again if X-Plane restarted
                    asked = now
                if self.last_packet:
                    if not self.connected:
                        self.connected = had_data = True
                        self._status("ok", "X-Plane: connected")
                    if now - self.last_packet > 30:
                        break                              # gone: start over
                elif now - started > UDP_GIVE_UP:
                    break                                  # never answered
                await asyncio.sleep(0.5)
        finally:
            try:
                self._subscribe(0)                         # tell X-Plane to stop sending
            except Exception:
                pass
            self._close_udp()
            if self.connected:
                self._status("warn", "X-Plane: waiting for the sim")
            self.connected = False
        return had_data

    def _udp_packet(self, data: bytes):
        if data[:4] == b"RREF" and len(data) >= 13:
            for k in range(5, len(data) - 7, 8):
                idx, val = struct.unpack_from("<if", data, k)
                if 0 <= idx < len(DATAREFS):
                    self.values[DATAREFS[idx][1]] = val
                elif ID_BASE <= idx < ID_BASE + TAIL_LEN:
                    self.tail[idx - ID_BASE] = int(val)
                    self._tail_info()
            self.last_packet = time.monotonic()
            self._emit()
        elif data[:4] == b"RPOS" and len(data) >= 5 + 24:
            lon, lat = struct.unpack_from("<dd", data, 5)
            if _pos_ok(lat, lon):
                self.pos = (lat, lon)
            self.last_packet = time.monotonic()

    def _tail_info(self):
        if self.on_info:
            acid = bytes(c for c in self.tail if 32 < c < 127).decode("ascii", "ignore").strip()
            if acid:
                self.on_info({"acid": acid})

    def _emit(self):
        now = time.monotonic()
        if now - self.last_sent < 0.8 / RATE or len(self.values) < len(DATAREFS):
            return
        self.last_sent = now
        r = {"t": now, "td_vs": None}
        for _, key, conv in DATAREFS:
            r[key] = conv(self.values[key])
        if self.pos:
            r["lat"], r["lon"] = self.pos
        self.on_reading(r)

    # ------------------------------------------------------------ Web API (X-Plane 12.1.1+)

    async def _web_session(self) -> bool:
        if not self.http:
            return False
        host = self.cfg.get("xplane_host") or "127.0.0.1"
        base = f"http://{host}:{WEB_PORT}/api"
        try:
            async with self.http.get(f"{base}/capabilities", timeout=aiohttp.ClientTimeout(total=3)) as r:
                if r.status != 200:
                    return False
                caps = await r.json()
            ids = {}
            names = [d[0] for d in DATAREFS] + ["sim/flightmodel/position/latitude",
                                                  "sim/flightmodel/position/longitude", TAILNUM]
            for name in names:
                async with self.http.get(f"{base}/v1/datarefs", params={"filter[name]": name},
                                         timeout=aiohttp.ClientTimeout(total=3)) as r:
                    if r.status != 200:
                        return False                       # no flight loaded yet
                    data = (await r.json()).get("data") or []
                    if data:
                        ids[data[0]["id"]] = name
            if len(ids) < len(DATAREFS):
                return False
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, KeyError):
            return False
        version = (caps.get("x-plane") or {}).get("version", "12")
        had_data = False
        try:
            async with self.http.ws_connect(f"ws://{host}:{WEB_PORT}/api/v1", heartbeat=20) as ws:
                await ws.send_str(json.dumps({"req_id": 1, "type": "dataref_subscribe_values",
                                              "params": {"datarefs": [{"id": i} for i in ids]}}))
                self.mode = "web"
                key_of = {d[0]: (d[1], d[2]) for d in DATAREFS}
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        break
                    m = json.loads(msg.data)
                    if m.get("type") != "dataref_update_values":
                        continue
                    for sid, val in (m.get("data") or {}).items():
                        name = ids.get(int(sid))
                        if name in key_of:
                            self.values[key_of[name][0]] = float(val)
                        elif name == TAILNUM and isinstance(val, (str, list)):
                            raw = val.encode() if isinstance(val, str) else bytes(int(c) & 255 for c in val)
                            self.tail = list(raw[:TAIL_LEN]) + [0] * (TAIL_LEN - len(raw[:TAIL_LEN]))
                            self._tail_info()
                        elif name and name.endswith("latitude"):
                            self.values["_lat"] = float(val)
                        elif name and name.endswith("longitude"):
                            self.values["_lon"] = float(val)
                    if "_lat" in self.values and "_lon" in self.values:
                        self.pos = (self.values["_lat"], self.values["_lon"])
                    if not self.connected:
                        self.connected = had_data = True
                        self._status("ok", f"X-Plane {version}: connected through the Web API (10 per second). "
                                           "For exact touchdowns switch on Settings > Network > Accept incoming connections")
                    self.last_packet = time.monotonic()
                    self.last_sent = 0.0
                    self._emit()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            pass
        finally:
            if self.connected:
                self._status("warn", "X-Plane: waiting for the sim")
            self.connected = False
        return had_data

    @property
    def low_rate(self) -> bool:
        return self.mode == "web"

    # ------------------------------------------------------------ runways from apt.dat

    def xplane_root(self) -> Path | None:
        """The X-Plane folder: from Settings, else from the files X-Plane's installer leaves."""
        if self.cfg.get("xplane_folder"):
            p = Path(self.cfg["xplane_folder"])
            return p if p.exists() else None
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return None
        for name in ("x-plane_install_12.txt", "x-plane_install_11.txt"):
            f = Path(local) / name
            if f.exists():
                for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                    p = Path(line.strip())
                    if line.strip() and p.exists():
                        return p
        return None

    def apt_files(self) -> list[Path]:
        root = self.xplane_root()
        if not root:
            return []
        out = []
        packs = root / "Custom Scenery" / "scenery_packs.ini"
        if packs.exists():
            for line in packs.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("SCENERY_PACK ") and not line.startswith("SCENERY_PACK_DISABLED"):
                    rel = line[len("SCENERY_PACK "):].strip().rstrip("/\\")
                    p = Path(rel) if os.path.isabs(rel) else root / rel
                    f = p / "Earth nav data" / "apt.dat"
                    if f.exists():
                        out.append(f)
        for rel in ("Global Scenery/Global Airports/Earth nav data/apt.dat",
                    "Resources/default scenery/default apt dat/Earth nav data/apt.dat"):
            f = root / rel
            if f.exists():
                out.append(f)
        return out

    async def runway_ends(self, icao: str) -> list[dict] | None:
        icao = icao.upper()
        if icao not in self.apt_cache:
            self.apt_cache[icao] = await asyncio.to_thread(self._find_airport, icao)
        return self.apt_cache[icao] or None

    def _find_airport(self, icao: str) -> list[dict]:
        for f in self.apt_files():
            ends = read_apt(f, icao)
            if ends:
                log.info("runways for %s from %s", icao, f)
                return ends
        return []


def _bearing(lat1, lon1, lat2, lon2):
    y = math.sin(math.radians(lon2 - lon1)) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2)) -
         math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(math.radians(lon2 - lon1)))
    return math.degrees(math.atan2(y, x)) % 360


def _dist(lat1, lon1, lat2, lon2):
    dy = math.radians(lat2 - lat1) * 6371000.0
    dx = math.radians(lon2 - lon1) * 6371000.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dx, dy)


def read_apt(path: Path, icao: str) -> list[dict]:
    """The runway ends of one airport in an apt.dat file. Row 100 is a land runway:
    100 width surface shoulder smoothness centreline-lights edge-lights signs, then per
    end: ident lat lon displaced-threshold(m) blastpad markings approach-lights tdz reil."""
    ends, inside = [], False
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            code = line[:4].strip()
            if code in ("1", "16", "17"):
                if inside:
                    break                                  # next airport: done
                parts = line.split()
                inside = len(parts) >= 5 and parts[4].upper() == icao
                continue
            if code == "99" and inside:
                break
            if not inside or code != "100":
                continue
            p = line.split()
            if len(p) < 26:
                continue
            try:
                width = float(p[1])
                a = (p[8], float(p[9]), float(p[10]), float(p[11]))
                b = (p[17], float(p[18]), float(p[19]), float(p[20]))
            except ValueError:
                continue
            length = _dist(a[1], a[2], b[1], b[2])
            for (ident, lat, lon, disp), other in ((a, b), (b, a)):
                ends.append({"airport": icao, "ident": ident, "lat": lat, "lon": lon,
                             "hdg": _bearing(lat, lon, other[1], other[2]),
                             "length": length, "width": width, "displaced": disp})
    return ends
