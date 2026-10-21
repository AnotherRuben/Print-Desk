"""Slip logs: a lasting record of slips, printable again later.

Two of them: the datalink log (acars_log.jsonl: ACARS messages, notices and
weather from flights) and the stream log (stream_log.jsonl: every Twitch and
YouTube event, printed or not). Each entry keeps the slip exactly as it would
print, original times included. Profile pictures and emotes are kept as small
PNG files next to the log, so a reprint looks like the first print.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import time
from pathlib import Path

from slips import Line, Slip, render_text

log = logging.getLogger("slip_log")

LINE_FIELDS = ("kind", "text", "right", "align", "big", "bold", "inverse", "wide")


class SlipLog:
    def __init__(self, path: Path, keep: int = 1000, image_dir: Path | None = None, kind: str = "sim"):
        self.path = path
        self.keep = keep
        self.image_dir = image_dir
        self.slip_kind = kind               # "sim" slips skip the stream pause, "twitch" ones don't
        self.entries: list[dict] = []
        self._load()

    # ------------------------------------------------------------ pictures

    def _put_image(self, img) -> str | None:
        if not self.image_dir or img is None:
            return None
        try:
            buf = io.BytesIO()
            im = img.convert("RGBA") if img.mode not in ("RGB", "RGBA", "L", "1") else img
            im.save(buf, "PNG")
            data = buf.getvalue()
            name = hashlib.sha1(data).hexdigest()[:20] + ".png"
            self.image_dir.mkdir(exist_ok=True)
            p = self.image_dir / name
            if not p.exists():
                p.write_bytes(data)
            return name
        except Exception as e:
            log.info("couldn't keep a picture for the log: %s", e)
            return None

    def _get_image(self, name):
        if not name or not self.image_dir:
            return None
        try:
            from PIL import Image
            img = Image.open(self.image_dir / name)
            img.load()
            return img
        except Exception:
            return None

    def _tidy_images(self):
        if not self.image_dir or not self.image_dir.exists():
            return
        used = set()
        for e in self.entries:
            for d in e.get("lines", []):
                used.add(d.get("image_file"))
                used.update(p.get("image_file") for p in d.get("parts") or [])
        for p in self.image_dir.glob("*.png"):
            if p.name not in used:
                p.unlink(missing_ok=True)

    # ------------------------------------------------------------ slips in and out

    def _lines_out(self, slip: Slip):
        out = []
        for ln in slip.lines:
            d = {k: getattr(ln, k) for k in LINE_FIELDS if getattr(ln, k) not in ("", False, None)}
            d.setdefault("kind", "text")
            if ln.kind == "image":
                f = self._put_image(ln.image)
                if f:
                    d["image_file"] = f
                else:
                    continue                                 # no picture kept: leave the line out
            elif ln.kind == "rich" and ln.parts:
                parts = []
                for p in ln.parts:
                    if p.get("type") == "emote":
                        parts.append({"type": "emote", "name": p.get("name", ""),
                                      "image_file": self._put_image(p.get("image"))})
                    else:
                        parts.append({"type": "text", "text": p.get("text", "")})
                d["parts"] = parts
            out.append(d)
        return out

    def _slip_in(self, entry: dict, with_images=True) -> Slip:
        s = Slip(entry["title"], kind=self.slip_kind)
        for d in entry["lines"]:
            ln = Line(**{k: v for k, v in d.items() if k in LINE_FIELDS})
            if ln.kind == "image":
                ln.image = self._get_image(d.get("image_file")) if with_images else True
                if ln.image is None:
                    continue
            elif ln.kind == "rich":
                parts = []
                for p in d.get("parts") or []:
                    if p["type"] == "emote":
                        img = self._get_image(p.get("image_file")) if with_images else None
                        parts.append({"type": "emote", "name": p.get("name", ""), "image": img})
                    else:
                        parts.append(p)
                ln.parts = parts
            s.lines.append(ln)
        return s

    # ------------------------------------------------------------ file

    def _load(self):
        if not self.path.exists():
            return
        try:
            for raw in self.path.read_text(encoding="utf-8").splitlines():
                if raw.strip():
                    try:
                        self.entries.append(json.loads(raw))
                    except json.JSONDecodeError:
                        continue                      # a half-written line after a crash
        except OSError as e:
            log.warning("couldn't read %s: %s", self.path.name, e)
        self.entries = self.entries[-self.keep:]

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in self.entries),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("couldn't save %s: %s", self.path.name, e)

    def _append(self, entry):
        self.entries.append(entry)
        if len(self.entries) > self.keep:
            self.entries = self.entries[-self.keep:]
            self._save()
            self._tidy_images()
            return
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("couldn't write %s: %s", self.path.name, e)

    # ------------------------------------------------------------ use

    def has_hoppie(self, hoppie_id) -> dict | None:
        return next((e for e in reversed(self.entries) if e.get("hoppie_id") == hoppie_id), None)

    def add(self, slip: Slip, cat: str, flight: dict | None = None, **meta) -> dict:
        n = (self.entries[-1]["n"] + 1) if self.entries else 1
        entry = {"n": n, "at": time.time(), "cat": cat, "title": slip.title, "printed": 0,
                 **({"callsign": flight.get("callsign", ""),
                     "route": f"{flight['origin']}-{flight['destination']}"} if flight else {}),
                 "lines": self._lines_out(slip), **meta}
        self._append(entry)
        return entry

    def get(self, n) -> dict | None:
        return next((e for e in self.entries if e["n"] == n), None)

    def slip(self, entry: dict) -> Slip:
        return self._slip_in(entry)

    def mark_printed(self, entry: dict):
        entry["printed"] = entry.get("printed", 0) + 1
        self._save()

    def view(self, width=48, limit=300, fields=()):
        """Newest first, with the slip as text for the screen."""
        base = ("n", "at", "cat", "title", "printed", "from", "old", "callsign", "route") + tuple(fields)
        out = []
        for e in sorted(self.entries[-limit:], key=lambda e: e["at"], reverse=True):
            row = {k: e.get(k) for k in base}
            row["countable"] = bool(e.get("item")) and e.get("cat") not in ("hype", "away")
            row["text"] = render_text(self._slip_in(e, with_images=False), width)
            out.append(row)
        return out


def AcarsLog(path: Path) -> SlipLog:
    """The datalink log (kept for the flight code)."""
    return SlipLog(path, keep=1000, kind="sim")
