"""Loading, saving and editing config.toml.

The settings page edits the same file you could edit by hand. Saving rewrites
it with the explanation comments below, so it stays readable.
"""
from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path

DEFAULTS = {
    "general": {"channel_title": "YOUR CHANNEL", "output": "pushover"},
    "pushover": {"app_token": "", "user_key": "", "width": 32, "sound": "cashregister", "device": ""},
    "printer": {"connection": "network", "host": "", "port": 9100, "usb_name": "",
                "width": 48, "feed_lines": 4, "cut": True, "paced": True},
    "twitch": {"enabled": True, "client_id": ""},
    "streamelements": {"enabled": True, "jwt": ""},
    "youtube": {"enabled": False, "jwt": "", "currency": "EUR", "print_subscribers": True},
    "avatars": {"enabled": True, "size": "medium", "style": "photo", "lighten_dark": True, "emotes": True,
                "events": ["sub", "resub", "gift", "bits", "tip", "raid", "follow"]},
    "hoppie": {"enabled": True, "logon": "", "print_mode": "except",
               "own_printer": ["A319", "A320", "A321", "A19N", "A20N", "A21N"], "print_delay_seconds": 8,
               "auto_print": ["cpdlc", "telex"],
               "warn_unavailable": True, "warn_after_seconds": 60, "timeout_minutes": 3},
    "departure": {"mode": "connect", "prefer": "atis", "print": True},
    "arrival": {"enabled": True, "distance_nm": 150, "prefer": "atis", "print": True},
    "vatsim": {"cid": ""},
    "autopause": {"enabled": False, "altitude_ft": 15000, "taxi_kts": 5, "parked_seconds": 120},
    "panel": {"enabled": True, "port": 8770, "host": "0.0.0.0"},
    "simbrief": {"username": "", "print_format": "Leonardo MD80 (ACARS)", "cut_at": "[OFP]",
                 "size": "double_width"},
    "rules": {
        "min_bits": 500, "min_raid_viewers": 5,
        "print_follows": False, "follow_burst_limit": 5, "follow_burst_quiet_seconds": 60,
        "raid_follow_lists": True, "raid_follow_first_minutes": 2, "raid_follow_second_minutes": 5,
        "gift_collect_seconds": 5, "gift_collect_max_seconds": 30,
        "bundle_hype_train": True, "hype_train_start_slip": False, "hype_train_safety_minutes": 45,
        "tip_unknown_currency": "skip",
        "tip_minimums": {"EUR": 5, "USD": 5, "GBP": 5, "CHF": 5, "CAD": 5, "AUD": 5, "NZD": 5,
                         "SEK": 50, "NOK": 50, "DKK": 40, "PLN": 20, "CZK": 125, "HUF": 2000,
                         "JPY": 750, "BRL": 25, "MXN": 100},
    },
    "text": {
        "sub_footer": ["Welcome to the crew"],
        "gift_footer": ["Generosity noted in the logbook"],
        "bits_footer": [], "tip_footer": [],
        "raid_footer": ["Welcome aboard, raiders"],
        "follow_footer": ["Thanks for the follow"],
        "hype_footer": ["THANK YOU FOR RIDING, CHAT", "No refunds. All sales final."],
        "away_footer": ["Welcome back, captain"],
        "summary_footer": ["Thanks for the support"],
    },
}

SECRETS = {("pushover", "app_token"), ("pushover", "user_key"), ("streamelements", "jwt"), ("youtube", "jwt"),
           ("hoppie", "logon")}

# Free-form tables: their keys are data, not settings
FREE_TABLES = {("rules", "tip_minimums")}

