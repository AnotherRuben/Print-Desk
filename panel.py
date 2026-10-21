"""Control panel: a small web page served by Print Desk itself.

Open it in a browser, add it as an OBS custom browser dock, or use it on your
phone over your home network.
"""
from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web

import config_io
from outputs import EscPosOutput
from printers import lan_ip, list_local_printers, scan_network
from slips import Layouts, render_text

log = logging.getLogger("panel")
HERE = Path(__file__).resolve().parent


def rules_summary(cfg):
    r = cfg.get("rules", {})
    tips = r.get("tip_minimums", {})
    tip = f"{tips.get('EUR', 5):g}+ EUR/USD/GBP" if tips else "any"
    return [
        ["Subs and resubs", "always"],
        ["Gift subs", "with recipients"],
        ["Bits", f"{r.get('min_bits', 500)}+"],
        ["Tips", tip],
        ["Raids", f"{r.get('min_raid_viewers', 5)}+ viewers"],
        ["Follows", "on" if r.get("print_follows") else "off"],
        ["Hype train", "bundled" if r.get("bundle_hype_train", True) else "separate slips"],
    ] + ([["YouTube", f"members, gifts, Super Chats {tip}" + (", subscribers" if cfg.get("youtube", {}).get("print_subscribers", True) else "")]] if cfg.get("youtube", {}).get("enabled") else [])


