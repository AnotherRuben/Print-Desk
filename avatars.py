"""Fetching Twitch profile pictures for slips.

Lookups use the Twitch login Print Desk already has. Pictures are cached on
disk by their URL, so regulars aren't downloaded again; when someone changes
their picture the URL changes too, and the new one is fetched.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import time
from pathlib import Path

import aiohttp

log = logging.getLogger("avatars")

HELIX = "https://api.twitch.tv/helix"
LOGIN_RE = re.compile(r"^[a-zA-Z0-9_]{3,25}$")
LOOKUP_TTL = 6 * 3600              # re-check someone's picture URL after 6 hours


def _pil():
    try:
        from PIL import Image
        return Image
    except ImportError:
        return None


class Avatars:
    def __init__(self, cache_dir: Path, http: aiohttp.ClientSession, get_auth, api_base=None):
        """get_auth: callable returning the current TwitchAuth (or None)."""
        self.dir = cache_dir
        self.http = http
        self.get_auth = get_auth
        self.api = (api_base or HELIX).rstrip("/")
        self.urls: dict[str, tuple[str, float]] = {}     # "id:123" / "login:name" -> (url, when)

    @property
    def available(self):
        return _pil() is not None

    async def get(self, user_id: str | None = None, login: str | None = None, timeout: float = 4.0):
        """Returns a PIL image, or None (no account, default picture, or Twitch slow)."""
        if not self.available or not (user_id or login):
            return None
        if login and not user_id and not LOGIN_RE.match(login):
            return None                                     # a tip name that can't be a Twitch account
        try:
            return await asyncio.wait_for(self._get(user_id, login), timeout)
        except asyncio.TimeoutError:
            log.info("profile picture took too long, printing without")
        except Exception as e:
            log.info("no profile picture (%s)", e)
        return None

    async def get_url(self, url: str, name: str | None = None, timeout: float = 4.0):
        """A picture by its URL (YouTube, through StreamElements). Cached like the rest."""
        if not self.available or not url or not url.startswith("https://"):
            return None
        try:
            data = await asyncio.wait_for(self._download(url), timeout)
            Image = _pil()
            img = Image.open(io.BytesIO(data))
            img.load()
            return img
        except asyncio.TimeoutError:
            log.info("profile picture took too long, printing without")
        except Exception as e:
            log.info("no profile picture (%s)", e)
        return None

    async def _get(self, user_id, login):
        url = await self._lookup(user_id, login)
        if not url or "user-default-pictures" in url:
            return None                                     # Twitch's default avatar: skip
        data = await self._download(url)
        Image = _pil()
        img = Image.open(io.BytesIO(data))
        img.load()
        return img

    async def _lookup(self, user_id, login):
        key = f"id:{user_id}" if user_id else f"login:{login.lower()}"
        hit = self.urls.get(key)
        if hit and time.time() - hit[1] < LOOKUP_TTL:
            return hit[0]
        auth = self.get_auth()
        if not auth or not auth.access:
            return None
        params = {"id": user_id} if user_id else {"login": login.lower()}
        for attempt in range(2):
            async with self.http.get(f"{self.api}/users", params=params, headers=auth.headers()) as r:
                if r.status == 401 and attempt == 0:
                    await auth.refresh()
                    continue
                if r.status != 200:
                    raise RuntimeError(f"Twitch user lookup {r.status}")
                users = (await r.json()).get("data") or []
                break
        url = users[0].get("profile_image_url", "") if users else ""
        self.urls[key] = (url, time.time())
        if users:                                           # remember under both keys
            self.urls[f"id:{users[0]['id']}"] = (url, time.time())
            self.urls[f"login:{users[0]['login']}"] = (url, time.time())
        return url

    async def _download(self, url):
        self.dir.mkdir(exist_ok=True)
        path = self.dir / (hashlib.sha1(url.encode()).hexdigest() + ".img")
        if path.exists():
            return path.read_bytes()
        async with self.http.get(url) as r:
            if r.status != 200:
                raise RuntimeError(f"picture download {r.status}")
            data = await r.read()
        if len(data) > 5_000_000:
            raise RuntimeError("picture too large")
        path.write_bytes(data)
        self._tidy()
        return data

    def _tidy(self, keep=500):
        """Keeps the cache folder from growing forever."""
        files = sorted(self.dir.glob("*.img"), key=lambda p: p.stat().st_mtime)
        for p in files[:-keep]:
            p.unlink(missing_ok=True)


class DemoAvatars:
    """Test mode: a stand-in picture with the viewer's initial, no Twitch needed."""
    available = True

    async def get(self, user_id=None, login=None, timeout=0):
        if not _pil() or not login:
            return None
        from images import demo_avatar
        return demo_avatar(login)

    async def get_url(self, url, name=None, timeout=0):
        if not _pil() or not name:
            return None
        from images import demo_avatar
        return demo_avatar(name)


EMOTE_URL = "https://static-cdn.jtvnw.net/emoticons/v2/{id}/static/light/2.0"


class Emotes:
    """Twitch emote pictures, cached on disk by emote id (they never change)."""

    def __init__(self, cache_dir: Path, http: aiohttp.ClientSession, url: str | None = None):
        self.dir = cache_dir
        self.http = http
        self.url = url or EMOTE_URL
        self.mem: dict[str, object] = {}

    async def _one(self, emote_id: str):
        if emote_id in self.mem:
            return self.mem[emote_id]
        Image = _pil()
        self.dir.mkdir(exist_ok=True)
        safe = "".join(c for c in emote_id if c.isalnum() or c in "_-")[:80]
        path = self.dir / f"{safe}.img"
        if path.exists():
            data = path.read_bytes()
        else:
            async with self.http.get(self.url.format(id=emote_id)) as r:
                if r.status != 200:
                    raise RuntimeError(f"emote {r.status}")
                data = await r.read()
            path.write_bytes(data)
        img = Image.open(io.BytesIO(data))
        img.load()
        self.mem[emote_id] = img
        return img

    async def enrich(self, parts: list[dict], timeout: float = 4.0) -> list[dict] | None:
        """Adds pictures to the emote parts of a message. None when there are none."""
        if not _pil() or not parts or not any(p["type"] == "emote" for p in parts):
            return None
        wanted = [p for p in parts if p["type"] == "emote"][:30]

        async def fetch(p):
            try:
                p["image"] = await self._one(str(p["id"]))
            except Exception as e:
                log.info("no emote picture for %s (%s)", p.get("name"), e)

        out = [dict(p) for p in parts]
        try:
            await asyncio.wait_for(asyncio.gather(*(fetch(p) for p in out if p["type"] == "emote")), timeout)
        except asyncio.TimeoutError:
            log.info("emotes took too long, printing names instead")
        return out if any(p.get("image") is not None for p in out) else None


class DemoEmotes:
    """Test mode: stand-in emote pictures."""

    async def enrich(self, parts, timeout=0):
        if not _pil() or not parts or not any(p["type"] == "emote" for p in parts):
            return None
        from images import demo_emote
        return [dict(p, image=demo_emote(p.get("name", "?"))) if p["type"] == "emote" else dict(p)
                for p in parts]
