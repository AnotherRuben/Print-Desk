"""StreamElements through the Astro websocket gateway.

StreamElementsTips: tips on your Twitch StreamElements channel.
StreamElementsYouTube: memberships, gifted memberships, Super Chats and new
subscribers from your YouTube channel's StreamElements activity feed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path

import aiohttp

log = logging.getLogger("streamelements")

URL = "wss://astro.streamelements.com/"


def normalise_tip(data: dict) -> dict:
    d = data.get("donation") or {}
    return {"type": "tip",
            "id": data.get("_id"),
            "user": (d.get("user") or {}).get("username") or "Anonymous",
            "amount": d.get("amount", 0),
            "currency": d.get("currency", ""),
            "message": d.get("message", ""),
            "approved": data.get("approved"),
            "status": data.get("status")}


def _num(v, default=0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def normalise_youtube(act: dict, default_currency="EUR") -> dict | None:
    """One channel.activities message from YouTube, as a Print Desk event.

    StreamElements documents the activity names but not every field inside
    them, so this accepts the usual spellings and falls back sensibly."""
    if (act.get("provider") or "").lower() != "youtube":
        return None
    t = act.get("type")
    d = act.get("data") or {}
    user = d.get("displayName") or d.get("username") or d.get("name") or "Someone"
    base = {"platform": "youtube", "id": act.get("_id") or act.get("activityId"),
            "user": user, "user_id": None, "login": None,
            "avatar_url": d.get("avatar") or None}
    tier = d.get("tier") or d.get("level") or d.get("levelName") or ""
    message = d.get("message") or ""
    if t == "sponsor":                                       # a YouTube membership
        sender = d.get("sender") or d.get("gifter")
        if d.get("gifted") or sender:
            return {**base, "type": "gift_recipient", "tier": str(tier), "gifter": sender}
        months = int(_num(d.get("amount") or d.get("months"), 1)) or 1
        if months > 1:
            return {**base, "type": "resub", "tier": str(tier), "months": months, "message": message}
        return {**base, "type": "sub", "tier": str(tier)}
    if t in ("communityGiftPurchase", "giftedMembers"):      # someone gifting memberships
        return {**base, "type": "gift", "count": int(_num(d.get("amount") or d.get("quantity"), 1)) or 1,
                "tier": str(tier), "cumulative": None}
    if t == "superchat":                                     # Super Chats and Super Stickers
        return {**base, "type": "superchat", "amount": _num(d.get("amount")),
                "currency": (d.get("currency") or default_currency or "").upper(),
                "message": message, "sticker": bool(d.get("sticker") or d.get("stickerId"))}
    if t == "subscriber":                                    # a YouTube channel subscribe
        return {**base, "type": "follow"}
    return None


class StreamElementsTips:
    topic, label, what = "channel.tips", "StreamElements", "tips"

    def __init__(self, cfg: dict, http: aiohttp.ClientSession, on_event, status=None):
        self.status = status or (lambda level, detail="": None)
        self.cfg = cfg
        self.jwt = cfg.get("jwt", "")
        if not self.jwt:
            raise ValueError(f"no JWT token set (Settings > {self.label})")
        self.http = http
        self.on_event = on_event
        self.reconnect_token = None
        self.seen = deque(maxlen=500)

    async def run(self):
        backoff = 1
        while True:
            try:
                await self._session()
                backoff = 1
            except PermissionError as e:
                log.error("%s", e)
                self.status("error", "JWT refused - copy it again")
                await asyncio.sleep(300)
            except Exception as e:
                log.warning("StreamElements connection lost: %s", e)
                self.status("warn", "reconnecting")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _session(self):
        url = URL + (f"?reconnect_token={self.reconnect_token}" if self.reconnect_token else "")
        restored = bool(self.reconnect_token)
        self.reconnect_token = None
        # heartbeat: aiohttp answers the server's pings automatically
        async with self.http.ws_connect(url, autoping=True) as ws:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                m = json.loads(msg.data)
                t = m.get("type")
                if t == "welcome":
                    if not restored:
                        await ws.send_json({"type": "subscribe", "nonce": self.what,
                                            "data": {"topic": self.topic, "token": self.jwt,
                                                     "token_type": "jwt"}})
                    log.info("%s: connected", self.label)
                    if restored:
                        self.status("ok", self.what)
                elif t == "response":
                    if m.get("error"):
                        msg_txt = (m.get("data") or {}).get("message", "")
                        if m["error"] == "err_unauthorized":
                            raise PermissionError(f"StreamElements refused the JWT ({msg_txt}) - copy it again from the dashboard")
                        log.warning("StreamElements error %s: %s", m["error"], msg_txt)
                    else:
                        log.info("%s: subscribed to %s", self.label, self.what)
                        self.status("ok", self.what)
                elif t == "message" and m.get("topic") == self.topic:
                    ev = self.normalise(m.get("data") or {})
                    if not ev:
                        continue
                    if ev["id"] and ev["id"] in self.seen:
                        continue
                    self.seen.append(ev["id"])
                    log.info("%s: %s from %s", self.label, ev["type"], ev.get("user"))
                    await self.on_event(ev)
                elif t == "reconnect":
                    self.reconnect_token = (m.get("data") or {}).get("reconnect_token")
                    log.info("%s: server restarting, reconnecting", self.label)
                    return
        raise ConnectionError("closed by server")

    def normalise(self, data):
        return normalise_tip(data)


class StreamElementsYouTube(StreamElementsTips):
    topic, label, what = "channel.activities", "YouTube", "members, gifts, Super Chats"
    SAMPLES = 25                       # first YouTube activities kept, to check the field names

    def __init__(self, cfg, http, on_event, status=None, sample_file: Path | None = None):
        super().__init__(cfg, http, on_event, status)
        self.sample_file = sample_file

    def normalise(self, data):
        self._sample(data)
        return normalise_youtube(data, self.cfg.get("currency", "EUR"))

    def _sample(self, data):
        """StreamElements doesn't document every YouTube field, so the first few
        real ones are saved to youtube_events.jsonl for checking."""
        if not self.sample_file or (data.get("provider") or "").lower() != "youtube":
            return
        try:
            n = len(self.sample_file.read_text(encoding="utf-8").splitlines()) if self.sample_file.exists() else 0
            if n < self.SAMPLES:
                with self.sample_file.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"saved": time.strftime("%Y-%m-%d %H:%M:%S"), **data}, ensure_ascii=False) + "\n")
        except OSError:
            pass
