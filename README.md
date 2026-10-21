# Print Desk

Turns Twitch subs, gift subs, bits, raids and StreamElements tips into receipt slips,
and prints your SimBrief OFP on request. Slips go to a thermal printer (network or USB),
to your phone through Pushover, or both.

## What you need

- Windows 10 or 11
- A thermal receipt printer that speaks ESC/POS (most 80 mm receipt printers do),
  on your network or over USB. No printer yet? Pushover on your phone works for testing.
- Python 3.11 or newer. `install.bat` helps you get it (see below).

## Install

1. **Download Print Desk**: on GitHub, press the green **Code** button and choose
   **Download ZIP**, then unzip it somewhere you'll keep it (for example `Documents\Print Desk`).
2. **Double-click `install.bat`**.
   - Python already there: it installs the libraries Print Desk uses, and you're done.
   - No Python yet: it offers to install it with winget, Windows' own installer (press **Y**).
     Close the window when it's finished and double-click `install.bat` once more.
   - Rather do it yourself? Get the "Windows installer (64-bit)" from
     [python.org/downloads/windows](https://www.python.org/downloads/windows/), and in the
     installer **tick "Add python.exe to PATH"** before clicking *Install Now*. Then run
     `install.bat`.
3. **Double-click `start.bat`**. A black window opens: that's Print Desk running. Keep it open
   while you stream or fly.
4. Open **http://localhost:8770/settings** in your browser and fill in what you need.
   Everything can be set there, including the Twitch login. Changes apply straight away.

Windows may ask whether Python may use the network (the firewall): allow it on private
networks if you want to use the panel from your phone.

**Updating:** download the new version and copy its files over your folder. Your settings
(`config.toml`) and Twitch login (`twitch_tokens.json`) stay, because they're not part of
the download. Run `install.bat` again in case something new is needed.

## What prints

| Event | Rule (changeable in Settings) |
|---|---|
| New sub, resub | always (resub includes the message) |
| Gift subs | one slip per gifter, recipients listed one per line |
| Bits | 500 or more |
| Tips (StreamElements) | at least the minimum for that currency |
| Raids | 5 viewers or more, always on their own slip; follows in the next 2 minutes go on one list, and any from minute 2 to 5 on a second |
| Follows | off by default; one slip per person, floods bundled into one list |
| Hype train | everything above is held back and printed as one receipt when the train ends |
| YouTube (off by default) | members like subs, gifted memberships with recipients, Super Chats and Super Stickers with the tip minimums, new subscribers (own switch, on) |

## Settings page

- **Output:** printer, Pushover, console, or several at once.
- **Printer:** *Network* - type the IP or press **Scan network**, then **Test** each result
  to see which one is your receipt printer, and **Use** it. *USB* - pick the printer from
  the list of printers installed in Windows (install its Windows driver first).
  Office printers also show up in the network scan; a test slip sent to one of those can
  print a page of gibberish.
- **Twitch:** enter the Client ID (app registered on dev.twitch.tv, redirect URL
  `http://localhost`, Client Type **Public**) and press **Log in with Twitch**.
- **Profile pictures:** the viewer's Twitch picture on their slip (black and white on the
  printer, colour in Pushover). Size, which slips, and lightening of very dark pictures.
  Pictures are cached in the `avatar_cache` folder; it's safe to delete.
- **Emotes:** Twitch emotes in resub and bits messages print as small pictures
  (Pushover and the console show the emote names). Cached in `emote_cache`.
- **Follows:** switch on under *What prints*. Needs the Twitch login done once more,
  because follows need an extra permission.
