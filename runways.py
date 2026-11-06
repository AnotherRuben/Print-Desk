"""Runways, for touchdown zone and centreline on the landing report.

Uses runways.csv from OurAirports (free, worldwide, every runway end with its
position, true heading, length, width and displaced threshold). It's downloaded
once into the Print Desk folder and refreshed monthly. MSFS scenery can differ
a little from the real world, so treat a metre or two as normal.
"""
from __future__ import annotations

import csv
import logging
import math
import time
from pathlib import Path

import aiohttp

log = logging.getLogger("runways")

URL = "https://davidmegginson.github.io/ourairports-data/runways.csv"
FALLBACK_URL = "https://ourairports.com/data/runways.csv"
REFRESH_DAYS = 30
FT = 0.3048
EARTH = 6371000.0


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def offsets(lat0, lon0, heading, lat, lon):
    """(along, cross) in metres of a point from a runway end: along = down the runway
    from that end, cross = right of the centreline (looking down the runway)."""
    dy = math.radians(lat - lat0) * EARTH
    dx = math.radians(lon - lon0) * EARTH * math.cos(math.radians((lat + lat0) / 2))
    h = math.radians(heading)
    return dx * math.sin(h) + dy * math.cos(h), dx * math.cos(h) - dy * math.sin(h)


def ends(row: dict):
    """Both landing ends of one runway row, with the runway's length and width."""
    length = (_f(row.get("length_ft"), 0) or 0) * FT
    width = (_f(row.get("width_ft"), 45 / FT) or 45 / FT) * FT
    out = []
    for e in ("le", "he"):
        lat, lon, hdg = _f(row.get(f"{e}_latitude_deg")), _f(row.get(f"{e}_longitude_deg")), _f(row.get(f"{e}_heading_degT"))
        if lat is None or lon is None:
            continue
        if hdg is None:                         # work it out from the other end
            o = "he" if e == "le" else "le"
            lat2, lon2 = _f(row.get(f"{o}_latitude_deg")), _f(row.get(f"{o}_longitude_deg"))
            if lat2 is None or lon2 is None:
                continue
            y = math.sin(math.radians(lon2 - lon)) * math.cos(math.radians(lat2))
            x = (math.cos(math.radians(lat)) * math.sin(math.radians(lat2)) -
                 math.sin(math.radians(lat)) * math.cos(math.radians(lat2)) * math.cos(math.radians(lon2 - lon)))
            hdg = math.degrees(math.atan2(y, x)) % 360
        out.append({"airport": row.get("airport_ident", ""), "ident": row.get(f"{e}_ident", ""),
                    "lat": lat, "lon": lon, "hdg": hdg, "length": length, "width": width,
                    "displaced": (_f(row.get(f"{e}_displaced_threshold_ft"), 0) or 0) * FT})
    return out


def touchdown(rwy: dict, along_end: float, cross: float) -> dict:
    """Touchdown zone (ICAO: the first 900 m, or a third of shorter runways) and centreline."""
    landing_length = rwy["length"] - rwy["displaced"]
    from_threshold = along_end - rwy["displaced"]
    zone = 900.0 if landing_length >= 2400 else landing_length / 3
    tdz = "SHORT" if from_threshold < 0 else "LONG" if from_threshold > zone else "IN"
    return {"airport": rwy["airport"], "ident": rwy["ident"], "from_threshold": round(from_threshold),
            "cross": round(cross, 1), "tdz": tdz, "zone": round(zone), "width": round(rwy["width"])}


def best_match(rows, lat, lon, track):
    """The runway end you landed on: pointing your way, and you within its edges."""
    best = None
    for row in rows:
        if str(row.get("closed", "0")) == "1":
            continue
        for rwy in ends(row):
            if abs((track - rwy["hdg"] + 180) % 360 - 180) > 30:
                continue
            along, cross = offsets(rwy["lat"], rwy["lon"], rwy["hdg"], lat, lon)
            if not (-300 < along < rwy["length"] + 100) or abs(cross) > rwy["width"] / 2 + 15:
                continue
            if best is None or abs(cross) < abs(best[2]):
                best = (rwy, along, cross)
    return touchdown(*best) if best else None


class Runways:
    def __init__(self, path: Path, http: aiohttp.ClientSession | None):
        self.path = path
        self.http = http
        self.cache: dict[str, list] = {}

    async def ensure(self) -> bool:
        """The runway file, downloaded if missing or older than a month."""
        fresh = self.path.exists() and time.time() - self.path.stat().st_mtime < REFRESH_DAYS * 86400
        if fresh or not self.http:
            return self.path.exists()
        for url in (URL, FALLBACK_URL):
            try:
                async with self.http.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                    if r.status == 200:
                        data = await r.read()
                        if data.startswith(b'"id"') or data.startswith(b"id,"):
                            tmp = self.path.with_suffix(".tmp")
                            tmp.write_bytes(data)
                            tmp.replace(self.path)
                            self.cache.clear()
                            log.info("runway data updated (%d KB)", len(data) // 1024)
                            return True
            except Exception as e:
                log.info("runway data from %s: %s", url, e)
        return self.path.exists()

    def _rows(self, idents: set[str] | None, near: tuple[float, float] | None = None):
        out = []
        with self.path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if idents is not None:
                    if row.get("airport_ident", "").upper() in idents:
                        out.append(row)
                elif near:
                    lat, lon = _f(row.get("le_latitude_deg")), _f(row.get("le_longitude_deg"))
                    if lat is not None and lon is not None and abs(lat - near[0]) < 0.06 and abs(lon - near[1]) < 0.1:
                        out.append(row)
        return out

    async def find(self, lat: float, lon: float, track: float, airports=()) -> dict | None:
        """The runway and touchdown details, or None (no data, or not on a known runway)."""
        if not await self.ensure():
            return None
        idents = {a.upper() for a in airports if a}
        key = ",".join(sorted(idents))
        if idents:
            if key not in self.cache:
                self.cache[key] = self._rows(idents)
            hit = best_match(self.cache[key], lat, lon, track)
            if hit:
                return hit
        return best_match(self._rows(None, (lat, lon)), lat, lon, track)     # wherever you are
