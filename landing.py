"""Landing reports: recognises a landing in the sim readings and sums it up.

LandingWatcher gets a reading about 15 times a second (see simconnect_source.py)
and calls on_report with a dict once the aircraft has landed and slowed below
the print speed. It knows nothing about SimConnect, so it can be tested with
made-up readings.

A reading is a dict with:
  t          seconds (monotonic)
  on_ground  bool
  agl        feet above ground
  vs         vertical speed, feet/minute (negative = descending)
  ias, gs    indicated airspeed and groundspeed, knots
  pitch      degrees, nose up positive
  bank       degrees
  hdg, trk   true heading and true ground track, degrees
  g          G-force
  td_vs      the sim's own touchdown vertical speed, feet/minute (optional)
"""
from __future__ import annotations

from collections import deque

AIRBORNE_FOR = 10.0          # seconds in the air before a touchdown counts (not at load-in)
TRACE_BEFORE = 10            # seconds of approach in the second-by-second table
TRACE_AFTER = 3              # and after touchdown
G_WINDOW = 1.5               # seconds after touchdown to look for the G peak
BOUNCE_AGL = 1.0             # feet: off the ground again by more than this is a bounce


def _angle(a: float) -> float:
    """-180..180 degrees."""
    return (a + 180.0) % 360.0 - 180.0


def score(rep: dict) -> int:
    """A simple, open landing score out of 100 (not any airline's formula)."""
    s = 100.0
    vs = abs(rep["vs"])
    if vs > 240:
        s -= min(50.0, (vs - 240) / 8)               # 400 fpm: -20, 600 fpm: -45
    elif vs < 60:
        s -= (60 - vs) / 6                          # floated on: a little off
    if rep["g"] > 1.4:
        s -= min(20.0, (rep["g"] - 1.4) * 50)
    if abs(rep["bank"]) > 2:
        s -= (abs(rep["bank"]) - 2) * 4
    if abs(rep["crab"]) > 3:
        s -= (abs(rep["crab"]) - 3) * 2
    s -= 15 * rep["bounces"]
    if rep["tail_strike"] or rep["wing_strike"]:
        s = min(s, 30.0)
    return int(max(0, min(100, round(s))))


