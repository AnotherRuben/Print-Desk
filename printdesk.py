"""Print Desk - Twitch, StreamElements and SimBrief on a receipt printer.

  py printdesk.py run                   start (or double-click start.bat), then open
                                        http://localhost:8770 for the control panel
  py printdesk.py test all              fake stream events, nobody pays
  py printdesk.py test hype --output console
  py printdesk.py picture twitchname    a picture test with a real Twitch profile picture
  py printdesk.py picture photo.jpg     ...or with any picture file
  py printdesk.py picture photo.jpg --compare   the same picture in every style, to choose one
  py printdesk.py ofp                   print your latest SimBrief OFP
  py printdesk.py ofp --list            show which download formats exist
  py printdesk.py login                 Twitch login without the panel
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import aiohttp

import config_io
from app import App, HERE, TOKENS, build_output, make_history, output_list
from outputs import Dispatcher
from rules import Engine
from slips import Layouts

log = logging.getLogger("printdesk")


def make_outputs(cfg, override, http):
    outs = []
    for name in output_list(cfg, override):
        try:
            outs.append(build_output(name, cfg, http))
        except Exception as e:
            sys.exit(f"Output '{name}': {e}")
    return outs


async def one_shot(args, cfg, http):
    """test / ofp / login: do one thing and exit."""
    if args.command == "login":
        from twitch import TwitchAuth
        await TwitchAuth(cfg.get("twitch", {}).get("client_id", ""), TOKENS, http).device_login()
        return

    if args.command == "ofp" and args.list:
        from simbrief import SimBrief
        names = sorted(await SimBrief(cfg.get("simbrief", {}), http).downloads())
        print("Downloads in your latest OFP:\n  " + "\n  ".join(names))
        return

    history = make_history(HERE / "history.jsonl")
    dispatcher = Dispatcher(make_outputs(cfg, args.output, http), history)
    worker = asyncio.create_task(dispatcher.run())
    if args.command == "picture":
        await picture_tests(args.rest, cfg, http, dispatcher, args.compare)
    elif args.command == "ofp":
        from simbrief import SimBrief
        sb = SimBrief(cfg.get("simbrief", {}), http)
        await dispatcher.emit(sb.file_slip(Path(args.file)) if args.file else await sb.ofp_slip())
    elif args.scenario == "slip":
        from avatars import DemoAvatars
        pic = await DemoAvatars().get(login="Print Desk") if cfg["avatars"].get("enabled") else None
        await dispatcher.emit(Layouts(cfg).test(pic))
    else:
        from avatars import DemoAvatars
        from simulate import run_scenario
        cfg.setdefault("rules", {})["gift_collect_seconds"] = 1.5   # no need to wait in tests
        cfg["rules"]["print_follows"] = True                        # always show follows in tests
        cfg["rules"]["follow_burst_quiet_seconds"] = 1.5
        cfg["rules"]["raid_follow_first_minutes"] = 0.03              # ~2 s and ~5 s in tests
        cfg["rules"]["raid_follow_second_minutes"] = 0.08
        engine = Engine(cfg, dispatcher.emit, history)
        engine.avatars = DemoAvatars()                              # stand-in pictures
        from avatars import DemoEmotes
        engine.emotes = DemoEmotes()
        await run_scenario(args.scenario, engine)
        await engine.flush_gifts()
    await dispatcher.drain()
    worker.cancel()


async def picture_tests(targets, cfg, http, dispatcher, compare=False):
    """Real pictures on a slip: Twitch names (with your Twitch login) or picture files."""
    if not targets:
        sys.exit("Give one or more Twitch names or picture files, e.g.  py printdesk.py picture twitchname")
    from PIL import Image
    from avatars import Avatars
    from twitch import TwitchAuth
    auth = avatars = None
    a = cfg.get("avatars", {})
    note = (f"{a.get('size', 'medium')}, {a.get('style', 'photo')}, "
            f"lighten dark {'on' if a.get('lighten_dark', True) else 'off'}")
    for t in targets:
        path = Path(t.strip('"'))
        if path.suffix and path.exists():
            img = Image.open(path)
            img.load()
            name = path.stem
        else:
            if auth is None:
                try:
                    auth = TwitchAuth(cfg.get("twitch", {}).get("client_id", ""), TOKENS, http)
                except Exception as e:
                    sys.exit(f"Twitch names need the Twitch login: {e}")
                avatars = Avatars(HERE / "avatar_cache", http, lambda: auth)
            if not auth.has_tokens:
                sys.exit("Log in with Twitch first (Settings > Twitch, or py printdesk.py login).")
            name = t.lstrip("@")
            img = await avatars.get(login=name, timeout=10)
            print(f"{name}: {'picture found' if img else 'no picture (no such account, or the default one)'}")
        if compare:
            from images import STYLES
            await dispatcher.emit(Layouts(cfg).picture_compare(name, img, list(STYLES.items()),
                                                               f"{a.get('size', 'medium')} size"))
        else:
            await dispatcher.emit(Layouts(cfg).picture_test(name, img, note))


async def main(args):
    path = Path(args.config)
    first_run = not path.exists()
    async with aiohttp.ClientSession() as http:
        if args.command != "run":
            await one_shot(args, config_io.load(path), http)
            return

        app = App(path, http, args.output)
        await app.start()
        runner = None
        if app.cfg.get("panel", {}).get("enabled", True):
            from panel import Panel
            runner = await Panel(app).start()
        if first_run:
            log.info("First start: open http://localhost:%s/settings to set everything up.",
                     app.cfg.get("panel", {}).get("port", 8770))
        log.info("Print Desk running. Press Ctrl+C to stop.")
        try:
            await asyncio.Event().wait()
        finally:
            if runner:
                await runner.cleanup()


SCENARIOS = ["all", "sub", "resub", "gift", "bits", "tip", "raid", "follow", "youtube", "hype", "slip"]


def cli():
    p = argparse.ArgumentParser(description="Print Desk")
    p.add_argument("command", nargs="?", default="run", choices=["run", "login", "test", "ofp", "picture"])
    p.add_argument("rest", nargs="*", help="test: the scenario; picture: Twitch names or picture files")
    p.add_argument("--list", action="store_true", help="ofp: show the download formats in your latest OFP")
    p.add_argument("--compare", action="store_true", help="picture: print every picture style on one slip")
    p.add_argument("--file", help="ofp: print from a downloaded file instead of fetching from SimBrief")
    p.add_argument("--output", help="override output: console, pushover, printer (comma-separated)")
    p.add_argument("--config", default=str(HERE / "config.toml"))
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    args.scenario = (args.rest[0] if args.rest else "all") if args.command == "test" else "all"
    if args.command == "test" and args.scenario not in SCENARIOS:
        p.error(f"test: choose one of {', '.join(SCENARIOS)}")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(name)-14s %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(HERE / "printdesk.log", encoding="utf-8")])
    for noisy in ("aiohttp", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as e:
        log.error("%s", e)
        sys.exit(1)


if __name__ == "__main__":
    cli()
