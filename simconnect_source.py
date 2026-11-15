"""Reads the aircraft from MSFS (2020 or 2024) through SimConnect, for landing reports.

SimConnect is the sim's own add-on interface: nothing extra to install in MSFS.
Print Desk asks MSFS for all values at once, every sim frame, in a background
thread. Each set of values becomes a reading for LandingWatcher.

Which SimConnect.dll: the first one found of
  - a SimConnect.dll you put in the Print Desk folder
  - the free MSFS SDK's (MSFS 2020 SDK, then MSFS 2024 SDK)
  - the one in the "SimConnect" Python package (installed by install.bat)
The package's DLL is older and can't ask for the scenery's runways, so with it the
landing report uses the OurAirports runway data instead.
"""
from __future__ import annotations

import asyncio
import ctypes
import queue
import struct
import importlib.util
import logging
import os
import sys
import threading
import time

log = logging.getLogger("simconnect")

# (simvar, unit, key, how to turn it into the reading's value)
VARS = [
    ("SIM ON GROUND", "Bool", "on_ground", lambda v: v >= 0.5),
    ("PLANE ALT ABOVE GROUND", "feet", "agl", float),
    ("VERTICAL SPEED", "feet per minute", "vs", float),
    ("AIRSPEED INDICATED", "knots", "ias", float),
    ("GROUND VELOCITY", "knots", "gs", float),
    ("PLANE PITCH DEGREES", "degrees", "pitch", lambda v: -v),       # MSFS: negative is nose up
    ("PLANE BANK DEGREES", "degrees", "bank", float),
    ("PLANE HEADING DEGREES TRUE", "degrees", "hdg", lambda v: v % 360),
    ("GPS GROUND TRUE TRACK", "degrees", "trk", lambda v: v % 360),
    ("G FORCE", "GForce", "g", float),
    ("PLANE TOUCHDOWN NORMAL VELOCITY", "feet per second", "td_vs", lambda v: -abs(v) * 60),
    ("PLANE LATITUDE", "degrees", "lat", float),
    ("PLANE LONGITUDE", "degrees", "lon", float),
]

RECV_EXCEPTION, RECV_OPEN, RECV_QUIT, RECV_SIMOBJECT_DATA = 1, 2, 3, 8
DATATYPE_FLOAT64, DATATYPE_STRING32 = 4, 6
PERIOD_SIM_FRAME, PERIOD_SECOND = 3, 4
OBJECT_USER = 0
DEF_NUMBERS, DEF_ID = 1, 2
REQ_NUMBERS, REQ_ID = 1, 2
DATA_OFFSET = 40          # SIMCONNECT_RECV (12 bytes) + 7 DWORDs, then the values
MAX_RATE = 100.0          # readings per second passed on, at most (normally every sim frame)
DEF_AIRPORT = 3
FAC_REQ_FIRST = 1000      # facility request IDs start here (away from the data requests)
FAC_TIMEOUT = 6.0         # seconds to wait for an airport's runways
FAC_AIRPORT, FAC_RUNWAY = 0, 1   # SIMCONNECT_FACILITY_DATA_TYPE
RUNWAY_FIELDS = [("LATITUDE", "d"), ("LONGITUDE", "d"), ("HEADING", "f"), ("LENGTH", "f"), ("WIDTH", "f"),
                 ("PRIMARY_NUMBER", "i"), ("PRIMARY_DESIGNATOR", "i"),
                 ("SECONDARY_NUMBER", "i"), ("SECONDARY_DESIGNATOR", "i")]
RUNWAY_FMT = "<" + "".join(t for _, t in RUNWAY_FIELDS)
RUNWAY_SIZE = struct.calcsize(RUNWAY_FMT)


class RECV(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_uint32), ("dwVersion", ctypes.c_uint32), ("dwID", ctypes.c_uint32)]


HERE = os.path.dirname(os.path.abspath(__file__))


def dll_candidates() -> list[str]:
    out = [os.path.join(HERE, "SimConnect.dll")]
    for env, default in (("MSFS_SDK", r"C:\MSFS SDK"), ("MSFS2024_SDK", r"C:\MSFS 2024 SDK")):
        for root in (os.environ.get(env), default):
            if root:
                out.append(os.path.join(root, "SimConnect SDK", "lib", "SimConnect.dll"))
    return out


def dll_path() -> str | None:
    """The SimConnect.dll to use (see the top of this file)."""
    for p in dll_candidates():
        if os.path.exists(p):
            return p
    spec = importlib.util.find_spec("SimConnect")
    if spec and spec.submodule_search_locations:
        for folder in spec.submodule_search_locations:
            p = os.path.join(folder, "SimConnect.dll")
            if os.path.exists(p):
                return p
    return None