COMMENTS = {
    "general": "General",
    "general.output": 'where slips go: "console", "pushover", "printer", or several: "printer,pushover"',
    "pushover": "Pushover - pushover.net",
    "pushover.app_token": "create an application called Print Desk on pushover.net",
    "pushover.user_key": "shown on your pushover.net dashboard",
    "pushover.width": "characters per line on your phone",
    "printer": "Thermal printer",
    "printer.connection": '"network" (IP address) or "usb" (a printer installed in Windows)',
    "printer.host": "network: the printer's IP address",
    "printer.usb_name": "usb: the printer's name as shown in Windows",
    "printer.width": "characters per line, 48 for 80mm paper",
    "twitch": "Twitch - client_id from dev.twitch.tv/console/apps (client type Public)",
    "streamelements": "StreamElements - JWT token from your dashboard",
    "youtube": "YouTube, through StreamElements - the JWT token of your YouTube channel in StreamElements",
    "youtube.print_subscribers": "print new YouTube subscribers (on its own switch, separate from Twitch follows)",
    "youtube.currency": "currency for Super Chats when StreamElements doesn't say which",
    "avatars": "Profile pictures on slips",
    "avatars.size": '"small" (2 cm), "medium" (3.2 cm) or "large" (4.4 cm)',
    "avatars.style": '"photo" (smooth, best for faces), "crisp", "halftone" or "classic" (the first version)',
    "avatars.lighten_dark": "brighten very dark pictures so they print faster and cleaner",
    "avatars.events": "which slips get a picture: sub, resub, gift, bits, tip, raid, follow",
    "avatars.emotes": "Twitch emotes as pictures in viewer messages",
    "rules.print_follows": "print new followers (needs the Twitch login done once more)",
    "rules.follow_burst_limit": "more follows than this within a minute are bundled into one list",
    "rules.raid_follow_lists": "after a raid, follows go on a list printed at 2 and 5 minutes (works even with print_follows off)",
    "hoppie": "Datalink - Hoppie ACARS, read-only (Print Desk never sends anything)",
    "hoppie.logon": "your Hoppie logon code, the same one your aircraft uses",
    "hoppie.print_mode": '"log" (only the log), "auto" (always print), "except" (print unless you fly a type in own_printer) or "only" (print only on those types)',
    "hoppie.own_printer": "ICAO aircraft types for except/only, e.g. the Fenix family that prints by itself",
    "hoppie.print_delay_seconds": "wait about this long before printing, like an aircraft ACARS printer (a few seconds either way)",
    "departure.print": "print the departure weather when it arrives (it's in the datalink log either way)",
    "arrival.print": "print the arrival weather when it arrives (it's in the datalink log either way)",
    "hoppie.auto_print": "with print_mode auto: message types that print by themselves: cpdlc, telex, progress, position",
    "hoppie.timeout_minutes": "after 'clearance requested': print NO REPLY after this many minutes",
    "departure": "Departure weather",
    "departure.mode": '"connect" (when you connect to VATSIM at the departure airport), "start" (when the flight starts) or "off"',
    "departure.prefer": '"atis" (METAR if no ATIS is online) or "metar"',
    "arrival": "Arrival weather - needs you connected to VATSIM",
    "arrival.distance_nm": "print the arrival ATIS (or METAR) once within this distance",
    "arrival.prefer": '"atis" (METAR if no ATIS is online) or "metar"',
    "vatsim": "VATSIM",
    "vatsim.cid": "your VATSIM CID (optional): finds you on VATSIM whatever callsign you connect with",
    "autopause": "Pause stream printing in busy flight phases (needs you on VATSIM during a flight)",
    "autopause.altitude_ft": "paused from taxi until above this, and again below it (500 ft lower) until parked",
    "printer.paced": "send in small pieces and let each slip finish before the next (keep on unless printing is slow)",
    "panel": "Control panel (changes here need a restart)",
    "panel.host": '"0.0.0.0" = also reachable from your phone, "127.0.0.1" = this PC only',
    "simbrief": "SimBrief OFP printing",
    "simbrief.print_format": "name of the download to print",
    "simbrief.cut_at": "everything from this line down is left off",
    "simbrief.size": '"double_width" fills the paper, "normal" prints a narrow strip',
    "rules": "What prints",
    "rules.tip_unknown_currency": '"skip" or "print" for currencies not in the table below',
    "text": "Footer lines - one is picked at random when there are several",
}

