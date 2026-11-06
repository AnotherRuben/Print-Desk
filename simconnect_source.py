"""Reads the aircraft from MSFS (2020 or 2024) through SimConnect, for landing reports.

SimConnect is the sim's own add-on interface: nothing extra to install in MSFS.
Print Desk uses the SimConnect.dll that comes with the "SimConnect" Python package
(installed by install.bat) and asks MSFS for all values at once, every sim frame,
in a background thread. Each set of values becomes a reading for LandingWatcher.
"""
from __future__ import annotations

import ctypes
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
]

RECV_EXCEPTION, RECV_OPEN, RECV_QUIT, RECV_SIMOBJECT_DATA = 1, 2, 3, 8
DATATYPE_FLOAT64, DATATYPE_STRING32 = 4, 6
PERIOD_SIM_FRAME, PERIOD_SECOND = 3, 4
OBJECT_USER = 0
DEF_NUMBERS, DEF_ID = 1, 2
REQ_NUMBERS, REQ_ID = 1, 2
DATA_OFFSET = 40          # SIMCONNECT_RECV (12 bytes) + 7 DWORDs, then the values
MAX_RATE = 20.0           # readings per second passed on (sim frames can be faster)


class RECV(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_uint32), ("dwVersion", ctypes.c_uint32), ("dwID", ctypes.c_uint32)]


def dll_path() -> str | None:
    """The SimConnect.dll from the "SimConnect" Python package, if installed."""
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
                self._session(dll, h)
            except Exception as e:
                log.warning("SimConnect: %s", e)
            finally:
                dll.SimConnect_Close(h)
            self._call(self.on_status, "warn", "MSFS: waiting for the sim")
            self.stopping.wait(5)

    def _session(self, dll, h):
        for name, unit, _, _ in VARS:
            dll.SimConnect_AddToDataDefinition(h, DEF_NUMBERS, name.encode(), unit.encode(), DATATYPE_FLOAT64, 0, 0xFFFFFFFF)
        dll.SimConnect_AddToDataDefinition(h, DEF_ID, b"ATC ID", None, DATATYPE_STRING32, 0, 0xFFFFFFFF)
        dll.SimConnect_RequestDataOnSimObject(h, REQ_NUMBERS, DEF_NUMBERS, OBJECT_USER, PERIOD_SIM_FRAME, 0, 0, 1, 0)
        dll.SimConnect_RequestDataOnSimObject(h, REQ_ID, DEF_ID, OBJECT_USER, PERIOD_SECOND, 0, 0, 30, 0)
        self._call(self.on_status, "ok", "MSFS: connected")
        last_sent = 0.0
        quiet_since = time.monotonic()
        p = ctypes.POINTER(RECV)()
        size = ctypes.c_uint32()
        while not self.stopping.is_set():
            got = False
            while dll.SimConnect_GetNextDispatch(h, ctypes.byref(p), ctypes.byref(size)) == 0:
                got = True
                rid = p.contents.dwID
                if rid == RECV_QUIT:
                    return
                if rid != RECV_SIMOBJECT_DATA:
                    continue
                base = ctypes.cast(p, ctypes.c_void_p).value
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
