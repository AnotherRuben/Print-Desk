"""Rules engine: decides what prints, groups gift subs, bundles hype trains.

Sources (Twitch, StreamElements, test mode) feed normalised event dicts into
Engine.handle(). Each dict has a "type":

  sub             user, tier                      (new sub, not gifted)
  gift_recipient  user, tier                      (one receiver of a gift)
  gift            user|None, count, tier, cumulative
  resub           user, tier, months, message
  cheer           user, bits, message
  tip             user, amount, currency, message, approved, status
  superchat       user, amount, currency, message  (YouTube)
  follow          user                            (YouTube: a channel subscribe)

Events can carry platform="youtube" (default Twitch); YouTube memberships use
the sub/resub/gift types and print with YouTube wording.
  raid            user, viewers
  hype_begin      level, started
  hype_progress   level
  hype_end        level, total, top_contributions, started, ended
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from slips import Layouts, Slip

log = logging.getLogger("rules")


@dataclass
class GiftGroup:
    user: str | None
    count: int
    tier: str
    cumulative: int | None
    recipients: list = field(default_factory=list)
    created: float = field(default_factory=time.monotonic)
    timer: asyncio.Task | None = None
    done: bool = False
    user_id: str | None = None
    login: str | None = None
    platform: str = "twitch"
    avatar_url: str | None = None


@dataclass
class AwayState:
    """Stream printing paused: everything is collected for one receipt."""
    started: datetime
    items: list = field(default_factory=list)       # events, for the receipt
    slips: list = field(default_factory=list)       # finished slips (a hype train receipt, raid lists)
    logged: list = field(default_factory=list)      # their stream log entries, relabelled on resume
    owner: str = "manual"                           # "manual" or "auto" (flight phases)
    reason: str = ""                                # e.g. "taxi out", shown in the panel


@dataclass
class HypeState:
    started: datetime
    level: int = 1
    items: list = field(default_factory=list)
    safety: asyncio.Task | None = None


class Engine:
    def __init__(self, cfg: dict, emit, history=None):
        """emit: async callable taking a Slip. history: callable(kind, text)."""
        self.configure(cfg)
        self.emit = emit
        self.history = history or (lambda kind, text: None)
        self.groups: list[GiftGroup] = []
        self.orphans: list[tuple[dict, float]] = []
        self.orphan_task: asyncio.Task | None = None
        self.hype: HypeState | None = None
        self.avatars = None                 # set by the app: Avatars or DemoAvatars
        self.emotes = None                  # set by the app: Emotes or DemoEmotes
        self.followed: set[str] = set()     # one follow slip per person while running
        self.follow_times: list[float] = []
        self.follow_overflow: list[str] = []
        self.follow_flush: asyncio.Task | None = None
        self.raid_window: dict | None = None    # follows collected after a raid
        self.away: AwayState | None = None      # stream printing paused
        self.recorder = None                    # set by the app: async (slip, status, item) -> log entry
        self.relabel = None                     # set by the app: (entries, status) -> None

    def configure(self, cfg: dict):
        """(Re)reads thresholds and texts; safe to call while running."""
        r = cfg.get("rules", {})
        self.min_bits = int(r.get("min_bits", 500))
        self.min_raid = int(r.get("min_raid_viewers", 5))
        self.gift_idle = float(r.get("gift_collect_seconds", 5))
        self.gift_max = float(r.get("gift_collect_max_seconds", 30))
        self.bundle = bool(r.get("bundle_hype_train", True))
        self.hype_start_slip = bool(r.get("hype_train_start_slip", False))
        self.hype_safety = float(r.get("hype_train_safety_minutes", 45)) * 60
        self.tip_min = {k.upper(): float(v) for k, v in r.get("tip_minimums", {}).items()}
        self.tip_unknown = r.get("tip_unknown_currency", "skip")
        self.layouts = Layouts(cfg)
        a = cfg.get("avatars", {})
        self.avatar_events = set(a.get("events", [])) if a.get("enabled", True) else set()
        self.emotes_on = bool(a.get("emotes", True))
        self.print_follows = bool(r.get("print_follows", False))
        self.yt_subscribers = bool(cfg.get("youtube", {}).get("print_subscribers", True))
        self.follow_burst = int(r.get("follow_burst_limit", 5))
        self.follow_quiet = float(r.get("follow_burst_quiet_seconds", 60))
        self.raid_lists = bool(r.get("raid_follow_lists", True))
        first = float(r.get("raid_follow_first_minutes", 2)) * 60
        second = float(r.get("raid_follow_second_minutes", 5)) * 60
        self.raid_marks = [first, max(first, second)]

    # ------------------------------------------------------------ entry

    async def handle(self, ev: dict):
        t = ev.get("type")
        handler = getattr(self, f"_on_{t}", None)
        if handler is None:
            log.debug("ignoring event %s", t)
            return
        await handler(ev)

    def skip(self, reason: str, item: dict | None = None):
        log.info("skipped: %s", reason)
        self.history("skipped", reason)
        if item and self.recorder:              # still in the stream log, printable by hand
            builder = getattr(self.layouts, item["kind"], None)
            if builder:
                asyncio.ensure_future(self._record(builder(item), "below minimum", item))

    async def _record(self, slip, status, item=None):
        if self.recorder:
            try:
                entry = await self.recorder(slip, status, item)
                if status == "paused" and self.away and entry is not None:
                    self.away.logged.append(entry)
            except Exception:
                log.exception("couldn't write the stream log")

    async def _out(self, slip, item=None):
        """A finished stream slip: printed now, or kept for the break receipt."""
        if self.away:
            self.away.slips.append(slip)
            await self._record(slip, "paused", item)
            return
        await self.emit(slip)
        await self._record(slip, "printed", item)

    # ------------------------------------------------------------ pause (break receipt)

    def start_away(self, owner: str = "manual", reason: str = ""):
        if not self.away:
            self.away = AwayState(started=datetime.now(), owner=owner, reason=reason)
            self.history("held", f"stream printing paused{' (' + reason + ')' if reason else ''}, "
                                 "collecting for one receipt")
        elif owner == "auto":                   # a pause you started (for pushback) is taken over
            self.away.owner, self.away.reason = "auto", reason

    def away_view(self):
        if not self.away:
            return None
        return {"since": self.away.started.strftime("%H:%M"), "owner": self.away.owner,
                "reason": self.away.reason,
                "items": [f"{it['kind']} {it.get('user') or ''}".strip() for it in self.away.items],
                "slips": [sl.title for sl in self.away.slips]}

    async def end_away(self, mode: str = "receipt") -> int:
        """mode: "receipt" (one receipt), "each" (every slip as normal) or "discard"."""
        await self.flush_gifts()                # gifts still being collected belong to the break
        state, self.away = self.away, None
        if not state:
            return 0
        n = len(state.items) + len(state.slips)
        if state.logged and self.relabel:
            self.relabel(state.logged, {"receipt": "on pause receipt", "each": "printed"}
                         .get(mode, "not printed (paused)"))
        if mode == "discard" or not n:
            self.history("discarded" if n else "panel", f"{n} held while paused" if n else "resumed")
            return n
        if mode == "each":
            for it in state.items:
                if it["kind"] == "follows":
                    await self.emit(self.layouts.follows(it["names"]))
                    continue
                await self._attach_avatar(it)
                await self.emit(getattr(self.layouts, it["kind"])(it))
        elif state.items:
            info = {"started": state.started, "ended": datetime.now(),
                    "title": "WHILE YOU WERE FLYING" if state.owner == "auto" else "WHILE YOU WERE AWAY"}
            slip = self.layouts.away_bundle(state.items, info)
            await self.emit(slip)
            await self._record(slip, "printed", {"kind": "away", "platform": "both"})
        for sl in state.slips:                  # hype train receipts and lists, as they were
            await self.emit(sl)
        return n

    # ------------------------------------------------------------ delivery

    async def deliver(self, item: dict):
        """A qualifying item: bundle it during a hype train, otherwise print."""
        if self.hype and self.bundle:
            self.hype.items.append(item)
            log.info("bundled into hype train: %s %s", item["kind"], item.get("user"))
            self.history("bundled", f"{item['kind']} {item.get('user')}")
            await self._attach_avatar(item)
            await self._record(getattr(self.layouts, item["kind"])(item), "hype train", item)
            return
        await self._attach_avatar(item)
        slip = getattr(self.layouts, item["kind"])(item)
        if self.away:
            self.away.items.append(item)
            self.history("held", f"{item['kind']} {item.get('user')}")
            await self._record(slip, "paused", item)
            return
        await self.emit(slip)
        await self._record(slip, "printed", item)

    async def _attach_avatar(self, item: dict):
        if self.emotes and self.emotes_on and item.get("message_parts"):
            item["message_rich"] = await self.emotes.enrich(item["message_parts"])
        kind = "tip" if item["kind"] == "superchat" else item["kind"]   # Super Chats follow the tip setting
        if not self.avatars or kind not in self.avatar_events:
            return
        if item.get("avatar_url"):                       # YouTube: StreamElements gives the picture URL
            item["avatar"] = await self.avatars.get_url(item["avatar_url"], item.get("user"))
        elif item.get("platform", "twitch") == "twitch" and (item.get("user_id") or item.get("login")):
            item["avatar"] = await self.avatars.get(item.get("user_id"), item.get("login"))

    @staticmethod
    def _who(ev):
        return {"user_id": ev.get("user_id"), "login": ev.get("login"),
                "platform": ev.get("platform", "twitch"), "avatar_url": ev.get("avatar_url")}

    # ------------------------------------------------------------ subs

    async def _on_sub(self, ev):
        await self.deliver({"kind": "sub", "user": ev["user"], "tier": ev["tier"], **self._who(ev)})

    async def _on_resub(self, ev):
        await self.deliver({"kind": "resub", "user": ev["user"], "tier": ev["tier"],
                            "months": ev.get("months") or 1, "message": ev.get("message", ""),
                            "message_parts": ev.get("message_parts"),
                            **self._who(ev)})

    # ------------------------------------------------------------ gift subs
    # Twitch sends one "gift" event (gifter + count) and a separate sub event
    # per recipient, without linking them. Recipients may arrive before or
    # after the gift event, so we match them up in a short window.

    async def _on_gift(self, ev):
        g = GiftGroup(ev.get("user"), int(ev.get("count") or 1), ev.get("tier", "1000"),
                      ev.get("cumulative"), user_id=ev.get("user_id"), login=ev.get("login"),
                      platform=ev.get("platform", "twitch"), avatar_url=ev.get("avatar_url"))
        self.groups.append(g)
        now = time.monotonic()
        keep = []
        for rec, ts in self.orphans:
            if (len(g.recipients) < g.count and now - ts <= self.gift_idle * 2
                    and self._fits(g, rec)):
                g.recipients.append(rec["user"])
            else:
                keep.append((rec, ts))
        self.orphans = keep
        if len(g.recipients) >= g.count:
            await self._finish_group(g)
        else:
            self._arm(g)

    @staticmethod
    def _fits(g: GiftGroup, rec: dict) -> bool:
        """A recipient belongs to a gift from the same platform (and gifter, when known)."""
        if g.platform != rec.get("platform", "twitch"):
            return False
        gifter = rec.get("gifter")
        return not gifter or not g.user or gifter.lower() == g.user.lower()

    async def _on_gift_recipient(self, ev):
        for g in self.groups:
            if not g.done and len(g.recipients) < g.count and self._fits(g, ev):
                g.recipients.append(ev["user"])
                if len(g.recipients) >= g.count:
                    await self._finish_group(g)
                else:
                    self._arm(g)
                return
        self.orphans.append((ev, time.monotonic()))
        if not self.orphan_task or self.orphan_task.done():
            self.orphan_task = asyncio.create_task(self._orphan_sweep())

    def _arm(self, g: GiftGroup):
        """(Re)start the idle timer; a big gift bomb keeps extending it, up to a cap."""
        if g.timer:
            g.timer.cancel()
        remaining_cap = self.gift_max - (time.monotonic() - g.created)
        wait = max(0.0, min(self.gift_idle, remaining_cap))
        g.timer = asyncio.create_task(self._group_timeout(g, wait))

    async def _group_timeout(self, g, wait):
        await asyncio.sleep(wait)
        await self._finish_group(g)

    async def _finish_group(self, g: GiftGroup):
        if g.done:
            return
        g.done = True
        if g.timer and g.timer is not asyncio.current_task():
            g.timer.cancel()
        self.groups = [x for x in self.groups if x is not g]
        await self.deliver({"kind": "gift", "user": g.user, "count": g.count, "tier": g.tier,
                            "cumulative": g.cumulative, "recipients": g.recipients,
                            "user_id": g.user_id, "login": g.login,
                            "platform": g.platform, "avatar_url": g.avatar_url})

    async def _orphan_sweep(self):
        """Recipients whose gift event never showed up still get printed."""
        while self.orphans:
            await asyncio.sleep(self.gift_idle)
            now = time.monotonic()
            stale = [(r, ts) for r, ts in self.orphans if now - ts >= self.gift_idle * 2]
            self.orphans = [(r, ts) for r, ts in self.orphans if now - ts < self.gift_idle * 2]
            for rec, _ in stale:
                await self.deliver({"kind": "gift", "user": rec.get("gifter"), "count": 1,
                                    "tier": rec.get("tier", "1000"), "cumulative": None,
                                    "recipients": [rec["user"]], "platform": rec.get("platform", "twitch")})

    async def flush_gifts(self):
        for g in list(self.groups):
            await self._finish_group(g)

    # ------------------------------------------------------------ bits, tips, raids

    async def _on_cheer(self, ev):
        bits = int(ev.get("bits") or 0)
        if bits < self.min_bits:
            return self.skip(f"bits {bits} from {ev.get('user')} (minimum {self.min_bits})",
                             {"kind": "bits", "user": ev.get("user") or "Anonymous", "bits": bits,
                              "message": ev.get("message", ""), **self._who(ev)})
        await self.deliver({"kind": "bits", "user": ev.get("user") or "Anonymous",
                            "bits": bits, "message": ev.get("message", ""),
                            "message_parts": ev.get("message_parts"), **self._who(ev)})

    async def _on_tip(self, ev):
        cur = (ev.get("currency") or "").upper()
        amount = float(ev.get("amount") or 0)
        if ev.get("status") not in (None, "", "success"):
            return self.skip(f"tip {cur} {amount} from {ev.get('user')} status {ev.get('status')}")
        if ev.get("approved") not in (None, "", "allowed", "approved"):
            return self.skip(f"tip {cur} {amount} from {ev.get('user')} not approved ({ev.get('approved')})")
        minimum = self.tip_min.get(cur)
        item = {"kind": "tip", "user": ev.get("user") or "Anonymous", "amount": amount,
                "currency": cur, "message": ev.get("message", ""), "platform": "twitch"}
        if minimum is None:
            if self.tip_unknown != "print":
                return self.skip(f"tip {cur} {amount} from {ev.get('user')} (currency not in table)", item)
        elif amount < minimum:
            return self.skip(f"tip {cur} {amount} from {ev.get('user')} (minimum {minimum})", item)
        await self.deliver({"kind": "tip", "user": ev.get("user") or "Anonymous", "amount": amount,
                            "currency": cur, "message": ev.get("message", ""),
                            "login": ev.get("user")})   # a tip name may be a Twitch name

    async def _on_superchat(self, ev):
        """YouTube Super Chats and Super Stickers: the same minimums as tips."""
        cur = (ev.get("currency") or "").upper()
        amount = float(ev.get("amount") or 0)
        minimum = self.tip_min.get(cur)
        what = "Super Sticker" if ev.get("sticker") else "Super Chat"
        item = {"kind": "superchat", "user": ev.get("user") or "Someone", "amount": amount, "currency": cur,
                "message": ev.get("message", ""), "sticker": bool(ev.get("sticker")), **self._who(ev)}
        if minimum is None:
            if self.tip_unknown != "print":
                return self.skip(f"{what} {cur} {amount} from {ev.get('user')} (currency not in table)", item)
        elif amount < minimum:
            return self.skip(f"{what} {cur} {amount} from {ev.get('user')} (minimum {minimum})", item)
        await self.deliver({"kind": "superchat", "user": ev.get("user") or "Someone", "amount": amount,
                            "currency": cur, "message": ev.get("message", ""),
                            "sticker": bool(ev.get("sticker")), **self._who(ev)})

    # ------------------------------------------------------------ follows
    # Off by default. One slip per person; a flood (often a follow bot) is
    # collected and printed as one list once it has been quiet for a while.

    async def _on_follow(self, ev):
        in_raid = self.raid_window is not None and ev.get("platform", "twitch") == "twitch"
        wanted = self.yt_subscribers if ev.get("platform") == "youtube" else self.print_follows
        if not (wanted or in_raid):
            if self.recorder and ev.get("user"):    # switched off: only in the log
                item = {"kind": "follow", "user": ev.get("user"), **self._who(ev)}
                await self._record(self.layouts.follow(item), "not printed (switched off)", item)
            return
        key = ev.get("user_id") or (ev.get("user") or "").lower()
        if key and ev.get("platform") == "youtube":
            key = "yt:" + key
        if not key or key in self.followed:
            return
        self.followed.add(key)
        if self.away:                                   # paused: on the break receipt
            item = {"kind": "follow", "user": ev.get("user") or "?", **self._who(ev)}
            self.away.items.append(item)
            await self._record(self.layouts.follow(item), "paused", item)
            return
        if in_raid:                                     # goes on the after-raid list instead
            self.raid_window["names"].append(ev.get("user") or "?")
            return
        now = time.monotonic()
        self.follow_times = [t for t in self.follow_times if now - t < 60] + [now]
        if len(self.follow_times) > self.follow_burst or self.follow_overflow:
            self.follow_overflow.append(ev.get("user") or "?")
            if self.follow_flush:
                self.follow_flush.cancel()
            self.follow_flush = asyncio.create_task(self._flush_follows())
            return
        item = {"kind": "follow", "user": ev.get("user") or "?", **self._who(ev)}
        await self._attach_avatar(item)
        await self._out(self.layouts.follow(item), item)    # not part of a hype train

    async def _flush_follows(self):
        await asyncio.sleep(self.follow_quiet)
        names, self.follow_overflow = self.follow_overflow, []
        if names:
            self.history("follows", f"{len(names)} follows in a burst")
            await self._out(self.layouts.follows(names), {"kind": "follows", "platform": "twitch", "names": names})

    async def _on_raid(self, ev):
        viewers = int(ev.get("viewers") or 0)
        if viewers < self.min_raid:
            return self.skip(f"raid from {ev.get('user')} with {viewers} (minimum {self.min_raid})",
                             {"kind": "raid", "user": ev["user"], "viewers": viewers, **self._who(ev)})
        # raids are not part of a hype train, so they always print on their own
        item = {"kind": "raid", "user": ev["user"], "viewers": viewers, **self._who(ev)}
        await self._attach_avatar(item)
        if self.away:                                   # paused: on the break receipt, follows too
            self.away.items.append(item)
            await self._record(self.layouts.raid(item), "paused", item)
            return
        await self._out(self.layouts.raid(item), item)
        if self.raid_lists:
            await self._open_raid_window(ev["user"])

    # Follows right after a raid are mostly the raiders. They go on one list
    # printed at 2 minutes, and a second list at 5 minutes if more came in.

    async def _open_raid_window(self, raider):
        if self.raid_window:                            # a second raid: close the first one now
            old, self.raid_window = self.raid_window, None
            old["task"].cancel()
            await self._print_raid_list(old, final=True)
        w = {"raider": raider, "names": [], "start": time.monotonic(), "task": None}
        w["task"] = asyncio.create_task(self._raid_window_run(w))
        self.raid_window = w

    async def _raid_window_run(self, w):
        try:
            done = 0.0
            for i, mark in enumerate(self.raid_marks):
                await asyncio.sleep(max(0.0, mark - done))
                done = mark
                last = i == len(self.raid_marks) - 1
                if last and self.raid_window is w:
                    self.raid_window = None
                await self._print_raid_list(w, final=last)
        except asyncio.CancelledError:
            pass

    async def _print_raid_list(self, w, final):
        names, w["names"] = w["names"], []
        if not names:
            return
        lo = w.get("printed_until", 0)
        hi = max(lo + 1, round((time.monotonic() - w["start"]) / 60))
        w["printed_until"] = hi
        self.history("follows", f"{len(names)} follows after the raid by {w['raider']}")
        await self._out(self.layouts.raid_follows(names, w["raider"], lo, hi),
                        {"kind": "follows", "platform": "twitch", "names": names})

    # ------------------------------------------------------------ hype train

    async def _on_hype_begin(self, ev):
        if self.hype:
            return
        self.hype = HypeState(started=ev.get("started") or datetime.now(),
                              level=int(ev.get("level") or 1))
        self.hype.safety = asyncio.create_task(self._hype_safety())
        log.info("hype train started, bundling on")
        self.history("hype", "started")
        if self.hype_start_slip:
            await self._out(self.layouts.hype_start(ev), {"kind": "hype", "platform": "twitch"})

    async def _on_hype_progress(self, ev):
        if not self.hype:  # script started mid-train
            await self._on_hype_begin(ev)
        self.hype.level = max(self.hype.level, int(ev.get("level") or 1))

    async def _on_hype_end(self, ev):
        if not self.hype:
            await self._on_hype_begin(ev)
        await self.flush_gifts()           # open gift groups belong to the train
        state, self.hype = self.hype, None
        if state.safety and state.safety is not asyncio.current_task():
            state.safety.cancel()
        info = {"level": int(ev.get("level") or state.level),
                "started": ev.get("started") or state.started,
                "ended": ev.get("ended") or datetime.now(),
                "top_contributions": ev.get("top_contributions") or []}
        log.info("hype train ended at level %s with %d items", info["level"], len(state.items))
        await self._out(self.layouts.hype_bundle(state.items, info), {"kind": "hype", "platform": "twitch"})

    async def _hype_safety(self):
        """If the end event never arrives, don't hold slips back forever."""
        await asyncio.sleep(self.hype_safety)
        if self.hype:
            log.warning("no hype train end event received, printing bundle anyway")
            await self._on_hype_end({"level": self.hype.level})
