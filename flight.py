"""The current flight: datalink (Hoppie, read-only) and arrival weather.

Started with "Start flight" in the panel (or by printing the OFP), using the
callsign and airports from your latest SimBrief OFP. Nothing runs between
flights, so Hoppie and VATSIM are only asked while you're actually flying.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import re
import time
from datetime import datetime, timezone

import aiohttp

from pathlib import Path

from acars_log import AcarsLog
from hoppie import Hoppie, HoppieError, RESPONSES, decode_cpdlc
from slips import Line, Slip

log = logging.getLogger("flight")

VATSIM_DATA = "https://data.vatsim.net/v3/vatsim-data.json"
VATSIM_METAR = "https://metar.vatsim.net/{icao}"
MAX_FLIGHT_HOURS = 10            # a forgotten flight stops by itself
POLL = (45, 75)                  # Hoppie: check every 45-75 s, randomly
POLL_FAST = (22, 28)             # briefly faster while waiting for a reply
FIRST_FAST_CHECK = 20            # first check after "clearance requested"
VATSIM_EVERY = 60                # VATSIM updates every 15 s; once a minute is plenty for weather
AUTOPAUSE_EVERY = 15             # flight phases: as often as VATSIM updates, no more
PHASES = {"gate": "at the gate", "taxi_out": "taxi out", "climb": "climb", "cruise": "cruise",
          "descent": "descent and landing", "parked": "parked"}


def distance_nm(lat1, lon1, lat2, lon2) -> float:
    r = 3440.065                                   # earth radius in NM
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def zulu(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime("%H%MZ")


# ------------------------------------------------------------------ slips

def _box(title: str, s: Slip):
    s.center(f" {title} ".center(36, "-"))


PDC_WORDS = re.compile(r"\b(PDC|CLD|CLRD TO|CLEARED TO|DEPARTURE CLEARANCE|DEP CLEARANCE)\b")


def is_pdc(msg: dict) -> bool:
    """A departure clearance: a telex PDC, or a CPDLC "CLEARED TO" from the station."""
    text = decode_cpdlc(msg["packet"])["text"] if msg["type"] == "cpdlc" else msg.get("packet", "")
    return msg["type"] in ("telex", "cpdlc") and bool(PDC_WORDS.search(text.upper()))


def aircraft_type(fp: dict) -> str:
    """The ICAO type from a VATSIM flight plan ("A20N", from "A20N/M-SDE3FGHIRWY/LB1" or "H/B744/L")."""
    short = str(fp.get("aircraft_short") or "").strip().upper()
    if short:
        return short
    for field in (fp.get("aircraft_faa"), fp.get("aircraft")):
        for part in str(field or "").upper().split("/"):
            part = part.split("-")[0].strip()
            if 2 <= len(part) <= 4 and part.isalnum() and any(c.isdigit() for c in part):
                return part
    return ""


def acars_slip(msg: dict, callsign: str) -> Slip:
    s = Slip(f"ACARS {msg['type'].upper()} from {msg['from']}", kind="sim")
    _box("ACARS", s)
    s.kv(f"FROM: {msg['from']}", f"TO: {callsign}", bold=True)
    s.kv(f"TYPE: {msg['type'].upper()}", zulu(msg.get("seen")), bold=True)
    s.rule()
    text, response = msg["packet"], ""
    if msg["type"] == "cpdlc":
        d = decode_cpdlc(msg["packet"])
        text, response = d["text"], RESPONSES.get(d["response"], "")
    for line in (text or "(empty message)").split("\n"):
        s.text(line)
    if response:
        s.rule().text(f"RESPONSE: {response}", bold=True)
    _box("END", s)
    return s


def notice_slip(title: str, lines: list[str], big: str) -> Slip:
    s = Slip(title, kind="sim")
    _box("ACARS", s)
    s.center(big, big=True, inverse=True)
    for l in lines:
        s.center(l, bold=True)
    s.kv("", zulu())
    _box("END", s)
    return s


def atis_slip(icao: str, code: str, lines: list[str], where: str, source: str, label="ATIS") -> Slip:
    title = f"{icao} {label} {code}".strip() if source == "atis" else f"{icao} METAR"
    s = Slip(title, kind="sim")
    _box("ACARS", s)
    s.center(title, big=True)
    s.kv(where, zulu(), bold=True)
    s.rule()
    for l in lines:
        s.text(l)
    if source == "metar":
        s.rule().text("NO ATIS ONLINE - VATSIM METAR")
    _box("END", s)
    return s


# ------------------------------------------------------------------ the flight

class Flight:
    def __init__(self, app, log_path: Path | None = None):
        self.app = app
        self.info: dict | None = None
        self.started = 0.0
        self.task: asyncio.Task | None = None
        self.arrival_task: asyncio.Task | None = None
        self.autopause_task: asyncio.Task | None = None
        self.log = AcarsLog(log_path or Path(__file__).resolve().parent / "acars_log.jsonl")
        self.reset_state()

    def reset_state(self):
        self.messages: list[dict] = []          # newest first
        self.seen_ids: set[int] = set()
        self.first_peek = True
        self.station = None
        self.station_online: bool | None = None
        self.aircraft_online: bool | None = None
        self.warned_unavailable = False
        self.request: dict | None = None        # pending "clearance requested"
        self.arrival = {"status": "waiting", "distance": None, "printed": False}
        self.departure = {"status": "waiting", "printed": False}
        self._data = None                       # last VATSIM data, never carried into a new flight
        self.hoppie_status = ("off", "")
        self.last_check = None
        self._data_at = 0.0
        self._ac = None                         # (type, source, when) of the aircraft you fly
        self._pending: set = set()              # datalink slips waiting for their print delay
        self.vatsim_callsign = None             # the callsign you're connected with (found by CID)
        self.ap = {"phase": "gate", "alt": None, "gs": None, "airborne": False, "still_since": None,
                   "note": "", "suppressed": False}

    @property
    def cfg(self):
        return self.app.cfg.get("hoppie", {})

    @property
    def acfg(self):
        return self.app.cfg.get("arrival", {})

    # ------------------------------------------------------------ start / stop

    async def start(self, info: dict):
        await self.stop(quiet=True)
        self.reset_state()
        self.info = info
        self.started = time.time()
        self.station = info["origin"]
        self.app.history("flight", f"started {info['callsign']} {info['origin']}-{info['destination']}")
        self.task = asyncio.create_task(self._hoppie_loop())
        self.arrival_task = asyncio.create_task(self._weather_loop())
        self.autopause_task = asyncio.create_task(self._autopause_loop())

    async def stop(self, quiet=False):
        await self._auto_resume("flight ended")         # don't leave an automatic pause behind
        for t in (self.task, self.arrival_task, self.autopause_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self.task = self.arrival_task = self.autopause_task = None
        if self.info and not quiet:
            self.app.history("flight", f"ended {self.info['callsign']}")
        if not quiet:
            self.info = None

    async def emit(self, slip):
        await self.app.dispatcher.queue.put(slip)      # flight slips never wait for "pause"

    MODES = ("log", "auto", "except", "only")

    @property
    def mode(self) -> str:
        m = self.cfg.get("print_mode", "except")
        return m if m in self.MODES else "except"

    @property
    def auto(self) -> bool:
        """Whether datalink can print by itself at all (for the panel)."""
        return self.mode != "log"

    def listed_types(self) -> set[str]:
        raw = self.cfg.get("own_printer", [])
        raw = " ".join(raw) if isinstance(raw, list) else str(raw)
        return {t for t in re.split(r"[\s,;]+", raw.upper()) if t}

    async def aircraft(self) -> tuple[str, str]:
        """(ICAO type, where from): your VATSIM flight plan, else the SimBrief OFP."""
        if self._ac and time.time() - self._ac[2] < 300:
            return self._ac[0], self._ac[1]
        t, src = "", ""
        try:
            pilot = await self._vatsim_pilot()
            if pilot:
                t, src = aircraft_type(pilot.get("flight_plan") or {}), "VATSIM"
        except Exception as e:
            log.info("aircraft from VATSIM: %s", e)
        if not t and self.info and self.info.get("aircraft"):
            t, src = self.info["aircraft"], "SimBrief"
        self._ac = (t, src, time.time()) if t else None
        return t, src

    async def auto_ok(self) -> bool:
        """Print datalink by itself now? Depends on the mode and, for two of them, the aircraft."""
        m = self.mode
        if m in ("log", "auto"):
            return m == "auto"
        t, _ = await self.aircraft()
        listed = t in self.listed_types()
        return not listed if m == "except" else listed

    def _wanted(self, m: dict) -> bool:
        """Which messages may print by themselves. "pdc" means departure clearances,
        whether they come as a telex or over CPDLC; "telex" is all other free text too."""
        kinds = self.cfg.get("auto_print", ["cpdlc", "pdc"])
        return m["type"] in kinds or ("pdc" in kinds and is_pdc(m))

    async def print_soon(self, entry: dict):
        """Like an aircraft's ACARS printer: prints a few seconds after the message comes in."""
        base = float(self.cfg.get("print_delay_seconds", 8))
        spread = min(3.0, base * 0.4)                   # 8 s -> about 5 to 11 s
        delay = max(0.0, base + random.uniform(-spread, spread)) if base > 0 else 0.0
        async def later():
            await asyncio.sleep(delay)
            await self.print_entry(entry)
        t = asyncio.create_task(later())
        self._pending.add(t)
        t.add_done_callback(self._pending.discard)

    async def file(self, slip, cat: str, print_now: bool = False, **meta) -> dict:
        """Puts a slip in the datalink log, and prints it when asked to."""
        entry = self.log.add(slip, cat, self.info, **meta)
        if print_now:
            await self.print_entry(entry)
        return entry

    async def print_entry(self, entry: dict):
        self.log.mark_printed(entry)
        await self.emit(self.log.slip(entry))

    async def _notice(self, title, lines, big):
        entry = await self.file(notice_slip(title, lines, big), "notice")
        if await self.auto_ok():
            await self.print_soon(entry)

    # ------------------------------------------------------------ Hoppie

    def _hoppie(self):
        logon = self.cfg.get("logon", "")
        if not self.cfg.get("enabled", True) or not logon:
            return None
        return Hoppie(self.app.http, logon, self.cfg.get("mock_url"))

    async def _hoppie_loop(self):
        cycle = 0
        while time.time() - self.started < MAX_FLIGHT_HOURS * 3600:
            client = self._hoppie()
            if not client:
                self.hoppie_status = ("off", "no Hoppie logon code (Settings > Datalink)"
                                      if self.cfg.get("enabled", True) else "switched off")
            else:
                try:
                    await self._peek(client)
                    if cycle % 2 == 0 or self.request:      # station check every other round
                        await self._ping(client)
                    self.hoppie_status = ("ok", f"checked {zulu()}")
                except (HoppieError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                    self.hoppie_status = ("error", str(e)[:120])
                    log.warning("Hoppie: %s", e)
                await self._check_request()
            cycle += 1
            # Hoppie's rule: every 45-75 s; while waiting for a reply ~25 s is allowed briefly
            fast = self.request and self.request["state"] in ("waiting", "late")
            await asyncio.sleep(random.uniform(*(POLL_FAST if fast else POLL)))
        log.info("flight timed out after %d hours", MAX_FLIGHT_HOURS)
        self.info = None

    async def _peek(self, client):
        msgs = await client.peek(self.info["callsign"])
        new = [m for m in sorted(msgs, key=lambda m: m["id"]) if m["id"] not in self.seen_ids]
        for m in new:
            self.seen_ids.add(m["id"])
            m["seen"] = time.time()
            m["old"] = self.first_peek               # already there before this flight started
            # each Hoppie message goes into the log once, even across restarts
            entry = self.log.has_hoppie(m["id"])
            if entry is None:
                entry = await self.file(acars_slip(m, self.info["callsign"]), m["type"],
                                        hoppie_id=m["id"], old=m["old"], pdc=is_pdc(m), **{"from": m["from"]})
            m["lid"] = entry["n"]
            self.messages.insert(0, m)
            if m["old"]:
                continue
            self.app.history("acars", f"{m['type']} from {m['from']} (in the log)")
            if m["from"] == self.station:
                self._reply_received()
            if self._wanted(m) and await self.auto_ok():
                await self.print_soon(entry)
        del self.messages[100:]
        self.first_peek = False

    async def _ping(self, client):
        cs = self.info["callsign"]
        online = await client.ping(cs, [self.station, cs])
        was = self.station_online
        self.station_online = self.station in online
        self.aircraft_online = cs in online
        self.last_check = time.time()
        if was is not None and was != self.station_online:
            self.app.history("acars", f"{self.station} datalink {'available' if self.station_online else 'gone'}")
        # aircraft is on Hoppie but the departure station isn't: warn once, before any clearance
        if (self.aircraft_online and not self.station_online and not self.warned_unavailable
                and self.cfg.get("warn_unavailable", True) and not self._got_reply()):
            self.warned_unavailable = True
            await self._notice(f"Datalink not available at {self.station}",
                               [f"{self.station} NOT LOGGED ON", "REVERT TO VOICE"], "DATALINK N/A")

    def _got_reply(self):
        return any(m["from"] == self.station and not m["old"] for m in self.messages)

    async def print_message(self, m: dict):
        entry = self.log.get(m.get("lid"))
        if entry:
            await self.print_entry(entry)
        else:
            await self.emit(acars_slip(m, self.info["callsign"]))

    # ------------------------------------------------------------ "clearance requested"

    async def clearance_requested(self):
        if not self.info:
            raise ValueError("start a flight first")
        if self.station_online is False:
            self.request = {"state": "failed", "since": time.time()}
            await self._notice(f"Datalink not available at {self.station}",
                               [f"{self.station} NOT LOGGED ON", "REVERT TO VOICE"], "DATALINK N/A")
            return "Station not logged on - revert to voice."
        self.request = {"state": "waiting", "since": time.time()}
        # check sooner than the normal cycle
        if self.task and not self.task.done():
            self.task.cancel()
        self.task = asyncio.create_task(self._hoppie_loop_after_request())
        return f"Waiting for {self.station}."

    async def _hoppie_loop_after_request(self):
        await asyncio.sleep(FIRST_FAST_CHECK)
        await self._hoppie_loop()

    def _reply_received(self):
        if self.request and self.request["state"] in ("waiting", "late"):
            self.request = {"state": "received", "since": time.time()}

    async def _check_request(self):
        r = self.request
        if not r or r["state"] not in ("waiting", "late"):
            return
        age = time.time() - r["since"]
        if age >= float(self.cfg.get("timeout_minutes", 3)) * 60:
            self.request = {"state": "failed", "since": time.time()}
            await self._notice(f"No reply from {self.station}",
                               [f"NO REPLY FROM {self.station}", "REQUEST TIMED OUT", "REVERT TO VOICE"],
                               "NO REPLY")
        elif age >= float(self.cfg.get("warn_after_seconds", 60)):
            r["state"] = "late"

    def cancel_request(self):
        self.request = None

    def request_view(self):
        """Request state for the panel; 'late' is computed live between checks."""
        r = self.request
        if not r:
            return None
        state = r["state"]
        age = time.time() - r["since"]
        if state == "waiting" and age >= float(self.cfg.get("warn_after_seconds", 60)):
            state = "late"
        return {"state": state, "seconds": int(age),
                "timeout": int(float(self.cfg.get("timeout_minutes", 3)) * 60)}

    # ------------------------------------------------------------ departure and arrival weather

    @property
    def dcfg(self):
        return self.app.cfg.get("departure", {})

    async def _weather_loop(self):
        """One VATSIM check a minute for both: departure weather when you connect
        at the gate, arrival weather when you get within range."""
        dep_mode = self.dcfg.get("mode", "connect")
        arr_on = self.acfg.get("enabled", True)
        if dep_mode == "start":
            await self._safe_print("dep")
        if dep_mode == "off":
            self.departure["status"] = "off"
        if not arr_on:
            self.arrival["status"] = "off"
        elif self.info.get("dest_lat") is None:
            self.arrival["status"] = "no destination position in OFP"
            arr_on = False
        threshold = float(self.acfg.get("distance_nm", 150))
        while time.time() - self.started < MAX_FLIGHT_HOURS * 3600:
            need_dep = dep_mode == "connect" and not self.departure["printed"]
            need_arr = arr_on and not self.arrival["printed"]
            if not (need_dep or need_arr):
                break
            try:
                pilot = await self._vatsim_pilot()
                if pilot is None:
                    if need_dep:
                        self.departure["status"] = "waiting for you to connect to VATSIM"
                    if need_arr:
                        self.arrival["status"] = "not seen on VATSIM"
                else:
                    gs = pilot.get("groundspeed") or 0
                    if need_dep and gs < 40 and self._near_origin(pilot):
                        await self.print_weather("dep")
                    elif need_dep:
                        self.departure["status"] = "connected, not at the departure airport"
                    if need_arr:
                        d = distance_nm(pilot["latitude"], pilot["longitude"],
                                        self.info["dest_lat"], self.info["dest_lon"])
                        self.arrival.update({"distance": round(d), "status": "tracking"})
                        if d <= threshold and gs > 60:
                            await self.print_weather("arr", d)
            except Exception as e:
                for w in (self.departure, self.arrival):
                    if not w["printed"] and w["status"] != "off":
                        w["status"] = f"VATSIM: {str(e)[:80]}"
                log.info("VATSIM data: %s", e)
            await asyncio.sleep(VATSIM_EVERY)

    def _near_origin(self, pilot) -> bool:
        if self.info.get("orig_lat") is None:
            return True                      # no position in the OFP: connected on the ground is enough
        return distance_nm(pilot["latitude"], pilot["longitude"],
                           self.info["orig_lat"], self.info["orig_lon"]) <= 10

    async def _safe_print(self, which):
        try:
            await self.print_weather(which)
        except Exception as e:
            (self.departure if which == "dep" else self.arrival)["status"] = f"couldn't get weather: {e}"

    async def _vatsim_data(self):
        # the weather and the flight phases share one download (VATSIM updates every 15 s)
        if self._data is not None and time.time() - self._data_at < AUTOPAUSE_EVERY - 1:
            return self._data
        data = await self._vatsim_fetch()
        self._data, self._data_at = data, time.time()
        return data

    async def _vatsim_fetch(self):
        url = self.acfg.get("mock_vatsim_url") or VATSIM_DATA
        async with self.app.http.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                raise RuntimeError(f"data feed {r.status}")
            return await r.json(content_type=None)

    async def _vatsim_pilot(self):
        """You on VATSIM: by your CID when it's set (any callsign), otherwise by the OFP callsign."""
        self._data = await self._vatsim_data()
        pilots = self._data.get("pilots", [])
        cid = str(self.app.cfg.get("vatsim", {}).get("cid", "") or "").strip()
        if cid:
            me = next((p for p in pilots if str(p.get("cid")) == cid), None)
            if me is not None:
                self.vatsim_callsign = me.get("callsign")
            return me
        cs = self.info["callsign"]
        return next((p for p in pilots if p.get("callsign") == cs), None)

    async def print_weather(self, which: str, dist: float | None = None, print_now: bool | None = None):
        """which: "dep" (departure ATIS/METAR) or "arr" (arrival ATIS/METAR).
        Automatic fetches print only in auto mode; the panel buttons always print."""
        dep = which == "dep"
        icao = self.info["origin" if dep else "destination"]
        prefer = (self.dcfg if dep else self.acfg).get("prefer", "atis")
        data = getattr(self, "_data", None)
        atis = None
        if prefer == "atis":
            if data is None:
                data = await self._vatsim_data()
            stations = {a.get("callsign"): a for a in data.get("atis", [])}
            for name in ((f"{icao}_D_ATIS", f"{icao}_ATIS") if dep else (f"{icao}_A_ATIS", f"{icao}_ATIS")):
                if name in stations and stations[name].get("text_atis"):
                    atis = stations[name]
                    break
        where = "DEPARTURE" if dep else f"AT {dist:.0f} NM" if dist is not None else "ARRIVAL"
        if atis:
            lines = [" ".join(" ".join(atis["text_atis"]).split())]
            cs = atis.get("callsign", "")
            label = "DEP ATIS" if cs.endswith("_D_ATIS") else "ARR ATIS" if cs.endswith("_A_ATIS") else "ATIS"
            slip = atis_slip(icao, atis.get("atis_code") or "", lines, where, "atis", label)
        else:
            url = (self.acfg.get("mock_metar_url") or VATSIM_METAR).format(icao=icao)
            async with self.app.http.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                metar = (await r.text()).strip() if r.status == 200 else ""
            slip = atis_slip(icao, "", [metar or "NO METAR AVAILABLE"], where, "metar")
        # the weather has its own switch, apart from the datalink messages (log only by default)
        auto_weather = (self.dcfg if dep else self.acfg).get("print", True)
        now = auto_weather if print_now is None else print_now
        (self.departure if dep else self.arrival).update({"printed": True, "status": "printed" if now else "logged"})
        self.app.history("acars", slip.title + ("" if now else " (in the log)"))
        await self.file(slip, "weather", print_now=now, **{"from": icao})

    async def print_arrival_weather(self, dist: float | None = None):
        await self.print_weather("arr", dist)

    # ------------------------------------------------------------ auto-pause in busy phases
    # Gate: printing. Taxi out: paused (a pause you started for pushback is taken over).
    # Above the altitude: resumed with one receipt. Below it again (500 ft lower): paused.
    # Landed and standing still for a while: resumed with one receipt.

    @property
    def apcfg(self):
        return self.app.cfg.get("autopause", {})

    def _set_phase(self, phase):
        if phase != self.ap["phase"]:
            self.ap["phase"] = phase
            self.ap["suppressed"] = False               # a hand resume only lasts one phase
            self.app.history("flight", f"phase: {PHASES.get(phase, phase)}")

    async def _auto_pause(self, reason):
        if self.ap["suppressed"]:
            return
        self.app.engine.start_away("auto", reason)

    async def _auto_resume(self, why):
        e = self.app.engine
        if e.away and e.away.owner == "auto":
            self.app.history("flight", f"auto-pause ended ({why})")
            await self.app.resume_stream("receipt")

    def manual_resume(self):
        """You resumed by hand: no automatic pause again until the next phase."""
        e = self.app.engine
        if e.away and e.away.owner == "auto":
            self.ap["suppressed"] = True

    async def autopause_step(self, alt: float, gs: float, now: float | None = None):
        """One VATSIM position. Separate from the loop so it can be tested."""
        now = now or time.time()
        top = float(self.apcfg.get("altitude_ft", 15000))
        taxi = float(self.apcfg.get("taxi_kts", 5))
        parked_after = float(self.apcfg.get("parked_seconds", 120))
        ap = self.ap
        ap["alt"], ap["gs"] = round(alt), round(gs)
        ph = ap["phase"]
        if gs > 60:
            ap["airborne"] = True
        if ph == "gate" and gs > 60 and alt >= top:      # flight started in Print Desk while in cruise
            ap["airborne"] = True
            self._set_phase("cruise")
            return
        if ph in ("gate", "parked"):
            if gs >= taxi:
                ap["airborne"], ap["still_since"] = gs > 60, None
                self._set_phase("taxi_out")
                await self._auto_pause("taxi out")
        elif ph == "taxi_out":
            if ap["airborne"]:
                self._set_phase("climb")
        if ap["phase"] == "climb":
            if alt >= top:
                self._set_phase("cruise")
                await self._auto_resume(f"above {top:,.0f} ft")
        if ap["phase"] == "cruise":
            if alt < top - 500:
                self._set_phase("descent")
                await self._auto_pause("descent")
        elif ap["phase"] in ("descent", "climb") and ap["airborne"]:
            # a short hop that never reaches the altitude counts as descent once it slows down
            if alt >= top and ap["phase"] == "descent":
                self._set_phase("cruise")
                await self._auto_resume(f"back above {top:,.0f} ft")
            elif gs < taxi:
                ap["still_since"] = ap["still_since"] or now
                if now - ap["still_since"] >= parked_after:
                    self._set_phase("parked")
                    ap["airborne"], ap["still_since"] = False, None
                    await self._auto_resume("parked")
            else:
                ap["still_since"] = None

    async def _autopause_loop(self):
        while time.time() - self.started < MAX_FLIGHT_HOURS * 3600:
            if not self.apcfg.get("enabled", False):
                self.ap["note"] = "switched off"
                await self._auto_resume("switched off")
                await asyncio.sleep(3)
                continue
            try:
                pilot = await self._vatsim_pilot()
                if pilot is None:
                    self.ap["note"] = "waiting for you on VATSIM"
                else:
                    self.ap["note"] = ""
                    await self.autopause_step(float(pilot.get("altitude") or 0), float(pilot.get("groundspeed") or 0))
            except Exception as e:
                self.ap["note"] = f"VATSIM: {str(e)[:80]}"
            await asyncio.sleep(AUTOPAUSE_EVERY)

    def autopause_view(self):
        on = bool(self.apcfg.get("enabled", False))
        top = int(float(self.apcfg.get("altitude_ft", 15000)))
        if not on:
            return {"enabled": False, "altitude_ft": top}
        if not self.info:
            return {"enabled": True, "altitude_ft": top, "text": "Works during a flight (Start flight from SimBrief)."}
        ap, e = self.ap, self.app.engine
        own = bool(e.away and e.away.owner == "auto")
        bits = [PHASES.get(ap["phase"], ap["phase"]).capitalize()]
        if ap["alt"] is not None:
            bits.append(f"{ap['alt']:,} ft, {ap['gs']} kt")
        bits.append("paused" if own else "paused by hand" if e.away else
                    "resumed by hand" if ap["suppressed"] else "printing")
        return {"enabled": True, "altitude_ft": top, "phase": ap["phase"], "paused": own,
                "text": ap["note"].capitalize() if ap["note"] else " · ".join(bits)}

    # ------------------------------------------------------------ panel view

    def view(self):
        if not self.info:
            return None
        return {"info": self.info,
                "station": self.station,
                "station_online": self.station_online,
                "aircraft_online": self.aircraft_online,
                "hoppie": {"level": self.hoppie_status[0], "detail": self.hoppie_status[1]},
                "request": self.request_view(),
                "arrival": self.arrival,
                "departure": self.departure,
                "departure_mode": self.dcfg.get("mode", "connect"),
                "arrival_nm": self.acfg.get("distance_nm", 150) if self.acfg.get("enabled", True) else None,
                "print_mode": self.mode,
                "aircraft": {"type": self._ac[0], "source": self._ac[1]} if self._ac else None,
                "listed": sorted(self.listed_types()),
                "messages": [{"id": m["id"], "from": m["from"], "type": m["type"],
                              "text": decode_cpdlc(m["packet"])["text"] if m["type"] == "cpdlc" else m["packet"],
                              "time": zulu(m["seen"]), "old": m["old"], "pdc": is_pdc(m),
                              "printed": bool((self.log.get(m.get("lid")) or {}).get("printed"))}
                             for m in self.messages[:40]]}
