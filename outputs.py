"""Outputs: console, Pushover and an ESC/POS thermal printer (network or USB)."""
from __future__ import annotations

import asyncio
import io
import logging

import aiohttp

from slips import Slip, render_text, _columns, _hard_wrap, _kv, _wrap

log = logging.getLogger("outputs")


class ConsoleOutput:
    name = "console"

    def __init__(self, width=48):
        self.width = width

    async def send(self, slip: Slip):
        bar = "=" * self.width
        print(f"\n{bar}\n{render_text(slip, self.width)}\n{bar}\n", flush=True)


class PushoverOutput:
    """Sends the slip as a monospace Pushover notification."""
    name = "pushover"
    URL = "https://api.pushover.net/1/messages.json"
    LIMIT = 1024  # Pushover's maximum message length

    def __init__(self, cfg: dict, http: aiohttp.ClientSession):
        self.token = cfg.get("app_token", "")
        self.user = cfg.get("user_key", "")
        self.width = int(cfg.get("width", 32))
        self.sound = cfg.get("sound", "")
        self.device = cfg.get("device", "")
        self.http = http
        if not self.token or not self.user:
            raise ValueError("Pushover keys not set (Settings > Pushover)")

    def chunks(self, text: str):
        parts, cur = [], ""
        for line in text.split("\n"):
            if len(cur) + len(line) + 1 > self.LIMIT and cur:
                parts.append(cur)
                cur = ""
            cur = f"{cur}\n{line}" if cur else line
        if cur:
            parts.append(cur)
        return parts

    @staticmethod
    def picture(slip: Slip):
        """The profile picture as a JPEG, attached in colour to the notification."""
        img = next((ln.image for ln in slip.lines if ln.kind == "image" and ln.image is not None), None)
        if img is None:
            return None
        try:
            from PIL import Image
            pic = img.convert("RGB")
            pic.thumbnail((600, 600), Image.LANCZOS)
            buf = io.BytesIO()
            pic.save(buf, "JPEG", quality=85)
            return buf.getvalue()
        except Exception as e:
            log.info("couldn't attach picture: %s", e)
            return None

    async def send(self, slip: Slip):
        parts = self.chunks(render_text(slip, self.width))
        picture = self.picture(slip)
        for i, part in enumerate(parts, 1):
            title = slip.title if len(parts) == 1 else f"{slip.title} ({i}/{len(parts)})"
            data = {"token": self.token, "user": self.user, "message": part,
                    "title": title[:250], "monospace": "1"}
            if self.sound:
                data["sound"] = self.sound
            if self.device:
                data["device"] = self.device
            if picture and i == 1:
                form = aiohttp.FormData()
                for k, v in data.items():
                    form.add_field(k, v)
                form.add_field("attachment", picture, filename="avatar.jpg", content_type="image/jpeg")
                body = form
            else:
                body = data
            async with self.http.post(self.URL, data=body) as r:
                if r.status != 200:
                    raise RuntimeError(f"Pushover {r.status}: {await r.text()}")


