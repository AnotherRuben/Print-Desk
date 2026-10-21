"""Events that came in while Print Desk wasn't running, from StreamElements.

StreamElements keeps an activity history for your channel (and for your
YouTube channel, if linked). At startup Print Desk asks for the stretch it
missed and adds those events to the stream log, marked "while Print Desk was
off". Nothing prints: they're there to look at, print by hand, or add up in a
summary.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from slips import Layouts
from streamelements import _num, normalise_youtube

log = logging.getLogger("backfill")

API = "https://api.streamelements.com/kappa/v2"
STATUS = "while Print Desk was off"
MAX_DAYS = 14                     # never reach further back than this
HEARTBEAT_EVERY = 60


def channel_from_jwt(jwt: str) -> str | None:
    """StreamElements JWTs carry the channel ID in their payload."""
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("channel")
    except Exception:
        return None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _ts(created: str | None) -> float:
    try:
        return datetime.fromisoformat((created or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def normalise_twitch(act: dict) -> dict | None:
    """A Twitch activity from the StreamElements history, as a Print Desk event."""
    if (act.get("provider") or "twitch").lower() != "twitch":
        return None
    t, d = act.get("type"), act.get("data") or {}
    user = d.get("displayName") or d.get("username") or d.get("name") or "Anonymous"
    base = {"platform": "twitch", "user": user, "login": (d.get("username") or "").lower() or None}
    if t in ("follow", "follower"):
        return {**base, "kind": "follow"}
    if t in ("subscriber", "subscription", "sub"):
        tier = str(d.get("tier") or "1000")
        if d.get("gifted") or d.get("sender"):
            return {**base, "kind": "gift_recipient", "tier": tier, "gifter": d.get("sender")}
        months = int(_num(d.get("amount"), 1)) or 1
        if months > 1:
            return {**base, "kind": "resub", "tier": tier, "months": months, "message": d.get("message") or ""}
        return {**base, "kind": "sub", "tier": tier}
    if t in ("communityGiftPurchase", "communityGift"):
        return {**base, "kind": "gift", "count": int(_num(d.get("amount"), 1)) or 1,
                "tier": str(d.get("tier") or "1000"), "cumulative": None}
    if t == "cheer":
        return {**base, "kind": "bits", "bits": int(_num(d.get("amount"))), "message": d.get("message") or ""}
    if t in ("tip", "donation"):
        return {**base, "kind": "tip", "amount": _num(d.get("amount")),
                "currency": (d.get("currency") or "").upper(), "message": d.get("message") or ""}
    if t == "raid":
        return {**base, "kind": "raid", "viewers": int(_num(d.get("amount")))}
    return None


def _youtube(act, currency):
    ev = normalise_youtube(act, currency)
    if not ev:
        return None
    kind = {"gift_recipient": "gift_recipient"}.get(ev["type"], ev["type"])
    out = {k: v for k, v in ev.items() if k not in ("type", "id", "user_id", "avatar_url")}
    return {**out, "kind": kind}


def group_gifts(events: list[tuple[float, dict, str]]):
    """Gift purchases take the recipients that came with them; lone recipients stay single gifts."""
    gifts = [(ts, ev, sid) for ts, ev, sid in events if ev["kind"] == "gift"]
    for ts, g, _ in gifts:
        g.setdefault("recipients", [])
    out = []
    for ts, ev, sid in events:
        if ev["kind"] != "gift_recipient":
            out.append((ts, ev, sid))
            continue
        home = next((g for gts, g, _ in gifts
                     if g["platform"] == ev["platform"] and abs(ts - gts) <= 120
                     and len(g["recipients"]) < g["count"]
                     and (not ev.get("gifter") or ev["gifter"].lower() == (g.get("user") or "").lower())), None)
        if home is not None:
            home["recipients"].append(ev["user"])
            continue
        # no purchase event: recipients from the same gifter close together make one gift
        gifter = ev.get("gifter") or "Anonymous"
        made = next((o for o in reversed(out) if o[1]["kind"] == "gift" and o[1].get("_made")
                     and o[1]["platform"] == ev["platform"] and o[1]["user"] == gifter and ts - o[0] <= 120), None)
        if made:
            made[1]["recipients"].append(ev["user"])
            made[1]["count"] += 1
        else:
            out.append((ts, {"kind": "gift", "platform": ev["platform"], "user": gifter, "count": 1,
                             "tier": ev.get("tier", "1000"), "recipients": [ev["user"]],
                             "cumulative": None, "_made": True}, sid))
    for _, ev, _ in out:
        ev.pop("_made", None)
    return out


class Backfill:
    def __init__(self, app, seen_file: Path):
        self.app = app
        self.seen_file = seen_file
        self.api = API
        self.details: dict = {}

    # ------------------------------------------------------------ "Print Desk was running" marker

    def last_seen(self) -> float | None:
        try:
            return float(self.seen_file.read_text().strip())
        except (OSError, ValueError):
            return None

    def mark_seen(self):
        try:
            self.seen_file.write_text(str(time.time()))
        except OSError:
            pass

    async def heartbeat(self):
        """Every minute: 'still running', so the next start knows what it missed."""
        import asyncio
        while True:
            self.mark_seen()
            await asyncio.sleep(HEARTBEAT_EVERY)

    # ------------------------------------------------------------ fetching

    def _sources(self):
        cfg = self.app.cfg
        out = []
        se = cfg.get("streamelements", {})
        if se.get("enabled", True) and se.get("jwt"):
            out.append(("twitch", se["jwt"]))
        yt = cfg.get("youtube", {})
        if yt.get("enabled") and yt.get("jwt"):
            out.append(("youtube", yt["jwt"]))
        return out

    async def _channel(self, jwt):
        ch = channel_from_jwt(jwt)
        if ch:
            return ch
        async with self.app.http.get(f"{self.api}/channels/me", headers={"Authorization": f"Bearer {jwt}"},
                                     timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                raise RuntimeError(f"StreamElements channel lookup {r.status}")
            return (await r.json()).get("_id")

    async def _get(self, url, jwt, params):
        async with self.app.http.get(url, params=params, headers={"Authorization": f"Bearer {jwt}"},
                                     timeout=aiohttp.ClientTimeout(total=30)) as r:
            body = await r.text()
            if r.status in (401, 403):
                raise PermissionError("StreamElements refused the JWT - copy it again")
            if r.status != 200:
                return r.status, body, None
        try:
            data = json.loads(body)
        except ValueError:
            return 200, body, None
        if isinstance(data, dict):
            data = data.get("docs") or data.get("activities") or data.get("data") or []
        return 200, body, data if isinstance(data, list) else []

    async def _activities(self, jwt, after: float, before: float, platform: str):
        """The history for a time range. StreamElements doesn't document this call, so
        when the range query gives nothing, it takes the latest activities and filters here."""
        channel = await self._channel(jwt)
        url = f"{self.api}/activities/{channel}"
        # "types" is required: without it StreamElements answers an empty list
        documented = ["follow", "tip", "subscriber", "cheer", "raid", "sponsor", "superchat"]
        extra = ["communityGiftPurchase", "giftedMembers"]          # gifts, not in every list of types
        base = [("after", _iso(after)), ("before", _iso(before)), ("limit", "500")]
        mins = [("mincheer", "0"), ("minhost", "0"), ("minsub", "0"), ("mintip", "0"), ("origin", "feed")]
        ms = [("after", str(int(after * 1000))), ("before", str(int(before * 1000))), ("limit", "500")]
        tries = [
            ("all types", base + mins + [("types", t) for t in documented + extra]),
            ("documented types", base + mins + [("types", t) for t in documented]),
            ("documented types, plain", base + [("types", t) for t in documented]),
            ("times in ms", ms + mins + [("types", t) for t in documented]),
        ]
        notes, raw = [], {}
        for name, params in tries:
            status, body, data = await self._get(url, jwt, params)
            raw[name] = {"status": status, "params": [f"{k}={v}" for k, v in params], "body": body[:200000]}
            if data is None:
                notes.append(f"{name}: HTTP {status}")
                continue
            inside = [a for a in data if after - 60 <= _ts(a.get("createdAt")) <= before + 60]
            notes.append(f"{name}: {len(data)} back, {len(inside)} in range")
            if inside:
                self._save_raw(platform, channel, raw)
                return inside, notes
        self._save_raw(platform, channel, raw)
        return [], notes

    def _save_raw(self, platform, channel, raw):
        """What StreamElements answered, to check when something is missing."""
        try:
            p = self.seen_file.with_name(f"se_history_{platform}.json")
            p.write_text(json.dumps({"saved": time.strftime("%Y-%m-%d %H:%M:%S"), "channel": channel,
                                     "answers": raw}, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    async def fetch(self, after: float, before: float | None = None) -> dict:
        """Adds what StreamElements has between the two times. Returns counts per source."""
        before = before or time.time()
        after = max(after, before - MAX_DAYS * 86400)
        book = self.app.stream_log
        known = {e.get("se_id") for e in book.entries if e.get("se_id")}
        recent = [(e["at"], e.get("cat"), (e.get("user") or "").lower()) for e in book.entries
                  if e["at"] >= after - 600]
        layouts = Layouts(self.app.cfg)
        result, self.details = {}, {}
        for platform, jwt in self._sources():
            try:
                acts, notes = await self._activities(jwt, after, before, platform)
            except Exception as e:
                log.warning("StreamElements history (%s): %s", platform, e)
                result[platform] = f"couldn't fetch: {e}"
                continue
            events, types, unused, already = [], {}, {}, 0
            for a in acts:
                t = a.get("type") or "?"
                types[t] = types.get(t, 0) + 1
                sid = a.get("_id") or a.get("activityId")
                if sid and sid in known:
                    already += 1
                    continue
                ev = (_youtube(a, self.app.cfg.get("youtube", {}).get("currency", "EUR"))
                      if platform == "youtube" else normalise_twitch(a))
                if ev:
                    events.append((_ts(a.get("createdAt")), ev, sid))
                else:
                    unused[f"{a.get('provider', '?')} {t}"] = unused.get(f"{a.get('provider', '?')} {t}", 0) + 1
            self.details[platform] = {"tries": notes, "types": types, "not_used": unused, "already": already}
            log.info("StreamElements history (%s): %s | types %s | not used %s", platform, "; ".join(notes), types, unused)
            added = 0
            for ts, ev, sid in sorted(group_gifts(events), key=lambda x: x[0]):
                kind, who = ev["kind"], (ev.get("user") or "").lower()
                # already in the log from when Print Desk was running (live events have no StreamElements id)
                if any(abs(ts - at) < 180 and cat == kind and u == who for at, cat, u in recent):
                    self.details[platform]["already"] += 1
                    continue
                builder = getattr(layouts, kind, None)
                if not builder:
                    continue
                layouts.when = datetime.fromtimestamp(ts)
                slip = builder(ev)
                book.add(slip, kind, at=ts, status=STATUS, platform=ev["platform"], user=ev.get("user"),
                         item=ev, se_id=sid)
                added += 1
            result[platform] = added
        return result