- **StreamElements:** your JWT token (your Twitch channel's, for tips).
- **YouTube:** link your YouTube channel in StreamElements, switch to that channel in the
  dashboard and copy *its* JWT token into Settings > YouTube. Only public livestreams send
  events. StreamElements doesn't document every YouTube field, so the first 25 YouTube
  events are saved to `youtube_events.jsonl`; if something prints oddly, that file shows why.
- **SimBrief:** username and which download to print; **Load from my last OFP** lists them.
- **What prints / Texts:** thresholds, tip minimums per currency, footer lines.

Keys and tokens are never shown again once saved; leave the field empty to keep them.
Settings are stored in `config.toml`, which you can still edit by hand.

## Control panel

**http://localhost:8770** - pause stream printing, print your OFP,
test slip, reprint last, connection status and a live log.

- **Pause:** everything that comes in (subs, gifts, bits, tips, raids, follows, YouTube) is
  collected, like a hype train. When you come back: **one receipt** ("While you were away",
  with raids and follows listed), **one by one** (every slip as normal), or **don't print**.
- **Stream log** (top right, or **http://localhost:8770/stream-log**): every Twitch and YouTube
  event with its slip, including the ones below your minimums and follows that were switched
  off. Filter by type, Twitch or YouTube, or "Not printed", and print anything again. Keeps the
  last 2000 in `stream_log.jsonl`, with the pictures in `stream_log_images`.
- **Summary:** in the stream log press **Select for a summary**, tick events (shift-click
  picks everything in between, or **Select all shown** with a filter on) and **Print summary**
  for one receipt adding them all up.
- **While Print Desk was off:** at startup Print Desk asks StreamElements what came in while
  it wasn't running (up to 14 days back, Twitch and YouTube) and adds it to the stream log as
  "while Print Desk was off". Nothing prints by itself; the *While off* filter shows them, so
  you can select them and print a summary. **Fetch missed events** does the same by hand.

- **OBS dock:** *Docks > Custom Browser Docks*, URL `http://localhost:8770`.
- **Phone:** the black window shows the address to use on your home network.
  Set *Panel > Reachable from* to "This PC only" to switch that off.

## Flights: datalink and arrival weather

Press **Start flight from SimBrief** in the panel (printing the OFP does this too).
Print Desk takes the ATC callsign and airports from your latest OFP and, until you press
**End flight** (or after 10 hours), it:

- **Reads your Hoppie messages** (read-only, with your logon code from Settings > Datalink)
  and puts every one in the **datalink log**. Nothing prints by itself: open the log from the
  panel (**Open datalink log**, or **http://localhost:8770/log** on a second screen or your
  phone), pick a message and press **Print** (or the P key). The log keeps the last 1000
  entries across restarts in `acars_log.jsonl`. Want automatic printing back? Settings >
  Datalink > *When something arrives* > *Print it automatically*.
- **Watches the departure station** (your departure ICAO; change it in the panel if the
  vACC uses another name). If your aircraft is on Hoppie and the station isn't, it adds
  `DATALINK N/A - REVERT TO VOICE` to the log once.
- **Times your clearance request:** after sending the PDC request in the aircraft, press
  **Clearance requested**. Orange after 1 minute without a reply, `NO REPLY` in the log after
  3 minutes (both in Settings). A reply from the station turns it green.
- **Fetches departure weather** into the log when you connect to VATSIM at the departure airport
  (or when the flight starts, or never - see Settings > Weather): the departure ATIS
  (`_D_ATIS`, else the main ATIS), or the METAR if no ATIS is online.
- **Fetches arrival weather** into the log once you're within 150 NM of your destination: the arrival
  ATIS (`_A_ATIS`, else the main ATIS), or the METAR. Needs you connected to VATSIM.
  The *Departure now* and *Arrival now* buttons in the panel always print straight away.

Print Desk never sends anything on Hoppie and checks only every 45-75 seconds
(briefly every ~25 seconds while waiting for a reply), as Hoppie asks.

## Auto-pause in busy flight phases

The switch under the main buttons. During a flight (and while you're on VATSIM) stream
printing pauses by itself:

- **Taxi out** (moving faster than 5 knots) until you climb through **15,000 ft**: then one
  "While you were flying" receipt.
- **Below 14,500 ft** on the way down until you've **stood still for 2 minutes** after landing:
  another receipt.
- **Pushback** is too slow to see reliably, so press Pause yourself; at taxi the automatic
  pause takes it over and ends it at 15,000 ft.
- **Resume by hand** whenever you like: it won't pause again until the next phase. A pause
  you start yourself in cruise is left alone. **End flight** ends an automatic pause.
- Altitude, taxi speed and the parked time are in Settings.

## Commands

From a command prompt in the Print Desk folder (`printdesk` runs `printdesk.bat`,
or type `py printdesk.py` instead of `printdesk`):

```
printdesk test all        fake stream events, nobody pays
printdesk test hype       just a hype train
printdesk test follow     follows, including a follow-bot flood
printdesk test youtube    members, gifted memberships, Super Chats, a new subscriber
printdesk picture yourname friend_name       real Twitch profile pictures on a test slip
printdesk picture "C:\Photos\me.jpg"            any picture file on a test slip
printdesk ofp             print your latest SimBrief OFP (same as print_ofp.bat)
printdesk ofp --list      show the download names in your latest OFP
printdesk login           Twitch login without the panel
```

Keep `config.toml` and `twitch_tokens.json` to yourself: they give access to your accounts.
