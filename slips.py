"""Slip model and layouts.

A slip is a list of lines with simple styling. Every output (console,
Pushover, thermal printer) renders the same slip in its own way, so the
layouts only live here.
"""
from __future__ import annotations

import random
import textwrap
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Line:
    kind: str = "text"          # text | rule | kv | blank
    text: str = ""
    right: str = ""             # right-hand column for kind="kv"
    align: str = "left"         # left | center
    big: bool = False           # double size on the printer
    bold: bool = False
    inverse: bool = False       # white on black on the printer
    wide: bool = False          # double width only (for kind="pre")
    image: object = None        # PIL image for kind="image" (a profile picture)
    style: str = ""             # kind="image": picture style for this one line (picture compare)
    parts: list | None = None   # kind="rich": message pieces, text and emote pictures


@dataclass
class Slip:
    title: str                  # short title for notifications and logs
    lines: list[Line] = field(default_factory=list)
    kind: str = "twitch"

    # small helpers so the layouts below read like the receipt itself
    def center(self, text, **style):
        self.lines.append(Line(text=text, align="center", **style))
        return self

    def text(self, text, **style):
        self.lines.append(Line(text=text, **style))
        return self

    def rule(self):
        self.lines.append(Line(kind="rule"))
        return self

    def blank(self):
        self.lines.append(Line(kind="blank"))
        return self

    def kv(self, left, right, **style):
        self.lines.append(Line(kind="kv", text=left, right=right, **style))
        return self

    def message(self, msg, rich=None):
        """A viewer message, quoted. With emote pictures (rich) it becomes one
        picture line on the printer; everywhere else emotes show as their name."""
        msg = " ".join((msg or "").split())
        if rich and any(p.get("image") is not None for p in rich if p["type"] == "emote"):
            plain = " ".join("".join(p["text"] if p["type"] == "text" else p["name"] for p in rich).split())
            self.lines.append(Line(kind="rich", text=f'"{plain}"', parts=rich))
        elif msg:
            self.lines.append(Line(text=f'"{msg}"'))
        return self


# ---------------------------------------------------------------- formatting

TIERS = {"1000": "Tier 1", "2000": "Tier 2", "3000": "Tier 3", "prime": "Prime"}


def tier_name(tier, platform="twitch"):
    if platform == "youtube":                   # YouTube membership levels have their own names
        t = str(tier or "").strip()
        return t if t and not t.isdigit() else "Member"
    return TIERS.get(str(tier), "Tier 1")


def is_yt(ev):
    return ev.get("platform") == "youtube"


def tier_short(tier):
    return {"1000": "T1", "2000": "T2", "3000": "T3"}.get(str(tier), "T1")


def money(amount, currency):
    return f"{currency.upper()} {amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def stamp(when: datetime | None = None):
    when = when or datetime.now()
    return when.strftime("%d-%m-%Y"), when.strftime("%H:%M")


def pick(options):
    if isinstance(options, str):
        return options
    options = [o for o in (options or []) if o]
    return random.choice(options) if options else ""


# ---------------------------------------------------------------- layouts

