# Japan visa slot monitor

Watches the Uzbekistan–Japan embassy booking calendar and wakes you when a slot
opens with enough seats for your group. It never books anything and never submits
any personal data. It only reads.

Site: <https://uzembassyryouji.rsvsys.jp/reservations/calendar>

## What it watches

By default: **category 12 / event 21 — VISA Application for short stay
(Representative or Travel Agency)**, current month plus the next two.

To point it somewhere else, change **one line** in `config.json`:

```json
"target": "short_stay_representative"
```

Valid values are the keys listed under `targets` in the same file:

| key | what it is |
| --- | --- |
| `short_stay_representative` | short stay, Representative or Travel Agency (**the real target**) |
| `short_stay_applicant` | short stay, Applicant — for testing |
| `coe_representative` | with COE, Representative |
| `coe_applicant` | with COE, Applicant |
| `govdocs_representative` | with Government Documents, Representative |
| `govdocs_applicant` | with Government Documents, Applicant |

Nothing else needs editing. Commit the change and the next run picks it up.

## How capacity works on this site

Confirmed from the booking system's own code and its live pages:

- A booking is **one time slot holding several applicants**, not several adjacent
  slots. You pick a time, then choose how many people.
- The day view offers **1 to 5 applicants** in a single booking, so a group of
  three or four fits in one slot — if that slot has the seats free.
- Each slot carries a remaining-seat count. Some pages express it as a hidden
  `data-stock` number, others print it as `残N件`. The monitor reads both.
- The month grid **cannot** tell you seat counts. That is why every day that looks
  open is opened up and read properly before you are told anything.

You are alerted only when a slot could actually hold a group of at least
`min_useful_seats` (3 by default). Every alert states the date, the times, and how
many seats were seen. If a slot is open but too small, it is logged and appears in
the daily summary — it does not wake you.

## How you are told

- **Slot found** → alarm on **Telegram and ntfy**, repeating every 60 seconds
  until you press **STOP ALARM** in Telegram. It stops by itself after 15 minutes.
- **Everything else** → Telegram only, never the alarm channel: warnings, crashes,
  wrong-calendar alerts, and the daily summary at 21:00 Tashkent.

No send is trusted just because the server said "200". Telegram is confirmed by
reading back the message id and the chat it landed in; ntfy is confirmed by
re-reading the topic and finding the message. If a channel cannot be confirmed,
you are told on the channel that worked.

## Why silence can be trusted

The previous monitor could be broken for a week and look exactly like "no slots".
This one separates those two states:

| what happened | what you get |
| --- | --- |
| read succeeded, nothing open | silence, then a daily summary saying "no slots today" |
| site returned an error | retried within seconds; never blind for more than about 30 |
| no successful read for 3 minutes | Telegram warning, and again when it recovers |
| site served the wrong calendar | immediate Telegram alert, and it refuses to say "no slots" |
| day squares in a shape it does not recognise | Telegram alert rather than a guess |
| the run crashed | Telegram alert from the workflow itself |
| no run alive at all | the watchdog says so within 10 minutes and restarts it |

**The wrong-calendar guard is the important one.** Switching this site to the
Representative calendar only works if you ask for the category and event and send
**no plan id at all**. Send the Applicant's plan and the site answers 200 with a
perfectly valid Applicant calendar and no error. Every single fetch is checked
against what was asked for, and a mismatch is a hard failure, never "no slots".

## Running it

It runs itself on GitHub Actions, free, on this public repository:

- `monitor.yml` — the watcher. Each run watches for about five hours, then a fresh
  run takes over. Polls every 15s during 05:00–12:00 UTC and every 55s otherwise.
- `watchdog.yml` — every 10 minutes, checks a monitor run is actually alive and
  shouts on Telegram if not.
- `selftest.yml` — Mondays, fires a real alert end to end and reports if it fails.

Three repository secrets are required: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
`NTFY_TOPIC`.

## Checking it yourself

```bash
python3 monitor.py --once        # read the live calendar once, print what it sees
python3 tools/prove_guard.py     # prove the wrong-calendar guard against the live site
python3 monitor.py --selftest    # ring both channels for real
```

Replay saved pages through the exact detection and alerting code:

```bash
python3 tools/make_fixtures.py
python3 tools/replay.py tools/fixtures/month_one_open.html tools/fixtures/day_open_stock.html
python3 tools/replay.py tools/fixtures/month_one_open.html tools/fixtures/day_open_stock.html --alert
```

`tools/fixtures/month_all_closed.html` and `day_all_full.html` are pages captured
live from the embassy site. The `*_open_*` pages are those same captured pages with
only the slot cell swapped for the open forms this booking software produces —
every embassy on this platform was fully booked when this was built, so no page
showing a real open slot could be captured. See `tools/make_fixtures.py`. The open
cells deliberately carry misleading `alt` text, because on this site the month grid
labels closed days "Available"; availability is never read from `alt`.

## Files

| file | what it does |
| --- | --- |
| `config.json` | the target, the group size, the timings |
| `rsvsys.py` | talking to the site, and the two rules about how it lies |
| `notify.py` | Telegram and ntfy, with delivery actually verified |
| `monitor.py` | the loop, the alarm decisions, the daily summary |