class LandingWatcher:
    def __init__(self, on_report, cfg: dict | None = None):
        self.on_report = on_report
        self.cfg = cfg or {}
        self.reset()

    def configure(self, cfg: dict):
        self.cfg = cfg or {}

    def reset(self):
        self.air_since = None        # when we last left the ground
        self.prev = None             # previous reading
        self.flare = None            # readings as we passed 50 ft
        self.td = None               # the touchdown being followed
        self.bouncing = False
        self.recent = deque()        # the last seconds of readings, for the table

    # ------------------------------------------------------------ readings

    def feed(self, r: dict):
        self.recent.append(r)
        keep = TRACE_BEFORE + TRACE_AFTER + 2
        while self.recent and r["t"] - self.recent[0]["t"] > (keep if self.td else TRACE_BEFORE + 2):
            self.recent.popleft()
        p, self.prev = self.prev, r
        if p is None:
            return
        if not r["on_ground"]:
            if self.air_since is None:
                self.air_since = r["t"]
            if self.td is None:
                if p["agl"] >= 50 > r["agl"] and r["vs"] < 0:
                    self.flare = {"ias": r["ias"], "vs": r["vs"]}
                return
            # in the air again after a touchdown: a bounce, or a touch-and-go
            if r["agl"] > BOUNCE_AGL and not self.bouncing:
                self.bouncing = True
                self.td["bounces"] += 1
            if r["t"] - self.td["t"] > AIRBORNE_FOR and r["agl"] > 100:
                self.td["touch_and_go"] = True
                self._finish()
                self.air_since = r["t"]
            return
        # on the ground
        if self.td is None:
            if self.air_since is not None and r["t"] - self.air_since >= AIRBORNE_FOR and not p["on_ground"]:
                self._touchdown(p, r)
            self.air_since = None
            return
        self.bouncing = False
        if r["t"] - self.td["t"] <= G_WINDOW:
            self.td["g"] = max(self.td["g"], r["g"])
        if r.get("td_vs") and self.td.get("sim_vs_before") != r["td_vs"]:
            self.td["sim_vs"] = r["td_vs"]                      # the sim's own touchdown value, once updated
        below = float(self.cfg.get("print_below_kts", 40))
        if r["gs"] < below and r["t"] - self.td["t"] > G_WINDOW:
            self._finish()

    def _touchdown(self, air: dict, ground: dict):
        """air: the last reading in the air; ground: the first on the ground."""
        self.td = {
            "t": ground["t"],
            "vs": air["vs"], "ias": air["ias"], "gs": air["gs"],
            "pitch": air["pitch"], "bank": air["bank"],
            "crab": _angle(air["hdg"] - air["trk"]) if air["gs"] > 30 else 0.0,
            "g": max(air["g"], ground["g"]),
            "bounces": 0, "touch_and_go": False,
            "flare": self.flare,
            "sim_vs_before": air.get("td_vs"),          # the sim's value from the previous landing
        }
        if ground.get("td_vs") and ground.get("td_vs") != air.get("td_vs"):
            self.td["sim_vs"] = ground["td_vs"]                # updated in the same instant
        self.flare = None

    def _trace(self, t0: float) -> list[dict]:
        """One row per whole second around touchdown, from the nearest reading."""
        rows = []
        for sec in range(-TRACE_BEFORE, TRACE_AFTER + 1):
            near = min(self.recent, key=lambda x: abs(x["t"] - (t0 + sec)), default=None)
            if near is None or abs(near["t"] - (t0 + sec)) > 0.6:
                continue
            rows.append({"s": sec, "agl": max(0, round(near["agl"])), "ias": round(near["ias"]),
                         "vs": round(near["vs"]), "pitch": round(near["pitch"], 1),
                         "bank": round(near["bank"], 1), "g": round(near["g"], 2)})
        return rows

    def _finish(self):
        td, self.td = self.td, None
        if td is None:
            return
        trace = self._trace(td["t"])
        vs = td["vs"]
        sim_vs = td.get("sim_vs")
        if sim_vs and 0 < abs(sim_vs) < 3000:              # more precise than our own reading
            vs = -abs(sim_vs)
        tail = float(self.cfg.get("tail_strike_pitch", 10.0))
        wing = float(self.cfg.get("wing_strike_bank", 7.0))
        rep = {
            "vs": round(vs), "g": round(td["g"], 2),
            "pitch": round(td["pitch"], 1), "bank": round(td["bank"], 1), "crab": round(td["crab"], 1),
            "ias": round(td["ias"]), "gs": round(td["gs"]),
            "bounces": td["bounces"], "touch_and_go": td["touch_and_go"],
            "flare": {k: round(v) for k, v in td["flare"].items()} if td["flare"] else None,
            "tail_strike": td["pitch"] >= tail,
            "wing_strike": abs(td["bank"]) >= wing,
        }
        rep["score"] = score(rep)
        for row in trace:                                       # the touchdown row: the real touchdown values
            if row["s"] == 0:
                row.update(vs=rep["vs"], g=rep["g"], pitch=rep["pitch"], bank=rep["bank"])
        rep["trace"] = trace
        self.on_report(rep)


def demo_readings(td_vs=-160.0, hz=15.0, crab=-2.0, bank=1.2, pitch=4.8, g_td=1.18):
    """A made-up approach (for "printdesk test landing"): 3 degree glide at 140 kt,
    flare from 30 ft, touchdown, roll-out below 40 kt."""
    t, agl, ias, vs, dt = 0.0, 600.0, 140.0, -750.0, 1 / hz
    landed, t_td = False, 0.0
    while True:
        t += dt
        if not landed:
            if agl < 30:
                vs += (td_vs - vs) * 0.25
                ias -= 0.15
            agl += vs / 60 * dt
            if agl <= 0:
                agl, landed, t_td = 0.0, True, t
        else:
            ias = max(0.0, ias - (22 * dt if t - t_td > 1 else 0))
        yield {"t": t, "on_ground": landed, "agl": agl, "vs": 0.0 if landed else vs, "ias": ias, "gs": ias - 4,
               "pitch": pitch if agl < 30 else 2.5, "bank": bank if agl < 50 else 0.0, "hdg": 250.0 + crab,
               "trk": 250.0, "g": g_td if landed and t - t_td < 0.3 else 1.0, "td_vs": None}
        if landed and ias < 20:
            return
