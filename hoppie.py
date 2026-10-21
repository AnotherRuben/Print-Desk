"""Read-only Hoppie ACARS client.

Print Desk never sends anything on Hoppie. It only:
- "peeks" at the messages sent to your callsign (doesn't mark them delivered,
  doesn't make you appear online, the aircraft still gets everything), and
- "pings" to see whether a station (e.g. EHAM) is logged on.
"""
from __future__ import annotations

import re

import aiohttp

URL = "https://www.hoppie.nl/acars/system/connect.html"
ITEM_RE = re.compile(r"\{(\d+)\s+([A-Z0-9_\-]+)\s+([a-z\-]+)\s+\{([^}]*)\}\}", re.S)


class HoppieError(Exception):
    pass


def parse_peek(text: str) -> list[dict]:
    text = text.strip()
    if text.startswith("error"):
        raise HoppieError(text[5:].strip(" {}") or "error")
    if not text.startswith("ok"):
        raise HoppieError(f"unexpected reply: {text[:80]}")
    return [{"id": int(i), "from": f, "type": t, "packet": p.strip()}
            for i, f, t, p in ITEM_RE.findall(text)]


def parse_ping(text: str) -> set[str]:
    text = text.strip()
    if text.startswith("error"):
        raise HoppieError(text[5:].strip(" {}") or "error")
    body = re.search(r"\{(.*)\}", text, re.S)
    return set(re.findall(r"[A-Z0-9_\-]{3,10}", body.group(1))) if body else set()


def decode_cpdlc(packet: str) -> dict:
    """/data2/12//WU/CLEARED TO @EBBR@ ... -> readable text and response type."""
    m = re.match(r"^/data2/(\d*)/(\d*)/([A-Z]{1,2})/(.*)$", packet, re.S)
    if not m:
        return {"text": packet, "response": "", "min": None}
    msg = m.group(4)
    msg = msg.replace("@_@", "\n").replace("_@", "\n")   # line breaks used by ground stations
    msg = msg.replace("@", "")
    lines = [" ".join(l.split()) for l in msg.split("\n")]
    return {"text": "\n".join(l for l in lines if l), "response": m.group(3),
            "min": int(m.group(1)) if m.group(1) else None}


RESPONSES = {"WU": "WILCO / UNABLE", "AN": "AFFIRM / NEGATIVE", "R": "ROGER", "Y": "RESPONSE REQUIRED"}


class Hoppie:
    def __init__(self, http: aiohttp.ClientSession, logon: str, url: str | None = None):
        self.http = http
        self.logon = logon
        self.url = url or URL

    async def _get(self, params: dict) -> str:
        async with self.http.get(self.url, params={"logon": self.logon, **params},
                                 timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                raise HoppieError(f"Hoppie returned {r.status}")
            return await r.text()

    async def peek(self, callsign: str) -> list[dict]:
        return parse_peek(await self._get({"from": callsign, "to": "SERVER", "type": "peek"}))

    async def ping(self, callsign: str, stations: list[str]) -> set[str]:
        """Which of `stations` are logged on to Hoppie right now."""
        return parse_ping(await self._get({"from": callsign, "to": "SERVER", "type": "ping",
                                           "packet": " ".join(stations)}))