class Layouts:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.channel = cfg.get("general", {}).get("channel_title", "YOUR CHANNEL")
        self.text_cfg = cfg.get("text", {})

    def _head(self, slip: Slip, label: str, avatar=None, platform="twitch"):
        slip.center(f"{self.channel} - YOUTUBE" if platform == "youtube" else self.channel).rule()
        slip.center(label, big=True, inverse=True)
        if avatar is not None:
            slip.blank()                                # breathing room under the title
            slip.lines.append(Line(kind="image", image=avatar, align="center"))

    def _foot(self, slip: Slip, key: str | None = None):
        d, t = stamp(getattr(self, "when", None))          # "when": for events from the history
        slip.rule().kv(d, t)
        line = pick(self.text_cfg.get(key)) if key else ""
        if line:
            slip.center(line)
        return slip

    def sub(self, ev):
        yt = is_yt(ev)
        s = Slip(f"New {'member' if yt else 'sub'}: {ev['user']}")
        self._head(s, "NEW MEMBER" if yt else "NEW SUB", ev.get("avatar"), ev.get("platform"))
        s.center(ev["user"], big=True).center(tier_name(ev["tier"], ev.get("platform")))
        return self._foot(s, "sub_footer")

    def resub(self, ev):
        yt = is_yt(ev)
        s = Slip(f"{'Member' if yt else 'Resub'}: {ev['user']} ({ev['months']} months)")
        self._head(s, "MEMBER" if yt else "RESUB", ev.get("avatar"), ev.get("platform"))
        s.center(ev["user"], big=True)
        s.center(f"{tier_name(ev['tier'], ev.get('platform'))} - {ev['months']} months", bold=True)
        if ev.get("message"):
            s.rule().message(ev["message"], ev.get("message_rich"))
        return self._foot(s, "sub_footer")

    def gift(self, ev):
        count = ev["count"]
        gifter = ev["user"] or "Anonymous"
        yt = is_yt(ev)
        word = "GIFTED MEMBERS" if yt else "GIFT SUBS"
        s = Slip(f"{'Gifted members' if yt else 'Gift subs'}: {gifter} x{count}")
        self._head(s, f"{word} x{count}" if count > 1 else ("GIFTED MEMBER" if yt else "GIFT SUB"),
                   ev.get("avatar"), ev.get("platform"))
        s.center(gifter, big=True)
        info = tier_name(ev["tier"], ev.get("platform"))
        if ev.get("cumulative"):
            info += f"  -  {ev['cumulative']} gifted total"
        s.center(info)
        recipients = ev.get("recipients") or []
        if recipients:
            s.rule().text("RECIPIENTS", bold=True)
            # printed in two columns by the renderers
            s.lines.append(Line(kind="columns", text="\n".join(recipients)))
            missing = count - len(recipients)
            if missing > 0:
                s.text(f"+ {missing} more")
        return self._foot(s, "gift_footer")

    def bits(self, ev):
        s = Slip(f"{ev['bits']:,} bits: {ev['user']}")
        self._head(s, f"{ev['bits']:,} BITS", ev.get("avatar"))
        s.center(ev["user"], big=True)
        if ev.get("message"):
            s.rule().message(ev["message"], ev.get("message_rich"))
        return self._foot(s, "bits_footer")

    def tip(self, ev):
        amount = money(ev["amount"], ev["currency"])
        s = Slip(f"Tip {amount}: {ev['user']}")
        self._head(s, f"TIP {amount}", ev.get("avatar"))
        s.center(ev["user"], big=True)
        if ev.get("message"):
            s.rule().message(ev["message"])
        return self._foot(s, "tip_footer")

    def superchat(self, ev):
        amount = money(ev["amount"], ev["currency"])
        what = "SUPER STICKER" if ev.get("sticker") else "SUPER CHAT"
        s = Slip(f"{what.title()} {amount}: {ev['user']}")
        label = f"{what} {amount}"
        fits = len(label) + 2 <= 24                     # double size: 24 characters a line
        self._head(s, label if fits else what, ev.get("avatar"), "youtube")
        if not fits:
            s.center(amount, big=True)
        s.center(ev["user"], big=True)
        if ev.get("message"):
            s.rule().message(ev["message"])
        return self._foot(s, "tip_footer")

    def follow(self, ev):
        if is_yt(ev):
            s = Slip(f"New YouTube subscriber: {ev['user']}")
            self._head(s, "NEW SUBSCRIBER", ev.get("avatar"), "youtube")
            s.center(ev["user"], big=True)
            return self._foot(s, "follow_footer")
        s = Slip(f"New follower: {ev['user']}")
        self._head(s, "NEW FOLLOWER", ev.get("avatar"))
        s.center(ev["user"], big=True)
        return self._foot(s, "follow_footer")

    def follows(self, names):
        """A burst of follows (often a follow bot) on one slip."""
        s = Slip(f"{len(names)} new followers")
        self._head(s, f"FOLLOWERS x{len(names)}")
        s.rule().text("NEW FOLLOWERS", bold=True)
        s.lines.append(Line(kind="columns", text="\n".join(names)))
        return self._foot(s)

    def raid_follows(self, names, raider, lo, hi):
        """Follows that came in after a raid, on one list."""
        s = Slip(f"{len(names)} follows after {raider}'s raid")
        self._head(s, f"RAID FOLLOWS x{len(names)}")
        s.center(f"after the raid by {raider}", bold=True)
        s.center(f"first {hi} min" if lo == 0 else f"minute {lo} to {hi}")
        s.rule()
        s.lines.append(Line(kind="columns", text="\n".join(names)))
        return self._foot(s, "follow_footer")

    def raid(self, ev):
        s = Slip(f"Raid: {ev['user']} ({ev['viewers']})")
        self._head(s, "INCOMING RAID", ev.get("avatar"))
        s.center(ev["user"], big=True)
        s.center(f"{ev['viewers']} passengers on board", bold=True)
        return self._foot(s, "raid_footer")

    def hype_start(self, ev):
        s = Slip("Hype train started")
        self._head(s, "HYPE TRAIN")
        s.center("DEPARTING NOW", bold=True)
        s.center("Full receipt follows at the end")
        return self._foot(s)

    def hype_bundle(self, items, info):
        level = info.get("level") or 0
        s = Slip(f"Hype train level {level}")
        s.center(self.channel).rule()
        s.center("HYPE TRAIN", big=True, inverse=True)
        s.center(f"LEVEL {level} REACHED", bold=True)
        start, end = info.get("started"), info.get("ended")
        if start and end:
            mins = max(1, round((end - start).total_seconds() / 60))
            s.center(f"{start:%H:%M} - {end:%H:%M}   ({mins} min)")
        s.rule()

        if not self._bundle_items(s, items):
            s.center("Nothing above the print thresholds")

        tops = info.get("top_contributions") or []
        if tops:
            s.rule()
            names = {"bits": "TOP BITS", "subscription": "TOP SUBS", "other": "TOP OTHER"}
            for t in tops:
                s.kv(names.get(t.get("type"), "TOP"), t.get("user_name") or "?", bold=True)
        s.rule()
        for line in self.text_cfg.get("hype_footer", ["THANK YOU FOR RIDING, CHAT"]):
            s.center(line, bold=line == self.text_cfg.get("hype_footer", [""])[0])
        return s

    def _bundle_items(self, s: Slip, items) -> bool:
        """The itemised part of a hype train or break receipt. False when empty."""
        if not items:
            return False
        subs, members, bits, tips, sc = 0, 0, 0, {}, {}
        raids, follows, yt_subs = [], [], []
        money_lines = [it for it in items if it["kind"] not in ("raid", "follow", "follows")]
        if money_lines:
            s.kv("QTY ITEM", "FROM", bold=True)
        for it in items:
            k = it["kind"]
            yt = is_yt(it)
            if yt and k in ("sub", "resub", "gift"):
                n = it["count"] if k == "gift" else 1
                qty = n
                label = {"sub": "YT MEMBER", "gift": "YT GIFT MEMBER",
                         "resub": f"YT MEMBER ({it.get('months', 1)}m)"}[k]
                members += n
            elif k == "superchat":
                qty, label = 1, f"SUPER CHAT {money(it['amount'], it['currency'])}"
                sc[it["currency"].upper()] = sc.get(it["currency"].upper(), 0) + it["amount"]
            elif k == "sub":
                qty, label = 1, f"SUB {tier_short(it['tier'])}"
                subs += 1
            elif k == "resub":
                qty, label = 1, f"RESUB {tier_short(it['tier'])} ({it['months']}m)"
                subs += 1
            elif k == "gift":
                qty, label = it["count"], f"GIFT SUB {tier_short(it['tier'])}"
                subs += it["count"]
            elif k == "bits":
                qty, label = 1, f"BITS {it['bits']:,}"
                bits += it["bits"]
            elif k == "tip":
                qty, label = 1, f"TIP {money(it['amount'], it['currency'])}"
                tips[it["currency"].upper()] = tips.get(it["currency"].upper(), 0) + it["amount"]
            elif k == "raid":
                raids.append(it)
                continue
            elif k == "follow":
                (yt_subs if yt else follows).append(it.get("user") or "?")
                continue
            elif k == "follows":                  # a flood list collected while away
                follows.extend(it.get("names") or [])
                continue
            else:
                continue
            s.kv(f"{qty:>3} {label}", it.get("user") or "Anonymous")
            if k == "gift" and it.get("recipients"):
                # recipients indented under the gift line, two columns
                s.lines.append(Line(kind="columns",
                                    text="\n".join(f"    {n}" for n in it["recipients"])))
                missing = it["count"] - len(it["recipients"])
                if missing > 0:
                    s.text(f"    + {missing} more")
        if money_lines:
            s.rule()
            s.kv("TOTAL SUBS", str(subs))
            if members:
                s.kv("TOTAL YT MEMBERS", str(members))
            s.kv("TOTAL BITS", f"{bits:,}")
            if tips:
                s.kv("TOTAL TIPS", " / ".join(money(v, c) for c, v in tips.items()))
            if sc:
                s.kv("TOTAL SUPER CHATS", " / ".join(money(v, c) for c, v in sc.items()))
        if raids:
            if money_lines:
                s.rule()
            s.text("RAIDS", bold=True)
            for r in raids:
                s.kv(r.get("user") or "?", f"{r.get('viewers', 0)} viewers")
        above = bool(money_lines or raids)
        for title, names in (("NEW FOLLOWERS", follows), ("NEW YT SUBSCRIBERS", yt_subs)):
            if names:
                if above:
                    s.rule()
                above = True
                s.kv(title, str(len(names)), bold=True)
                s.lines.append(Line(kind="columns", text="\n".join(names)))
        return True

    def away_bundle(self, items, info):
        """Everything that came in while stream printing was paused, on one receipt."""
        s = Slip(f"While paused: {len(items)} event{'' if len(items) == 1 else 's'}")
        s.center(self.channel).rule()
        s.center(info.get("title") or "WHILE YOU WERE AWAY", big=True, inverse=True)
        start, end = info.get("started"), info.get("ended")
        if start and end:
            mins = max(1, round((end - start).total_seconds() / 60))
            s.center(f"{start:%H:%M} - {end:%H:%M}   ({mins} min)", bold=True)
        s.rule()
        if not self._bundle_items(s, items):
            s.center("Nothing came in")
        s.rule()
        for line in self.text_cfg.get("away_footer", ["Welcome back, captain"]):
            s.center(line)
        return s

    def summary(self, items, first: datetime, last: datetime):
        """A total of events picked by hand in the stream log."""
        s = Slip(f"Summary: {len(items)} events")
        s.center(self.channel).rule()
        s.center("SUMMARY", big=True, inverse=True)
        if first.date() == last.date():
            s.center(f"{first:%d-%m-%Y}  {first:%H:%M} - {last:%H:%M}", bold=True)
        else:
            s.center(f"{first:%d-%m %H:%M} - {last:%d-%m %H:%M}", bold=True)
        s.center(f"{len(items)} event{'s' if len(items) != 1 else ''}")
        s.rule()
        if not self._bundle_items(s, items):
            s.center("Nothing to add up")
        s.rule()
        d, t = stamp()
        s.kv(f"Printed {d}", t)
        for line in self.text_cfg.get("summary_footer", ["Thanks for the support"]):
            s.center(line)
        return s

    def picture_test(self, name, avatar, note=""):
        """A real profile picture (or a photo) on a slip, to judge how it prints."""
        s = Slip(f"Picture test: {name}", kind="manual")
        self._head(s, "PICTURE TEST", avatar)
        s.center(name, big=True)
        if avatar is None:
            s.center("No picture found", bold=True)
            s.center("(no such account, or Twitch's default picture)")
        if note:
            s.center(note)
        return self._foot(s)

    def picture_compare(self, name, img, styles, note=""):
        """One picture in every style, labelled, to pick a style on real paper."""
        s = Slip(f"Picture styles: {name}", kind="manual")
        s.center(self.channel).rule()
        s.center("PICTURE STYLES", big=True, inverse=True)
        s.center(name, bold=True)
        if note:
            s.center(note)
        for key, label in styles:
            s.rule().center(label.split(" - ")[0].upper(), bold=True)
            s.lines.append(Line(kind="image", image=img, align="center", style=key))
        return self._foot(s)

    def test(self, avatar=None):
        s = Slip("Print Desk test", kind="manual")
        self._head(s, "TEST SLIP", avatar)
        s.center("If you can read this,", bold=True).center("the printer works.")
        return self._foot(s)


