# Japan visa slot monitor

Watches the Uzbekistan–Japan embassy booking calendar and wakes you when a slot
opens with enough seats for your group. It never books anything and never submits
any personal data. It only reads.

Site: <https://uzembassyryouji.rsvsys.jp/reservations/calendar>

## What it watches

Two calendars at once, each alerting separately so you always know which one
opened. Current month plus the next two.

- **REPRESENTATIVE** — category 12 / event 21, Representative or Travel Agency
- **INDIVIDUAL** — category 12 / event 20, Applicant

Change **two lines** in `config.json` and nothing else:

```json
"watch":  ["short_stay_representative", "short_stay_applicant"],
"groups": [4, 3]
```

`watch` takes any of the keys under `targets` in the same file:

| key | what it is |
| --- | --- |
| `short_stay_representative` | short stay, Representative or Travel Agency |
| `short_stay_applicant` | short stay, Applicant (individual) |
| `coe_representative` | with COE, Representative |
| `coe_applicant` | with COE, Applicant |
| `govdocs_representative` | with Government Documents, Representative |
| `govdocs_applicant` | with Government Documents, Applicant |

`groups` is the sizes you need to seat — `[4, 3]` for one group of four and one
of three. Every alert says which of them fits.

## How capacity works on this site

A booking is **one time slot holding several applicants**, not several adjacent
slots: you pick a time, then choose how many people. The day view offers **1 to 5
applicants** in a single booking. This is read straight off the live page, and it
matches a real booking made through this site in August 2026 in which five
applicants went into a single appointment.

So a group of four and a group of three each fit in one slot. Seven people do not,
which is why two separate bookings are needed.

**The number of seats left has never been observed live on this site.** Every day
is currently booked out, and a full slot shows only a crossed-out icon with no
number at all. Two things are nevertheless known about how the count is expressed:

- The booking system's own code hides slots whose hidden `data-stock` number is
  below the number of people you selected — so a per-slot count exists.
- The identical software at another Japanese embassy prints it in the slot as
  `残N件`.

The monitor reads **both** forms, and if a slot opens showing neither it still
alerts, worded `seats not shown`. An unreadable count is never a reason to stay
quiet. Until a real opening appears, treat any seat number in an alert as the
site's claim rather than something this project has validated.

> Anything in `tools/fixtures/*_open_*.html` is invented test data. The "3 seats"
> in those files is a number typed into `tools/make_fixtures.py`, not a reading
> from the embassy.

The month grid never shows seat counts at all. That is why every day that looks
open is opened up and read properly before you are told anything.

You are woken only when a slot could hold your smaller group. A slot too small for
either group is logged and appears in the daily summary instead.

## How you are told

A slot alert is three lines and nothing else, because it gets read half-awake on a
lock screen:

```
REPRESENTATIVE SLOTS AVAILABLE
2026-08-27 - 09:30 4 seats
https://uzembassyryouji.rsvsys.jp/reservations/calendar
```

with a **STOP ALARM** button underneath. The first word is always the calendar, so
Representative and Individual can never be confused. Tapping the ntfy notification
opens the calendar directly.

- **Slot found** → alarm on **Telegram and ntfy every 10 seconds**, about 90 times,
  until you press **STOP ALARM** in Telegram. It stops by itself after 15 minutes.
  The interval is `alarm_repeat_seconds` in `config.json`.
- **Each buzz replaces the last one.** The new message is sent first, then the
  previous one is deleted, so ninety buzzes leave exactly one message in the chat
  and the alarm is never briefly absent. If a delete fails it is ignored — a stray
  message is harmless and cannot stop the alarm. The final message is kept, so
  there is always something to tap.
- **Everything else** → Telegram only, never the alarm channel: warnings, crashes,
  wrong-calendar alerts, and the daily summary at 21:00 Tashkent. These are never
  deleted.

A real ringing phone call is not possible here: every service that places one
charges money, and this project is free with no card on file anywhere. A push
notification every 10 seconds is the loudest a free stack gets. To make those
actually wake you:

- **iPhone** — Settings → Notifications → **ntfy**: turn on Allow Notifications,
  Time Sensitive, and set the banner to Persistent. Do the same for **Telegram**.
- **Focus / silent mode** — add ntfy and Telegram to the Focus's *Allowed Apps*,
  otherwise the phone honours Do Not Disturb and stays quiet.
- **Telegram** — open the bot chat → notification settings → pick a loud custom
  sound and make sure the chat is not muted.

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
| site served the wrong calendar | immediate Telegram alert naming the calendar, and it refuses to say "no slots" for it |
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

**Changing `config.json` does not take effect immediately.** A run picks up the
code it started with and then watches for five hours, so an edit waits for the
current run to finish. To apply it now, cancel the running job — the queued run
starts within seconds with the new settings:

```bash
gh run cancel $(gh run list --workflow=monitor.yml --status in_progress --limit 1 --json databaseId --jq '.[0].databaseId') && gh workflow run monitor.yml
```

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
| `config.json` | which calendars to watch, the group sizes, the timings |
| `rsvsys.py` | talking to the site, and the two rules about how it lies |
| `notify.py` | Telegram and ntfy, with delivery actually verified |
| `monitor.py` | the loop, the alarm decisions, the daily summary |
