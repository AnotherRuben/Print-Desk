"""Twitch: one-time device login and EventSub over WebSocket."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import aiohttp

log = logging.getLogger("twitch")

SCOPES = "channel:read:subscriptions bits:read channel:read:hype_train moderator:read:followers"
FOLLOW_SCOPE = "moderator:read:followers"
AUTH = "https://id.twitch.tv/oauth2"
HELIX = "https://api.twitch.tv/helix"
WS_URL = "wss://eventsub.wss.twitch.tv/ws?keepalive_timeout_seconds=30"


class AuthError(Exception):
    pass


class TwitchAuth:
    def __init__(self, client_id: str, token_file: Path, http: aiohttp.ClientSession):
        if not client_id:
            raise ValueError("no Twitch Client ID set (Settings > Twitch)")
        self.client_id = client_id
        self.file = token_file
        self.http = http
        self.access = self.refresh_token = ""
        self.user_id = self.login = ""
        self.scopes: set[str] = set()
        if self.file.exists():
            data = json.loads(self.file.read_text())
            self.access, self.refresh_token = data.get("access", ""), data.get("refresh", "")

    @property
    def has_tokens(self):
        return bool(self.refresh_token)

    def _save(self):
        self.file.write_text(json.dumps({"access": self.access, "refresh": self.refresh_token}))

    def logout(self):
        self.access = self.refresh_token = ""
        if self.file.exists():
            self.file.unlink()

    async def start_device(self) -> dict:
        """Step 1: get a code for you to approve on twitch.tv."""
        async with self.http.post(f"{AUTH}/device",
                                  data={"client_id": self.client_id, "scopes": SCOPES}) as r:
            d = await r.json(content_type=None)
            if r.status != 200:
                msg = d.get("message", d) if isinstance(d, dict) else d
                raise AuthError(f"Twitch refused the login ({msg}). Is the app's Client Type set to Public?")
        return d

    async def finish_device(self, d: dict):
        """Step 2: wait until you've approved it, then save the tokens."""
        interval = int(d.get("interval", 5))
        deadline = time.monotonic() + int(d.get("expires_in", 1800))
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            async with self.http.post(f"{AUTH}/token", data={
                    "client_id": self.client_id, "scopes": SCOPES,
                    "device_code": d["device_code"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}) as r:
                t = await r.json(content_type=None)
            if r.status == 200:
                self.access, self.refresh_token = t["access_token"], t["refresh_token"]
                self._save()
                await self.validate()
                return self.login
            if t.get("message") not in ("authorization_pending", "slow_down"):
                raise AuthError(f"login failed: {t.get('message', t)}")
        raise AuthError("login timed out")

    async def device_login(self):
        """Command-line login: shows a code; you approve it in the browser once."""
        d = await self.start_device()
        print("\nOpen this page and approve Print Desk:")
        print(f"  {d['verification_uri']}")
        print(f"  (code {d['user_code']} should be pre-filled)\n")
        login = await self.finish_device(d)
        print(f"Logged in as {login}. You can now start Print Desk.")

    async def refresh(self):
        if not self.refresh_token:
            raise AuthError("not logged in to Twitch (Settings > Twitch > Log in)")
        async with self.http.post(f"{AUTH}/token", data={
                "client_id": self.client_id, "grant_type": "refresh_token",
                "refresh_token": self.refresh_token}) as r:
            t = await r.json()
        if r.status != 200:
            raise AuthError("Twitch login expired - log in again (Settings > Twitch)")
        self.access, self.refresh_token = t["access_token"], t.get("refresh_token", self.refresh_token)
        self._save()
        log.info("access token refreshed")

    async def validate(self):
        """Checks the token and fetches your user id. Refreshes when expired."""
        for attempt in range(2):
            async with self.http.get(f"{AUTH}/validate",
                                     headers={"Authorization": f"OAuth {self.access}"}) as r:
                if r.status == 200:
                    v = await r.json()
                    self.user_id, self.login = v["user_id"], v["login"]
                    self.scopes = set(v.get("scopes", []))
                    missing = set(SCOPES.split()) - set(v.get("scopes", []))
                    if missing:
                        log.warning("token is missing scopes %s - run login again", missing)
                    return
            await self.refresh()
        raise AuthError("could not validate Twitch token")

    def headers(self):
        return {"Authorization": f"Bearer {self.access}", "Client-Id": self.client_id}


def _name(ev, prefix="user"):
    """Display name, or the login when the display name won't print (e.g. CJK)."""
    name = ev.get(f"{prefix}_name") or ""
    login = ev.get(f"{prefix}_login") or ""
    return name if name.isascii() else (login or name)


def _time(s):
    if not s:
        return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", s.replace("Z", "+00:00"))  # Twitch sends nanoseconds
    try:
        return datetime.fromisoformat(s).astimezone().replace(tzinfo=None)
    except ValueError:
        return None


def _who(ev, prefix="user"):
    """user_id and login, used to look up the profile picture."""
    return {"user_id": ev.get(f"{prefix}_id"), "login": ev.get(f"{prefix}_login")}


def parts_from_emotes(text: str, emotes: list | None) -> list[dict] | None:
    """Resub messages: emote positions (begin/end, inclusive) in the text."""
    if not text or not emotes:
        return None
    parts, pos = [], 0
    for e in sorted(emotes, key=lambda e: int(e.get("begin", 0))):
        b, end = int(e.get("begin", 0)), int(e.get("end", 0))
        if b < pos or end < b or end >= len(text):
            continue
        if b > pos:
            parts.append({"type": "text", "text": text[pos:b]})
        parts.append({"type": "emote", "id": str(e.get("id")), "name": text[b:end + 1]})
        pos = end + 1
    if pos < len(text):
        parts.append({"type": "text", "text": text[pos:]})
    return parts if any(p["type"] == "emote" for p in parts) else None


def parts_from_fragments(fragments: list | None):
    """Bits messages: fragments; the Cheer100-style cheermotes are left out
    (the amount is already in the slip header)."""
    parts, text = [], []
    for f in fragments or []:
        kind = f.get("type")
        if kind == "cheermote":
            continue
        if kind == "emote" and f.get("emote"):
            parts.append({"type": "emote", "id": str(f["emote"].get("id")), "name": f.get("text", "")})
            text.append(f.get("text", ""))
        else:
            parts.append({"type": "text", "text": f.get("text", "")})
            text.append(f.get("text", ""))
    plain = " ".join("".join(text).split())
    return plain, (parts if any(p["type"] == "emote" for p in parts) else None)


def normalise(sub_type: str, ev: dict) -> dict | None:
    """Turns an EventSub event into the engine's simple event dict."""
    if sub_type == "channel.subscribe":
        kind = "gift_recipient" if ev.get("is_gift") else "sub"
        return {"type": kind, "user": _name(ev), "tier": ev.get("tier", "1000"), **_who(ev)}
    if sub_type == "channel.subscription.gift":
        anon = ev.get("is_anonymous")
        return {"type": "gift", "user": None if anon else _name(ev), "count": ev.get("total", 1),
                "tier": ev.get("tier", "1000"), "cumulative": None if anon else ev.get("cumulative_total"),
                **({} if anon else _who(ev))}
    if sub_type == "channel.subscription.message":
        msg = ev.get("message") or {}
        return {"type": "resub", "user": _name(ev), "tier": ev.get("tier", "1000"),
                "months": ev.get("cumulative_months") or 1, "message": msg.get("text", ""),
                "message_parts": parts_from_emotes(msg.get("text", ""), msg.get("emotes")), **_who(ev)}
    if sub_type == "channel.bits.use":
        if ev.get("type") != "cheer":
            return None                                   # Power-ups aren't cheers
        msg = ev.get("message") or {}
        plain, parts = parts_from_fragments(msg.get("fragments"))
        return {"type": "cheer", "user": _name(ev), "bits": ev.get("bits", 0),
                "message": plain if msg.get("fragments") else msg.get("text", ""),
                "message_parts": parts, **_who(ev)}
    if sub_type == "channel.follow":
        return {"type": "follow", "user": _name(ev), **_who(ev)}
    if sub_type == "channel.cheer":
        anon = ev.get("is_anonymous")
        return {"type": "cheer", "user": "Anonymous" if anon else _name(ev),
                "bits": ev.get("bits", 0), "message": ev.get("message", ""),
                **({} if anon else _who(ev))}
    if sub_type == "channel.raid":
        return {"type": "raid", "user": _name(ev, "from_broadcaster_user"),
                "viewers": ev.get("viewers", 0), **_who(ev, "from_broadcaster_user")}
    if sub_type == "channel.hype_train.begin":
        return {"type": "hype_begin", "level": ev.get("level", 1), "started": _time(ev.get("started_at"))}
    if sub_type == "channel.hype_train.progress":
        return {"type": "hype_progress", "level": ev.get("level", 1), "started": _time(ev.get("started_at"))}
    if sub_type == "channel.hype_train.end":
        return {"type": "hype_end", "level": ev.get("level"), "total": ev.get("total"),
                "top_contributions": ev.get("top_contributions") or [],
                "started": _time(ev.get("started_at")), "ended": _time(ev.get("ended_at"))}
    return None


class TwitchEventSub:
    def __init__(self, auth: TwitchAuth, http: aiohttp.ClientSession, on_event, cfg: dict,
                 status=None):
        self.status = status or (lambda level, detail="": None)
        self.auth = auth
        self.http = http
        self.on_event = on_event
        self.ws_url = cfg.get("mock_ws_url") or WS_URL
        self.api = (cfg.get("mock_api_url") or HELIX).rstrip("/")
        self.seen = deque(maxlen=500)

    def subscriptions(self):
        b = {"broadcaster_user_id": self.auth.user_id}
        extra = []
        if FOLLOW_SCOPE in self.auth.scopes:
            extra.append(("channel.follow", "2", {"broadcaster_user_id": self.auth.user_id,
                                                  "moderator_user_id": self.auth.user_id}))
        return extra + [
            ("channel.subscribe", "1", b),
            ("channel.subscription.gift", "1", b),
            ("channel.subscription.message", "1", b),
            ("channel.bits.use", "1", b),
            ("channel.raid", "1", {"to_broadcaster_user_id": self.auth.user_id}),
            ("channel.hype_train.begin", "2", b),
            ("channel.hype_train.progress", "2", b),
            ("channel.hype_train.end", "2", b),
        ]

    async def run(self):
        self.status("warn", "connecting")
        try:
            await self.auth.validate()
        except AuthError as e:
            self.status("error", str(e))
            raise
        log.info("Twitch: listening for %s", self.auth.login)
        backoff = 1
        while True:
            try:
                await self._session()
                backoff = 1
            except AuthError as e:
                self.status("error", str(e))
                raise
            except Exception as e:
                log.warning("Twitch connection lost: %s", e)
                self.status("warn", "reconnecting")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _recv(self, ws, timeout):
        msg = await asyncio.wait_for(ws.receive(), timeout)
        if msg.type == aiohttp.WSMsgType.TEXT:
            return json.loads(msg.data)
        raise ConnectionError(f"websocket closed ({ws.close_code})")

    async def _session(self):
        ws = await self.http.ws_connect(self.ws_url)
        try:
            welcome = await self._recv(ws, 15)
            session = welcome["payload"]["session"]
            keepalive = int(session.get("keepalive_timeout_seconds") or 30)
            await self._subscribe_all(session["id"])
            while True:
                m = await self._recv(ws, keepalive + 10)
                kind = m["metadata"]["message_type"]
                if kind == "notification":
                    mid = m["metadata"]["message_id"]
                    if mid in self.seen:
                        continue
                    self.seen.append(mid)
                    p = m["payload"]
                    ev = normalise(p["subscription"]["type"], p["event"])
                    if ev:
                        log.info("Twitch event: %s", ev["type"])
                        await self.on_event(ev)
                elif kind == "session_reconnect":
                    # move to the new connection; subscriptions carry over
                    new = await self.http.ws_connect(m["payload"]["session"]["reconnect_url"])
                    await self._recv(new, 15)
                    await ws.close()
                    ws = new
                    log.info("Twitch: reconnected to new session")
                elif kind == "revocation":
                    log.warning("Twitch revoked %s: %s", m["payload"]["subscription"]["type"],
                                m["payload"]["subscription"]["status"])
        finally:
            await ws.close()

    async def _subscribe_all(self, session_id):
        for sub_type, version, condition in self.subscriptions():
            body = {"type": sub_type, "version": version, "condition": condition,
                    "transport": {"method": "websocket", "session_id": session_id}}
            for attempt in range(2):
                async with self.http.post(f"{self.api}/eventsub/subscriptions",
                                          headers=self.auth.headers(), json=body) as r:
                    if r.status in (200, 202):
                        break
                    if r.status == 401 and attempt == 0:
                        await self.auth.refresh()
                        continue
                    log.error("could not subscribe to %s: %s %s", sub_type, r.status, await r.text())
                    break
        log.info("Twitch: subscribed to %d event types", len(self.subscriptions()))
        if FOLLOW_SCOPE in self.auth.scopes:
            self.status("ok", self.auth.login)
        else:
            self.status("ok", f"{self.auth.login} - log in again to enable follows")