class SimSource:
    """Background reader. Calls (in the asyncio loop):
       on_reading(dict)        about MAX_RATE times a second while connected
       on_status(level, text)  connected / waiting for MSFS / problems
       on_info(dict)           the aircraft's ATC ID now and then"""

    def __init__(self, loop, on_reading, on_status, on_info=None):
        self.loop = loop
        self.on_reading, self.on_status, self.on_info = on_reading, on_status, on_info
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None
        self.facilities = False          # True while connected with a DLL that can read the scenery's runways
        self.fac_queue: queue.Queue = queue.Queue()

    async def airport_runways(self, icao: str) -> list[dict] | None:
        """The airport's runways from the sim's scenery: dicts with lat, lon (centre),
        hdg (true), length, width (metres), p_num, p_des, s_num, s_des. None if the
        sim can't tell (older DLL, not connected, unknown airport, no answer)."""
        if not self.facilities:
            return None
        fut = self.loop.create_future()
        self.fac_queue.put((icao.upper(), fut))
        try:
            return await asyncio.wait_for(fut, FAC_TIMEOUT + 2)
        except asyncio.TimeoutError:
            return None

    def _answer(self, fut, value):
        def done():
            if not fut.done():
                fut.set_result(value)
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(done)

    def _call(self, fn, *args):
        if fn and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(fn, *args)

    def start(self):
        if sys.platform != "win32":
            self._call(self.on_status, "off", "MSFS: only on Windows")
            return
        path = dll_path()
        if not path:
            self._call(self.on_status, "error", "MSFS: run install.bat once more (SimConnect is missing)")
            return
        self.thread = threading.Thread(target=self._run, args=(path,), daemon=True, name="simconnect")
        self.thread.start()

    def stop(self):
        self.stopping.set()

    # ------------------------------------------------------------ the thread

    def _run(self, path):
        dll = ctypes.WinDLL(path)
        HANDLE = ctypes.c_void_p
        dll.SimConnect_Open.argtypes = [ctypes.POINTER(HANDLE), ctypes.c_char_p, ctypes.c_void_p,
                                        ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32]
        dll.SimConnect_Open.restype = ctypes.c_long
        dll.SimConnect_Close.argtypes = [HANDLE]
        dll.SimConnect_Close.restype = ctypes.c_long
        dll.SimConnect_AddToDataDefinition.argtypes = [HANDLE, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_char_p,
                                                       ctypes.c_int, ctypes.c_float, ctypes.c_uint32]
        dll.SimConnect_AddToDataDefinition.restype = ctypes.c_long
        dll.SimConnect_RequestDataOnSimObject.argtypes = [HANDLE, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                                                          ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32,
                                                          ctypes.c_uint32, ctypes.c_uint32]
        dll.SimConnect_RequestDataOnSimObject.restype = ctypes.c_long
        dll.SimConnect_GetNextDispatch.argtypes = [HANDLE, ctypes.POINTER(ctypes.POINTER(RECV)),
                                                   ctypes.POINTER(ctypes.c_uint32)]
        dll.SimConnect_GetNextDispatch.restype = ctypes.c_long
        try:
            dll.SimConnect_AddToFacilityDefinition.argtypes = [HANDLE, ctypes.c_uint32, ctypes.c_char_p]
            dll.SimConnect_AddToFacilityDefinition.restype = ctypes.c_long
            dll.SimConnect_RequestFacilityData.argtypes = [HANDLE, ctypes.c_uint32, ctypes.c_uint32,
                                                           ctypes.c_char_p, ctypes.c_char_p]
            dll.SimConnect_RequestFacilityData.restype = ctypes.c_long
            has_facilities = True
        except AttributeError:
            has_facilities = False
        log.info("SimConnect.dll: %s (scenery runways: %s)", path, "yes" if has_facilities else "no")
        waiting_said = False
        while not self.stopping.is_set():
            h = HANDLE()
            if dll.SimConnect_Open(ctypes.byref(h), b"Print Desk", None, 0, None, 0) != 0:
                if not waiting_said:
                    self._call(self.on_status, "warn", "MSFS: waiting for the sim")
                    waiting_said = True
                self.stopping.wait(10)
                continue
            waiting_said = False
            try:
                self._session(dll, h, has_facilities)
            except Exception as e:
                log.warning("SimConnect: %s", e)
            finally:
                self.facilities = False
                dll.SimConnect_Close(h)
            self._call(self.on_status, "warn", "MSFS: waiting for the sim")
            self.stopping.wait(5)

    def _session(self, dll, h, has_facilities=False):
        for name, unit, _, _ in VARS:
            dll.SimConnect_AddToDataDefinition(h, DEF_NUMBERS, name.encode(), unit.encode(), DATATYPE_FLOAT64, 0, 0xFFFFFFFF)
        dll.SimConnect_AddToDataDefinition(h, DEF_ID, b"ATC ID", None, DATATYPE_STRING32, 0, 0xFFFFFFFF)
        dll.SimConnect_RequestDataOnSimObject(h, REQ_NUMBERS, DEF_NUMBERS, OBJECT_USER, PERIOD_SIM_FRAME, 0, 0, 1, 0)
        dll.SimConnect_RequestDataOnSimObject(h, REQ_ID, DEF_ID, OBJECT_USER, PERIOD_SECOND, 0, 0, 30, 0)
        if has_facilities:
            for f in ["OPEN AIRPORT", "N_RUNWAYS", "OPEN RUNWAY", *[n for n, _ in RUNWAY_FIELDS], "CLOSE RUNWAY", "CLOSE AIRPORT"]:
                dll.SimConnect_AddToFacilityDefinition(h, DEF_AIRPORT, f.encode())
            self.facilities = True
        self._call(self.on_status, "ok", "MSFS: connected" + ("" if has_facilities else
                   " (runways from OurAirports; see README for scenery runways)"))
        pending: dict[int, dict] = {}
        next_req = FAC_REQ_FIRST
        last_sent = 0.0
        quiet_since = time.monotonic()
        p = ctypes.POINTER(RECV)()
        size = ctypes.c_uint32()
        while not self.stopping.is_set():
            while has_facilities:                                   # airports asked for by the app
                try:
                    icao, fut = self.fac_queue.get_nowait()
                except queue.Empty:
                    break
                req, next_req = next_req, next_req + 1
                if dll.SimConnect_RequestFacilityData(h, DEF_AIRPORT, req, icao.encode(), b"") != 0:
                    self._answer(fut, None)
                    continue
                pending[req] = {"fut": fut, "runways": [], "t": time.monotonic()}
            for req in [r for r, v in pending.items() if time.monotonic() - v["t"] > FAC_TIMEOUT]:
                v = pending.pop(req)
                self._answer(v["fut"], v["runways"] or None)
            got = False
            while dll.SimConnect_GetNextDispatch(h, ctypes.byref(p), ctypes.byref(size)) == 0:
                got = True
                rid = p.contents.dwID
                if rid == RECV_QUIT:
                    return
                base = ctypes.cast(p, ctypes.c_void_p).value
                if pending and rid not in (RECV_SIMOBJECT_DATA, RECV_EXCEPTION, RECV_OPEN):
                    self._facility_packet(base, p.contents.dwSize, pending)
                    continue
                if rid != RECV_SIMOBJECT_DATA:
                    continue
                request = ctypes.c_uint32.from_address(base + 12).value
                if request == REQ_NUMBERS:
                    now = time.monotonic()
                    if now - last_sent < 1.0 / MAX_RATE:
                        continue
                    last_sent = now
                    values = (ctypes.c_double * len(VARS)).from_address(base + DATA_OFFSET)
                    reading = {"t": now}
                    for (_, _, key, conv), v in zip(VARS, values):
                        reading[key] = conv(v)
                    self._call(self.on_reading, reading)
                elif request == REQ_ID and self.on_info:
                    raw = ctypes.string_at(base + DATA_OFFSET, 32).split(b"\0")[0]
                    self._call(self.on_info, {"acid": raw.decode("ascii", "ignore").strip()})
            if got:
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since > 30:
                return                                          # nothing for 30 s: reconnect
            time.sleep(0.01)


    def _facility_packet(self, base: int, size: int, pending: dict):
        """SIMCONNECT_RECV_FACILITY_DATA / _END. The request ID comes straight after the
        12-byte header; the data is at the end of the packet, so its exact offset
        (which depends on the struct packing) doesn't matter."""
        if size < 16:
            return
        req = ctypes.c_uint32.from_address(base + 12).value
        v = pending.get(req)
        if v is None:
            return
        if size <= 16:                                          # FACILITY_DATA_END
            pending.pop(req)
            self._answer(v["fut"], v["runways"] or None)
            return
        if size < 28 or ctypes.c_uint32.from_address(base + 24).value != FAC_RUNWAY or size < 28 + RUNWAY_SIZE:
            return
        for off in (size - RUNWAY_SIZE, 37, 40, 36):           # end of packet; packed; aligned
            if off < 28 or off + RUNWAY_SIZE > size:
                continue
            lat, lon, hdg, length, width, pn, pd, sn, sd = struct.unpack(
                RUNWAY_FMT, ctypes.string_at(base + off, RUNWAY_SIZE))
            if (-90 <= lat <= 90 and -180 <= lon <= 180 and abs(lat) + abs(lon) > 0 and 0 <= hdg <= 360
                    and 100 < length < 10000 and 5 < width < 200 and 0 <= pn <= 45 and 0 <= sn <= 45
                    and 0 <= pd <= 7 and 0 <= sd <= 7):
                v["runways"].append({"lat": lat, "lon": lon, "hdg": hdg % 360, "length": length, "width": width,
                                     "p_num": pn, "p_des": pd, "s_num": sn, "s_des": sd})
                return
