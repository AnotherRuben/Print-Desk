"""The running Print Desk: outputs, rules and connections, rebuilt live when
settings change in the control panel."""
from __future__ import annotations

import asyncio
import time
import json
import logging
from datetime import datetime
from pathlib import Path

import aiohttp

import config_io
from outputs import ConsoleOutput, Dispatcher, EscPosOutput, PushoverOutput
from rules import Engine
from state import State

log = logging.getLogger("printdesk")
HERE = Path(__file__).resolve().parent
SOURCES = {"twitch": "Twitch", "streamelements": "StreamElements", "youtube": "YouTube"}
TOKENS = HERE / "twitch_tokens.json"
OUTPUT_NAMES = ("console", "pushover", "printer")


def make_history(path: Path, state: State | None = None):
    def history(kind, text, body=None):
        entry = {"time": datetime.now().isoformat(timespec="seconds"), "kind": kind, "text": text}
        if body:
            entry["slip"] = body
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if state:
            state.add_log(kind, text)
    return history


def output_list(cfg, override=None):
    raw = override or cfg.get("general", {}).get("output", "console")
    return [n.strip() for n in raw.split(",") if n.strip()]


def build_output(name, cfg, http):
    if name == "console":
        return ConsoleOutput(int(cfg.get("printer", {}).get("width", 48)))
    if name == "pushover":
        return PushoverOutput(cfg.get("pushover", {}), http)
    if name == "printer":
        return EscPosOutput(cfg.get("printer", {}), cfg.get("avatars", {}))
    raise ValueError(f"unknown output '{name}'")