# ---------------------------------------------------------------- plain text

def _wrap(text, width):
    return textwrap.wrap(text, width) or [""]


def _hard_wrap(text, width):
    return [text[i:i + width] for i in range(0, len(text), width)] or [""]


def _kv(left, right, width):
    if len(left) + len(right) + 1 <= width:
        return [left + " " * (width - len(left) - len(right)) + right]
    return [left[:width], right.rjust(width)[:width]]


def _columns(names, width):
    """Recipient list: one name per line, so long names never get cut off."""
    return [n[:width] for n in names]


def render_text(slip: Slip, width: int = 48) -> str:
    """Render a slip as monospace text (console and Pushover)."""
    out = []
    for ln in slip.lines:
        if ln.kind == "rule":
            out.append("-" * width)
        elif ln.kind == "blank":
            out.append("")
        elif ln.kind == "kv":
            out.extend(_kv(ln.text, ln.right, width))
        elif ln.kind == "columns":
            out.extend(_columns(ln.text.split("\n"), width))
        elif ln.kind == "image":
            out.append("[ profile picture ]".center(width).rstrip())
        elif ln.kind == "rich":
            out.extend(_wrap(ln.text, width))
        elif ln.kind == "pre":       # preformatted, keep spacing exactly
            out.extend(_hard_wrap(ln.text, width))
        else:
            text = ln.text
            if ln.inverse:
                text = f"[ {text} ]"
            for part in _wrap(text, width):
                out.append(part.center(width).rstrip() if ln.align == "center" else part)
    return "\n".join(out)