class EscPosOutput:
    """ESC/POS receipt printer, over the network (port 9100) or as a USB printer
    installed in Windows (sent RAW through the print spooler)."""
    name = "printer"

    ESC, GS = b"\x1b", b"\x1d"

    def __init__(self, cfg: dict, avatar_cfg: dict | None = None):
        a = avatar_cfg or {}
        self.avatar_size = a.get("size", "medium")
        self.lighten = bool(a.get("lighten_dark", True))
        self.style = a.get("style", "photo")
        self.connection = cfg.get("connection", "network")
        self.host = str(cfg.get("host", "")).strip()
        self.port = int(cfg.get("port", 9100))
        self.usb_name = str(cfg.get("usb_name", "")).strip()
        self.width = int(cfg.get("width", 48))
        self.feed = int(cfg.get("feed_lines", 4))
        self.cut = bool(cfg.get("cut", True))
        self.paced = bool(cfg.get("paced", True))
        if self.connection == "usb" and not self.usb_name:
            raise ValueError("no USB printer chosen (Settings > Printer)")
        if self.connection != "usb" and not self.host:
            raise ValueError("no printer IP address set (Settings > Printer)")

    def _image(self, img, style: str = "") -> bytes:
        if img is None:
            return b""
        try:
            from images import SIZES, escpos_raster, to_bitmap
        except ImportError:
            return b""                                   # Pillow missing: print without picture
        dots = min(SIZES.get(self.avatar_size, 256), self.width * 12)
        bw = to_bitmap(img, dots, lighten_dark=self.lighten, style=style or self.style)
        return escpos_raster(bw) + b"\n"

    def _rich(self, ln) -> bytes:
        """A message with emotes, printed as one picture. Falls back to text."""
        try:
            from images import escpos_raster, render_rich
            return escpos_raster(render_rich(ln.parts, self.width * 12)) + b"\n"
        except Exception as e:
            log.info("emote message as text instead (%s)", e)
            return b"".join(self._enc(r) + b"\n" for r in _wrap(ln.text, self.width))

    def describe(self):
        return self.usb_name if self.connection == "usb" else f"{self.host}:{self.port}"

    def _enc(self, text: str) -> bytes:
        return text.encode("cp437", errors="ignore")

    def build(self, slip: Slip) -> bytes:
        E, G = self.ESC, self.GS
        out = bytearray(E + b"@")                       # initialise
        half = self.width // 2
        for ln in slip.lines:
            align = 1 if ln.align == "center" else 0
            out += E + b"a" + bytes([align])
            if ln.kind == "rule":
                out += self._enc("-" * self.width) + b"\n"
                continue
            if ln.kind == "blank":
                out += b"\n"
                continue
            if ln.kind == "columns":
                for row in _columns(ln.text.split("\n"), self.width):
                    out += self._enc(row) + b"\n"
                continue
            if ln.kind == "image":
                out += self._image(ln.image, ln.style)
                continue
            if ln.kind == "rich":
                out += self._rich(ln)
                continue
            if ln.kind == "pre":
                if ln.wide:
                    out += G + b"!\x10"                 # double width, normal height
                for row in _hard_wrap(ln.text, half if ln.wide else self.width):
                    out += self._enc(row) + b"\n"
                out += G + b"!\x00"
                continue
            big = ln.big and len(ln.text) <= half    # too long names fall back to bold
            if ln.bold or (ln.big and not big):
                out += E + b"E\x01"
            if big:
                out += G + b"!\x11"                     # double width + height
            if ln.inverse:
                out += G + b"B\x01"
            width = half if big else self.width
            if ln.kind == "kv":
                rows = _kv(ln.text, ln.right, width)
            else:
                text = f" {ln.text} " if ln.inverse else ln.text
                # short lines go out as-is: wrapping would trim the padding of inverted labels
                rows = [text] if len(text) <= width else _wrap(text, width)
            for row in rows:
                out += self._enc(row) + b"\n"
            out += G + b"!\x00" + G + b"B\x00" + E + b"E\x00"
        out += E + b"a\x00" + E + b"d" + bytes([self.feed])
        if self.cut:
            out += G + b"V\x01"                         # partial cut
        return bytes(out)

    async def send(self, slip: Slip):
        from pathlib import Path
        from printers import PrinterLock, send_local, send_network
        data = self.build(slip)
        async with PrinterLock(Path(__file__).resolve().parent / "printer.lock"):
            if self.connection == "usb":
                await send_local(self.usb_name, data)
            else:
                await send_network(self.host, self.port, data, paced=self.paced)
                # the printer is still printing what it received: give it time before the next slip
                await asyncio.sleep(min(4.0, 0.4 + len(data) / 25000))


class Dispatcher:
    """Queue so slips go out one by one, in order, even during a hype train."""

    def __init__(self, outputs, history, state=None):
        self.outputs = outputs
        self.history = history
        self.state = state
        self.queue: asyncio.Queue = asyncio.Queue()

    async def emit(self, slip: Slip):
        """Stream slips wait while paused; sim and manual slips always go out."""
        if self.state and self.state.paused and slip.kind == "twitch":
            self.state.held.append(slip)
            self.history("held", slip.title)
            return
        await self.queue.put(slip)

    async def resume(self, print_held=True):
        held, self.state.held = self.state.held, []
        self.state.paused = False
        if print_held:
            for slip in held:
                await self.queue.put(slip)
        elif held:
            self.history("discarded", f"{len(held)} held slips")
        return len(held)

    async def run(self):
        while True:
            slip = await self.queue.get()
            self.history("printed", slip.title, render_text(slip, 48))
            if self.state:
                self.state.last = slip
            if not self.outputs:
                log.warning("no working output - slip not sent: %s", slip.title)
                self.history("failed", f"no working output: {slip.title}")
            for out in list(self.outputs):
                for attempt in range(3):
                    try:
                        await out.send(slip)
                        if self.state:
                            where = out.describe() + " - " if hasattr(out, "describe") else ""
                            self.state.status(out.name, "ok", f"{where}last: {slip.title}")
                        break
                    except Exception as e:
                        log.warning("%s failed (attempt %d): %s", out.name, attempt + 1, e)
                        if self.state:
                            self.state.status(out.name, "error", str(e)[:120])
                        await asyncio.sleep(2 * (attempt + 1))
                else:
                    log.error("%s gave up on: %s", out.name, slip.title)
                    self.history("failed", f"{out.name}: {slip.title}")
            self.queue.task_done()

    async def drain(self):
        await self.queue.join()