HEADER = ("# Print Desk configuration\n"
          "# You can edit this in the control panel (Settings) or by hand.\n")


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v) if (k not in ("tip_minimums",)) else dict(v)
        else:
            out[k] = v
    return out


def load(path: Path) -> dict:
    """Reads config.toml (creating it on first run) merged over the defaults."""
    if not path.exists():
        save(path, DEFAULTS)
        return copy.deepcopy(DEFAULTS)
    with path.open("rb") as f:
        return deep_merge(DEFAULTS, tomllib.load(f))


# ------------------------------------------------------------------ writing

def _val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_val(x) for x in v) + "]"
    raise TypeError(f"can't write {type(v).__name__} to config")


def _key(k: str) -> str:
    return k if k.replace("_", "").replace("-", "").isalnum() and k.isascii() else json.dumps(k)


def dumps(cfg: dict) -> str:
    out = [HEADER]
    order = list(DEFAULTS) + [s for s in cfg if s not in DEFAULTS]
    for section in order:
        table = cfg.get(section)
        if not isinstance(table, dict):
            continue
        out.append("")
        if section in COMMENTS:
            out.append(f"# {COMMENTS[section]}")
        out.append(f"[{section}]")
        subtables = []
        for k, v in table.items():
            if isinstance(v, dict):
                subtables.append((k, v))
                continue
            line = f"{_key(k)} = {_val(v)}"
            note = COMMENTS.get(f"{section}.{k}")
            out.append(f"{line:<40} # {note}" if note else line)
        for k, v in subtables:
            out.append("")
            out.append(f"[{section}.{_key(k)}]")
            for kk, vv in v.items():
                out.append(f"{_key(kk)} = {_val(vv)}")
    return "\n".join(out) + "\n"


def save(path: Path, cfg: dict):
    text = dumps(cfg)
    tomllib.loads(text)                     # never write a file we can't read back
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ------------------------------------------------------------------ settings page

def public(cfg: dict) -> dict:
    """Config for the browser: secrets are never sent, only whether they are set."""
    out = copy.deepcopy(cfg)
    out["_secrets"] = {}
    for sec, key in SECRETS:
        val = out.get(sec, {}).get(key, "")
        out["_secrets"][f"{sec}.{key}"] = bool(val)
        if sec in out:
            out[sec][key] = ""
    return out


def _coerce(value, default, where):
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            raise ValueError(f"{where} must be a whole number")
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{where} must be a number")
    if isinstance(default, list):
        if isinstance(value, str):
            value = value.split("\n")
        return [str(x).strip() for x in (value or []) if str(x).strip()]
    if isinstance(default, str):
        return "" if value is None else str(value).strip()
    return value


def apply_update(current: dict, incoming: dict) -> dict:
    """Merges what the settings page sent into the current config, with type checks."""
    new = copy.deepcopy(current)
    for section, values in (incoming or {}).items():
        if section.startswith("_") or not isinstance(values, dict) or section not in DEFAULTS:
            continue
        for key, value in values.items():
            if (section, key) in SECRETS and (value is None or value == ""):
                continue                                   # empty = keep the saved secret
            if (section, key) in FREE_TABLES:
                table = {}
                for cur, amount in (value or {}).items():
                    cur = str(cur).strip().upper()
                    if not cur:
                        continue
                    try:
                        table[cur] = float(amount) if float(amount) % 1 else int(float(amount))
                    except (TypeError, ValueError):
                        raise ValueError(f"tip minimum for {cur} must be a number")
                new[section][key] = table
                continue
            default = DEFAULTS[section].get(key)
            if default is None:
                continue                                   # unknown key from the page: ignore
            new[section][key] = _coerce(value, default, f"{section}.{key}")
    return new


def changed_sections(old: dict, new: dict) -> set:
    return {s for s in set(old) | set(new) if old.get(s) != new.get(s)}