class Panel:
    def __init__(self, app):
        self.app = app
        self.ofp_busy = False

    @property
    def state(self):
        return self.app.state

    @property
    def dispatcher(self):
        return self.app.dispatcher

    def web_app(self):
        w = web.Application()
        r = w.router
        r.add_get("/", self.page("panel.html"))
        r.add_get("/settings", self.page("settings.html"))
        r.add_get("/log", self.page("log.html"))
        r.add_get("/stream-log", self.page("log.html"))
        r.add_get("/api/log", self.api_log)
        r.add_post("/api/log/print/{n}", self.api_log_print)
        r.add_post("/api/log/summary", self.api_log_summary)
        r.add_post("/api/log/fetch", self.api_log_fetch)
        r.add_get("/api/state", self.api_state)
        r.add_post("/api/pause", self.api_pause)
        r.add_post("/api/resume", self.api_resume)
        r.add_post("/api/discard", self.api_discard)
        r.add_post("/api/test", self.api_test)
        r.add_post("/api/reprint", self.api_reprint)
        r.add_post("/api/ofp", self.api_ofp)
        # settings
        r.add_get("/api/config", self.api_config)
        r.add_post("/api/config", self.api_config_save)
        r.add_get("/api/printers/local", self.api_local_printers)
        r.add_post("/api/printers/scan", self.api_scan)
        r.add_post("/api/printers/test", self.api_printer_test)
        r.add_get("/api/twitch", self.api_twitch)
        r.add_post("/api/twitch/login", self.api_twitch_login)
        r.add_post("/api/twitch/logout", self.api_twitch_logout)
        r.add_get("/api/simbrief/downloads", self.api_simbrief_downloads)
        # flight / datalink
        r.add_post("/api/flight/start", self.api_flight_start)
        r.add_post("/api/flight/end", self.api_flight_end)
        r.add_post("/api/flight/station", self.api_flight_station)
        r.add_post("/api/flight/requested", self.api_flight_requested)
        r.add_post("/api/flight/cancel", self.api_flight_cancel)
        r.add_post("/api/flight/print/{id}", self.api_flight_print)
        r.add_post("/api/flight/arrival", self.api_flight_arrival)
        r.add_post("/api/flight/departure", self.api_flight_departure)
        r.add_post("/api/autopause", self.api_autopause)
        r.add_post("/api/picture-test", self.api_picture_test)
        return w

    async def start(self):
        pc = self.app.cfg.get("panel", {})
        host = pc.get("host", "0.0.0.0")
        port = int(pc.get("port", 8770))
        runner = web.AppRunner(self.web_app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        log.info("Control panel: http://localhost:%d", port)
        if host == "0.0.0.0":
            log.info("On your phone (same network): http://%s:%d", lan_ip(), port)
        return runner

    # ---------------------------------------------------------------- helpers

    def page(self, name):
        async def handler(request):
            return web.FileResponse(HERE / name, headers={"Cache-Control": "no-store"})
        return handler

    def ok(self, msg, **extra):
        return web.json_response({"ok": True, "message": msg, **extra})

    def fail(self, msg, status=400):
        return web.json_response({"ok": False, "message": msg}, status=status)

    async def body(self, request):
        try:
            return await request.json()
        except Exception:
            return {}

    # ---------------------------------------------------------------- main page

    async def api_state(self, request):
        s, cfg = self.state, self.app.cfg
        sb = cfg.get("simbrief", {})
        away = self.app.engine.away_view()
        return web.json_response({
            "paused": bool(away) or s.paused,
            "held": [x.title for x in s.held],
            "away": away,
            "queue": self.dispatcher.queue.qsize(),
            "last": s.last.title if s.last else None,
            "last_text": render_text(s.last, 48) if s.last else None,
            "conn": s.conn,
            "log": list(s.log)[:60],
            "rules": rules_summary(cfg),
            "outputs": self.app.output_names(),
            "ofp_busy": self.ofp_busy,
            "simbrief": bool(sb.get("username") or sb.get("userid")),
            "flight": self.app.flight.view(),
            "autopause": self.app.flight.autopause_view(),
        })

    async def api_pause(self, request):
        self.app.engine.start_away()
        self.dispatcher.history("panel", "stream printing paused")
        return self.ok("Paused. Everything is collected for one receipt.")

    async def api_resume(self, request, mode=None):
        mode = mode or (await self.body(request)).get("mode") or "receipt"
        if mode not in ("receipt", "each", "discard"):
            return self.fail("Unknown way to resume.")
        self.app.flight.manual_resume()
        n = await self.app.resume_stream(mode)
        self.dispatcher.history("panel", "stream printing resumed")
        if not n:
            return self.ok("Resumed. Nothing came in.")
        return self.ok({"receipt": f"Resumed. Printing {n} event(s) on one receipt.",
                        "each": f"Resumed. Printing {n} slip(s).",
                        "discard": f"Resumed. {n} event(s) not printed (still in the stream log)."}[mode])

    async def api_discard(self, request):
        return await self.api_resume(request, "discard")

    async def api_test(self, request):
        if not self.dispatcher.outputs:
            return self.fail("No working output. Check Settings.")
        await self.dispatcher.queue.put(Layouts(self.app.cfg).test(await self._demo_pic()))
        return self.ok("Test slip sent.")

    async def _demo_pic(self):
        if not self.app.cfg.get("avatars", {}).get("enabled", True):
            return None
        from avatars import DemoAvatars
        return await DemoAvatars().get(login="Print Desk")

    async def api_reprint(self, request):
        if not self.state.last:
            return self.fail("Nothing printed yet.")
        await self.dispatcher.queue.put(self.state.last)
        return self.ok(f"Reprinting: {self.state.last.title}")

    async def api_ofp(self, request):
        if self.ofp_busy:
            return self.fail("Already fetching the OFP.")
        from simbrief import SimBrief, flight_from_ofp
        self.ofp_busy = True
        try:
            sb = SimBrief(self.app.cfg.get("simbrief", {}), self.app.http)
            slip = await sb.ofp_slip()
        except Exception as e:
            log.warning("OFP print failed: %s", e)
            return self.fail(str(e).split("\n")[0])
        finally:
            self.ofp_busy = False
        await self.dispatcher.queue.put(slip)
        msg = f"Printing {slip.title}"
        try:                                    # printing the OFP also starts the flight
            info = flight_from_ofp(sb.last)
            current = self.app.flight.info
            if not current or current.get("ofp_id") != info.get("ofp_id") or current["callsign"] != info["callsign"]:
                await self.app.flight.start(info)
                msg += f" - flight {info['callsign']} started"
        except Exception:
            log.exception("couldn't start flight from OFP")
        return self.ok(msg)

    # ---------------------------------------------------------------- settings

    async def api_config(self, request):
        return web.json_response(config_io.public(self.app.cfg))

    async def api_config_save(self, request):
        try:
            done = await self.app.update_config(await self.body(request))
        except ValueError as e:
            return self.fail(str(e))
        except Exception as e:
            log.exception("saving settings failed")
            return self.fail(f"Couldn't save: {e}", 500)
        return self.ok(" ".join(done), config=config_io.public(self.app.cfg))

    async def api_local_printers(self, request):
        try:
            return web.json_response({"ok": True, **list_local_printers()})
        except Exception as e:
            return self.fail(str(e))

    async def api_scan(self, request):
        b = await self.body(request)
        try:
            res = await scan_network(self.app.http, b.get("subnet") or None, int(b.get("port") or 9100))
        except Exception as e:
            return self.fail(str(e))
        return web.json_response({"ok": True, **res})

    async def api_printer_test(self, request):
        """Prints a test slip on a printer before you save it, to tell printers apart."""
        b = await self.body(request)
        pcfg = {**self.app.cfg.get("printer", {}), **{k: v for k, v in b.items()
                                                      if k in ("connection", "host", "port", "usb_name")}}
        try:
            out = EscPosOutput(pcfg, self.app.cfg.get("avatars", {}))
            slip = Layouts(self.app.cfg).test(await self._demo_pic())
            slip.center(out.describe())
            await out.send(slip)
        except Exception as e:
            return self.fail(f"Test failed: {e}")
        return self.ok(f"Test slip sent to {out.describe()}.")

    async def api_twitch(self, request):
        return web.json_response(self.app.twitch_account())

    async def api_twitch_login(self, request):
        b = await self.body(request)
        if b.get("client_id"):   # use the ID typed on the page, even before saving
            await self.app.update_config({"twitch": {"client_id": b["client_id"]}})
        try:
            info = await self.app.twitch_login_start()
        except Exception as e:
            return self.fail(str(e))
        return self.ok("Approve Print Desk on Twitch.", **info)

    async def api_twitch_logout(self, request):
        await self.app.twitch_logout()
        return self.ok("Logged out of Twitch.")

    async def api_simbrief_downloads(self, request):
        from simbrief import SimBrief
        try:
            names = sorted(await SimBrief(self.app.cfg.get("simbrief", {}), self.app.http).downloads())
        except Exception as e:
            return self.fail(str(e).split("\n")[0])
        return web.json_response({"ok": True, "downloads": names})

    # ---------------------------------------------------------------- flight / datalink

    async def api_flight_start(self, request):
        from simbrief import SimBrief
        try:
            info = await SimBrief(self.app.cfg.get("simbrief", {}), self.app.http).flight()
        except Exception as e:
            log.exception("start flight failed")          # full details in printdesk.log
            return self.fail(str(e).split("\n")[0])
        await self.app.flight.start(info)
        return self.ok(f"Flight {info['callsign']} {info['origin']}-{info['destination']} started.")

    async def api_flight_end(self, request):
        await self.app.flight.stop()
        return self.ok("Flight ended. Datalink and arrival checks stopped.")

    async def api_flight_station(self, request):
        b = await self.body(request)
        st = str(b.get("station", "")).strip().upper()
        f = self.app.flight
        if not f.info:
            return self.fail("Start a flight first.")
        if not st.replace("_", "").isalnum() or not 3 <= len(st) <= 10:
            return self.fail("That doesn't look like a station name.")
        f.station, f.station_online, f.warned_unavailable = st, None, False
        return self.ok(f"Watching {st}. Status updates at the next check.")

    async def api_flight_requested(self, request):
        try:
            msg = await self.app.flight.clearance_requested()
        except ValueError as e:
            return self.fail(str(e).capitalize() + ".")
        return self.ok(msg)

    async def api_flight_cancel(self, request):
        self.app.flight.cancel_request()
        return self.ok("Request timer stopped.")

    async def api_flight_print(self, request):
        mid = int(request.match_info["id"])
        f = self.app.flight
        m = next((m for m in f.messages if m["id"] == mid), None)
        if not m or not f.info:
            return self.fail("Message not found.")
        await f.print_message(m)
        return self.ok(f"Printing message from {m['from']}.")

    async def api_picture_test(self, request):
        """Prints real Twitch profile pictures with the current picture settings."""
        import re
        body = await self.body(request)
        raw = str(body.get("names") or "")
        compare = bool(body.get("compare"))
        names = [n.lstrip("@") for n in re.split(r"[\s,]+", raw) if n.strip()][:5]
        if not names:
            return self.fail("Type one or more Twitch names.")
        if not self.app.twitch_auth or not self.app.twitch_auth.access:
            return self.fail("Log in with Twitch first (Settings > Twitch); pictures are looked up with it.")
        if not self.dispatcher.outputs:
            return self.fail("No working output. Check Settings.")
        a = self.app.cfg.get("avatars", {})
        note = (f"{a.get('size', 'medium')}, {a.get('style', 'photo')}, "
                f"lighten dark {'on' if a.get('lighten_dark', True) else 'off'}")
        found = 0
        for n in names:
            pic = await self.app.avatars.get(login=n, timeout=8)
            found += pic is not None
            if compare and pic is not None:
                from images import STYLES
                slip = Layouts(self.app.cfg).picture_compare(n, pic, list(STYLES.items()),
                                                             f"{a.get('size', 'medium')} size")
            else:
                slip = Layouts(self.app.cfg).picture_test(n, pic, note)
            await self.dispatcher.queue.put(slip)
        return self.ok(f"Printing {len(names)} picture test(s)" +
                       ("." if found == len(names) else f", {len(names) - found} without a picture."))

    async def api_autopause(self, request):
        on = bool((await self.body(request)).get("enabled"))
        await self.app.update_config({"autopause": {"enabled": on}})
        top = int(float(self.app.cfg.get("autopause", {}).get("altitude_ft", 15000)))
        return self.ok(f"Auto-pause on: from taxi to {top:,} ft and from {top:,} ft to parked." if on
                       else "Auto-pause off.")

    async def api_flight_arrival(self, request):
        return await self._weather("arr")

    async def api_flight_departure(self, request):
        return await self._weather("dep")

    def _book(self, request):
        return "stream" if request.query.get("book") == "stream" else "acars"

    async def api_log(self, request):
        width = int(self.app.cfg.get("printer", {}).get("width", 48) or 48)
        if self._book(request) == "stream":
            return web.json_response({"book": "stream",
                                      "entries": self.app.stream_log.view(width, limit=600,
                                                                          fields=("status", "platform", "user")),
                                      "paused": bool(self.app.engine.away)})
        f = self.app.flight
        cur = f.info or {}
        return web.json_response({"book": "acars", "entries": f.log.view(width),
                                  "current": {"callsign": cur.get("callsign"),
                                              "route": f"{cur['origin']}-{cur['destination']}" if cur else None},
                                  "print_mode": "auto" if f.auto else "log"})

    async def api_log_print(self, request):
        stream = self._book(request) == "stream"
        book = self.app.stream_log if stream else self.app.flight.log
        try:
            entry = book.get(int(request.match_info["n"]))
        except ValueError:
            entry = None
        if not entry:
            return self.fail("That isn't in the log any more.")
        if not self.dispatcher.outputs:
            return self.fail("No working output. Check Settings.")
        if stream:                              # printed by hand: straight out, even while paused
            book.mark_printed(entry)
            slip = book.slip(entry)
            slip.kind = "manual"
            await self.dispatcher.queue.put(slip)
        else:
            await self.app.flight.print_entry(entry)
        return self.ok(f"Printing {entry['title']}.")

    async def api_log_fetch(self, request):
        """Fetches missed events from the StreamElements history into the stream log."""
        import time as _t
        try:
            hours = min(14 * 24, max(1, float((await self.body(request)).get("hours") or 24)))
        except (TypeError, ValueError):
            hours = 24
        if not self.app.backfill._sources():
            return self.fail("Needs a StreamElements JWT (Settings > StreamElements, or YouTube).")
        res = await self.app.backfill.fetch(_t.time() - hours * 3600)
        names = {"twitch": "Twitch", "youtube": "YouTube"}
        parts = []
        for k, v in res.items():
            d = self.app.backfill.details.get(k, {})
            if not isinstance(v, int):
                parts.append(f"{names[k]}: {v}")
                continue
            got = sum(d.get("types", {}).values())
            line = f"{names[k]}: StreamElements sent {got} event(s), {v} new in the log"
            if d.get("already"):
                line += f", {d['already']} already there"
            if d.get("not_used"):
                line += " (not used: " + ", ".join(f"{t} x{n}" for t, n in d["not_used"].items()) + ")"
            if not got:
                line += " - " + "; ".join(d.get("tries", []))
            parts.append(line)
        ok = any(isinstance(v, int) for v in res.values())
        return web.json_response({"ok": ok, "message": ". ".join(parts) + ".", "result": res,
                                  "details": self.app.backfill.details})

    async def api_log_summary(self, request):
        """One receipt adding up the stream log entries you picked."""
        from datetime import datetime
        ns = (await self.body(request)).get("ns") or []
        book = self.app.stream_log
        entries = [e for e in (book.get(int(n)) for n in ns if str(n).isdigit()) if e]
        # receipts are already totals of other entries, so they're left out
        picked = [e for e in entries if e.get("item") and e["cat"] not in ("hype", "away")]
        if not picked:
            return self.fail("Pick some events first (receipts don't count, their events are in the log).")
        if not self.dispatcher.outputs:
            return self.fail("No working output. Check Settings.")
        picked.sort(key=lambda e: e["at"])
        slip = Layouts(self.app.cfg).summary([e["item"] for e in picked],
                                             datetime.fromtimestamp(picked[0]["at"]),
                                             datetime.fromtimestamp(picked[-1]["at"]))
        slip.kind = "manual"
        await self.dispatcher.queue.put(slip)
        left = len(entries) - len(picked)
        return self.ok(f"Printing a summary of {len(picked)} event(s)." +
                       (f" {left} receipt(s) left out." if left else ""))

    async def _weather(self, which):
        f = self.app.flight
        if not f.info:
            return self.fail("Start a flight first.")
        try:
            await f.print_weather(which, f.arrival.get("distance") if which == "arr" else None, print_now=True)
        except Exception as e:
            return self.fail(f"Couldn't get the weather: {e}")
        return self.ok(f"Printing {f.info['origin' if which == 'dep' else 'destination']} weather.")