class App:
    def __init__(self, config_path: Path, http: aiohttp.ClientSession, output_override=None):
        self.path = config_path
        self.http = http
        self.override = output_override
        self.cfg = config_io.load(config_path)
        self.state = State()
        self.history = make_history(HERE / "history.jsonl", self.state)
        self.dispatcher = Dispatcher([], self.history, self.state)
        self.engine = Engine(self.cfg, self.dispatcher.emit, self.history)
        from acars_log import SlipLog
        self.stream_log = SlipLog(HERE / "stream_log.jsonl", keep=2000,
                                  image_dir=HERE / "stream_log_images", kind="twitch")
        self.engine.recorder = self.record_stream
        self.engine.relabel = self.relabel_stream
        from backfill import Backfill
        self.backfill = Backfill(self, HERE / "last_running.txt")
        self.twitch_auth = None
        from avatars import Avatars
        self.avatars = Avatars(HERE / "avatar_cache", http, lambda: self.twitch_auth,
                               self.cfg.get("twitch", {}).get("mock_api_url"))
        self.engine.avatars = self.avatars
        from avatars import Emotes
        self.engine.emotes = Emotes(HERE / "emote_cache", http)
        self.sources: dict[str, asyncio.Task] = {}
        from flight import Flight
        self.flight = Flight(self, HERE / "acars_log.jsonl")
        self.login_task: asyncio.Task | None = None
        self.login_info: dict = {}

    async def record_stream(self, slip, status, item=None):
        """Every Twitch and YouTube event goes into the stream log, printed or not."""
        item = item or {}
        keep = {k: v for k, v in item.items()
                if k not in ("avatar", "message_rich", "message_parts") and
                isinstance(v, (str, int, float, bool, list, type(None)))}
        return self.stream_log.add(slip, item.get("kind", "other"), status=status,
                                   platform=item.get("platform", "twitch"), user=item.get("user"), item=keep)

    async def resume_stream(self, mode: str = "receipt") -> int:
        n = await self.engine.end_away(mode)
        n += await self.dispatcher.resume(print_held=mode != "discard")
        return n

    def relabel_stream(self, entries, status):
        for e in entries:
            e["status"] = status
        self.stream_log._save()

    # ------------------------------------------------------------ outputs

    def output_names(self):
        return ",".join(output_list(self.cfg, self.override))

    def rebuild_outputs(self):
        for name in OUTPUT_NAMES:
            self.state.conn.pop(name, None)
        outs = []
        for name in output_list(self.cfg, self.override):
            try:
                out = build_output(name, self.cfg, self.http)
                outs.append(out)
                detail = out.describe() if hasattr(out, "describe") else "ready"
                self.state.status(name, "ok", detail)
            except Exception as e:
                self.state.status(name, "error", str(e))
        self.dispatcher.outputs = outs

    # ------------------------------------------------------------ sources

    async def _guard(self, name, coro):
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("%s stopped: %s", name, e)
            self.state.status(name, "error", str(e)[:200])

    def _status(self, name):
        return lambda level, detail="": self.state.status(name, level, detail)

    async def stop_source(self, name):
        task = self.sources.pop(name, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def start_source(self, name):
        await self.stop_source(name)
        section = self.cfg.get(name, {})
        if not section.get("enabled", True):
            self.state.status(name, "off", "switched off")
            return
        st = self._status(name)
        try:
            if name == "twitch":
                from twitch import TwitchAuth, TwitchEventSub
                auth = TwitchAuth(section.get("client_id", ""), TOKENS, self.http)
                if not auth.has_tokens:
                    raise ValueError("not logged in (Settings > Twitch > Log in with Twitch)")
                self.twitch_auth = auth
                runner = TwitchEventSub(auth, self.http, self.engine.handle, section, status=st).run()
            elif name == "youtube":
                from streamelements import StreamElementsYouTube
                st("warn", "connecting")
                runner = StreamElementsYouTube(section, self.http, self.engine.handle, status=st,
                                               sample_file=HERE / "youtube_events.jsonl").run()
            else:
                from streamelements import StreamElementsTips
                st("warn", "connecting")
                runner = StreamElementsTips(section, self.http, self.engine.handle, status=st).run()
        except Exception as e:
            st("error", str(e))
            return
        self.sources[name] = asyncio.create_task(self._guard(name, runner))

    # ------------------------------------------------------------ lifecycle

    async def start(self):
        self.rebuild_outputs()
        self.worker = asyncio.create_task(self.dispatcher.run())
        for name in SOURCES:
            await self.start_source(name)
        last = self.backfill.last_seen()
        self.heartbeat = asyncio.create_task(self.backfill.heartbeat())
        if last is None:                               # first start with the stream log: the last day
            asyncio.create_task(self._catch_up(time.time() - 24 * 3600))
        elif time.time() - last > 120:                 # Print Desk was off for a while
            asyncio.create_task(self._catch_up(last))

    async def _catch_up(self, since: float):
        try:
            res = await self.backfill.fetch(since)
        except Exception as e:
            log.warning("couldn't check for missed events: %s", e)
            return
        n = sum(v for v in res.values() if isinstance(v, int))
        if n:
            self.history("panel", f"{n} event(s) came in while Print Desk was off - see the stream log")

    async def update_config(self, incoming: dict) -> list[str]:
        """Called by the settings page. Saves, then applies only what changed."""
        new = config_io.apply_update(self.cfg, incoming)
        changed = config_io.changed_sections(self.cfg, new)
        if not changed:
            return ["Nothing changed."]
        config_io.save(self.path, new)
        self.cfg = new
        done = ["Saved."]
        if changed & {"general", "pushover", "printer", "avatars"}:
            self.rebuild_outputs()
            done.append("Outputs updated.")
        if changed & {"rules", "text", "general", "avatars", "youtube"}:
            self.engine.configure(new)
            done.append("Rules updated.")
        for name in SOURCES:
            if name in changed:
                await self.start_source(name)
                done.append(f"{SOURCES[name]} reconnected.")
        if "panel" in changed:
            done.append("Panel address changes apply after restarting Print Desk.")
        return done

    # ------------------------------------------------------------ Twitch login from the panel

    async def twitch_login_start(self) -> dict:
        from twitch import TwitchAuth
        auth = TwitchAuth(self.cfg.get("twitch", {}).get("client_id", ""), TOKENS, self.http)
        d = await auth.start_device()
        if self.login_task and not self.login_task.done():
            self.login_task.cancel()
        self.login_info = {"status": "waiting", "uri": d["verification_uri"], "code": d["user_code"]}
        self.login_task = asyncio.create_task(self._finish_login(auth, d))
        return self.login_info

    async def _finish_login(self, auth, d):
        try:
            login = await auth.finish_device(d)
            self.login_info = {"status": "done", "login": login}
            self.history("panel", f"logged in to Twitch as {login}")
            await self.start_source("twitch")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.login_info = {"status": "error", "message": str(e)}

    async def twitch_logout(self):
        await self.stop_source("twitch")
        self.twitch_auth = None
        if TOKENS.exists():
            TOKENS.unlink()
        self.login_info = {}
        self.state.status("twitch", "error", "not logged in (Settings > Twitch > Log in with Twitch)")

    def twitch_account(self) -> dict:
        return {"logged_in": TOKENS.exists(), "login": self.state.conn.get("twitch", {}).get("detail")
                if self.state.conn.get("twitch", {}).get("level") == "ok" else None,
                "pending": self.login_info}
