"""Fake events for test mode. Nobody spends money, the engine can't tell."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta


def _with_login(ev):
    """Fake events get a login so test mode can show stand-in profile pictures."""
    if ev.get("user") and "login" not in ev and ev.get("type") not in ("tip",):
        ev = {**ev, "login": ev["user"].lower()}
    return ev


async def run_scenario(name: str, engine, pause=0.3):
    async def send(ev, wait=pause):
        await engine.handle(_with_login(ev))
        await asyncio.sleep(wait)

    async def gift_bomb(gifter, names, tier="1000", recipients_first=False):
        gift = {"type": "gift", "user": gifter, "count": len(names), "tier": tier, "cumulative": 247}
        recs = [{"type": "gift_recipient", "user": n, "tier": tier} for n in names]
        for ev in (recs + [gift]) if recipients_first else ([gift] + recs):
            await engine.handle(_with_login(ev))
            await asyncio.sleep(0.05)

    if name in ("sub", "all"):
        await send({"type": "sub", "user": "kaasbaas_nl", "tier": "1000"})
    if name in ("resub", "all"):
        msg = "still waiting for you to land that 737 without bouncing deskLand deskLand"
        await send({"type": "resub", "user": "fokker_fan", "tier": "1000", "months": 14, "message": msg,
                    "message_parts": [{"type": "text", "text": "still waiting for you to land that 737 without bouncing "},
                                      {"type": "emote", "id": "demo1", "name": "deskLand"},
                                      {"type": "text", "text": " "},
                                      {"type": "emote", "id": "demo1", "name": "deskLand"}]})
    if name in ("gift", "all"):
        await gift_bomb("stroopwafel77", ["pilot_pete", "windmill_wim", "avgeek_anna", "rotterdam_rik",
                                          "klm_kees", "taxiway_tom", "squawk7000", "hollandse_hans",
                                          "flaps_full", "nightshift_nina"])
        # Twitch sometimes delivers the recipients first; this one tests that
        await gift_bomb("brit_on_vatsim", ["tea_and_tcas", "heathrow_hal"], recipients_first=True)
        await asyncio.sleep(engine.gift_idle + 0.5)
    if name in ("bits", "all"):
        await send({"type": "cheer", "user": "vatsim_vera", "bits": 1000, "message": "for the go-around fund deskHype",
                    "message_parts": [{"type": "text", "text": "for the go-around fund "},
                                      {"type": "emote", "id": "demo2", "name": "deskHype"}]})
        await send({"type": "cheer", "user": "avgeek_anna", "bits": 100, "message": "cheer100"})  # skipped
    if name in ("tip", "all"):
        await send({"type": "tip", "user": "schiphol_sam", "amount": 10, "currency": "EUR",
                    "message": "buy yourself a better landing"})
        await send({"type": "tip", "user": "stockholm_stig", "amount": 20, "currency": "SEK", "message": ""})  # skipped
    if name in ("raid", "all"):
        await send({"type": "raid", "user": "CloudBaseTV", "viewers": 42})
        await send({"type": "raid", "user": "tiny_streamer", "viewers": 3})  # skipped
        for i, n in enumerate(["cloud_crew_1", "cloud_crew_2", "cloud_crew_3", "cloud_crew_4"]):
            await engine.handle({"type": "follow", "user": n, "user_id": f"r{i}"})
        await asyncio.sleep(engine.raid_marks[0] + 0.3)                # first list
        await engine.handle({"type": "follow", "user": "late_raider", "user_id": "r9"})
        await asyncio.sleep(engine.raid_marks[1] - engine.raid_marks[0] + 0.3)   # second list
    if name in ("follow", "all"):
        await send({"type": "follow", "user": "plane_spotter_nl", "user_id": "f1"})
        await send({"type": "follow", "user": "plane_spotter_nl", "user_id": "f1"})      # refollow: ignored
        for i in range(10):                                                               # a follow bot
            await engine.handle({"type": "follow", "user": f"b0t_{i:03d}", "user_id": f"bot{i}"})
        await asyncio.sleep(engine.follow_quiet + 0.5)
    if name in ("youtube", "all"):
        # shaped like StreamElements' channel.activities messages, through the real parser
        from streamelements import normalise_youtube
        n = iter(range(1, 100))

        async def yt(kind, wait=pause, **data):
            ev = normalise_youtube({"provider": "youtube", "type": kind, "_id": f"yt{next(n)}",
                                    "data": {"avatar": "https://example.invalid/a.jpg", **data}}, "EUR")
            if ev:                                      # the real connection skips what it doesn't print
                await engine.handle(ev)
            await asyncio.sleep(wait)

        await yt("sponsor", displayName="Cockpit Carla", tier="Captain")
        await yt("sponsor", displayName="Jumpseat Joost", tier="First Officer", amount=7,
                 message="seven months of watching you miss the centreline")
        await yt("communityGiftPurchase", displayName="Gate Agent Gerda", amount=3, tier="Captain", wait=0.05)
        for r in ("Tower Tess", "Ground Gijs", "Apron Anouk"):
            await yt("sponsor", displayName=r, gifted=True, sender="Gate Agent Gerda", tier="Captain", wait=0.05)
        await asyncio.sleep(engine.gift_idle + 0.5)
        await yt("superchat", displayName="Pushback Pim", amount=10, currency="EUR",
                 message="for the fuel bill")
        await yt("superchat", displayName="Sticker Sanne", amount=5, currency="EUR", sticker=True)
        await yt("superchat", displayName="Tiny Tom", amount=2, currency="EUR", message="hi")   # skipped
        await yt("subscriber", displayName="Spotter Sem")
        await yt("follow", displayName="ignored")                                             # not a YouTube event we print
    if name in ("hype", "all"):
        start = datetime.now()
        await send({"type": "hype_begin", "level": 1, "started": start})
        await send({"type": "sub", "user": "kaasbaas_nl", "tier": "1000"})
        await send({"type": "resub", "user": "fokker_fan", "tier": "1000", "months": 14, "message": "choo choo"})
        await gift_bomb("stroopwafel77", [f"viewer_{i}" for i in range(1, 11)])
        await send({"type": "cheer", "user": "vatsim_vera", "bits": 1000, "message": ""})
        await send({"type": "raid", "user": "CloudBaseTV", "viewers": 42})   # prints on its own
        await send({"type": "tip", "user": "schiphol_sam", "amount": 10, "currency": "EUR", "message": ""})
        await send({"type": "hype_progress", "level": 3})
        await send({"type": "cheer", "user": "squawk7000", "bits": 500, "message": ""})
        await send({"type": "cheer", "user": "lurker", "bits": 50, "message": ""})  # skipped
        await send({"type": "tip", "user": "brit_on_vatsim", "amount": 5, "currency": "GBP", "message": ""})
        await asyncio.sleep(0.5)
        await send({"type": "hype_end", "level": 4, "total": 9500,
                    "started": start, "ended": start + timedelta(minutes=17),
                    "top_contributions": [{"user_name": "stroopwafel77", "type": "subscription", "total": 5000},
                                          {"user_name": "vatsim_vera", "type": "bits", "total": 1000}]})
