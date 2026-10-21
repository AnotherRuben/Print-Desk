"""SimBrief: fetch your latest OFP and print one of its download formats.

The chosen download (e.g. "Leonardo MD80 (ACARS)") is cut off at a marker
line such as [OFP], so only the cockpit-printer part ends up on paper.
"""
from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urljoin

import aiohttp

from slips import Line, Slip

log = logging.getLogger("simbrief")

API = "https://www.simbrief.com/api/xml.fetcher.php"


def find_downloads(data, directory=""):
    """Walks the OFP JSON and returns {name: url} for every downloadable file."""
    found = {}
    if isinstance(data, dict):
        directory = data.get("directory") or directory
        name, link = data.get("name"), data.get("link")
        if isinstance(name, str) and isinstance(link, str) and link:
            found[name] = urljoin(directory, link) if directory else link
        for v in data.values():
            if isinstance(v, (dict, list)):
                found.update(find_downloads(v, directory))
    elif isinstance(data, list):
        for v in data:
            found.update(find_downloads(v, directory))
    return found


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _obj(v) -> dict:
    """SimBrief sends a list instead of a single entry when there are several
    (e.g. two alternates). Use the first one."""
    if isinstance(v, list):
        v = next((x for x in v if isinstance(x, dict)), None)
    return v if isinstance(v, dict) else {}


def flight_from_ofp(d: dict) -> dict:
    d = _obj(d)
    g, o, a = _obj(d.get("general")), _obj(d.get("origin")), _obj(d.get("destination"))
    callsign = (_obj(d.get("atc")).get("callsign")
                or f"{g.get('icao_airline', '')}{g.get('flight_number', '')}").strip().upper()
    if not callsign or not o.get("icao_code") or not a.get("icao_code"):
        raise RuntimeError("couldn't find callsign and airports in your latest OFP")
    return {"callsign": callsign,
            "origin": str(o["icao_code"]).upper(),
            "destination": str(a["icao_code"]).upper(),
            "alternate": str(_obj(d.get("alternate")).get("icao_code") or "").upper(),
            "dest_lat": _num(a.get("pos_lat")), "dest_lon": _num(a.get("pos_long")),
            "orig_lat": _num(o.get("pos_lat")), "orig_lon": _num(o.get("pos_long")),
            "ofp_id": _obj(d.get("params")).get("request_id")}


def cut_text(text: str, marker: str) -> list[str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4).split("\n")
    if marker:
        for i, line in enumerate(lines):
            if line.strip() == marker:
                lines = lines[:i]
                break
        else:
            log.warning("marker %s not found, printing the whole file", marker)
    while lines and not lines[-1].strip():
        lines.pop()
    return [l.rstrip() for l in lines]


def ofp_slip(lines: list[str], title: str, wide: bool) -> Slip:
    s = Slip(title, kind="sim")
    for line in lines:
        s.lines.append(Line(kind="pre", text=line, wide=wide))
    return s


class SimBrief:
    def __init__(self, cfg: dict, http: aiohttp.ClientSession):
        self.username = str(cfg.get("username", "")).strip()
        self.userid = str(cfg.get("userid", "")).strip()
        self.format = cfg.get("print_format", "Leonardo MD80 (ACARS)")
        self.marker = cfg.get("cut_at", "[OFP]")
        self.wide = cfg.get("size", "double_width") == "double_width"
        self.http = http
        self.last = None

    async def latest(self) -> dict:
        if not (self.username or self.userid):
            raise ValueError("no SimBrief username set (Settings > SimBrief)")
        params = {"json": "v2"}
        params["userid" if self.userid else "username"] = self.userid or self.username
        async with self.http.get(API, params=params) as r:
            if r.status != 200:
                raise RuntimeError(f"SimBrief returned {r.status}: {(await r.text())[:200]}")
            self.last = await r.json(content_type=None)
            return self.last

    async def flight(self) -> dict:
        """The flight from your latest OFP: callsign, airports, destination position."""
        return flight_from_ofp(await self.latest())

    async def downloads(self) -> dict:
        return find_downloads(await self.latest())

    async def ofp_slip(self) -> Slip:
        files = await self.downloads()
        # "MD-80", "MD80" and "md 80" are all the same download: compare letters and digits only
        squash = lambda t: "".join(c for c in t.lower() if c.isalnum())
        wanted = squash(self.format)
        match = next((n for n in files if squash(n) == wanted), None) \
            or next((n for n in files if wanted and wanted in squash(n)), None)
        if not match:
            names = "\n  ".join(sorted(files)) or "(none found)"
            raise RuntimeError(f"no download called '{self.format}'. Available:\n  {names}")
        async with self.http.get(files[match]) as r:
            if r.status != 200:
                raise RuntimeError(f"download of {match} failed: {r.status}")
            text = await r.text(errors="replace")
        lines = cut_text(text, self.marker)
        title = lines[0].strip() if lines else match
        log.info("printing %s (%d lines)", match, len(lines))
        return ofp_slip(lines, f"OFP {title}", self.wide)

    def file_slip(self, path: Path) -> Slip:
        """Print from a file you downloaded yourself (also handy for testing)."""
        lines = cut_text(path.read_text(encoding="utf-8", errors="replace"), self.marker)
        title = lines[0].strip() if lines else path.name
        return ofp_slip(lines, f"OFP {title}", self.wide)
