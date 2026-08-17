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

`groups` is the sizes you need to seat — `[4, 3]` for one group of four and one of
three. It is used to decide what is worth waking you for, not shown in alerts.

### Changing it from Telegram

You do not need to touch the file. In the bot:

| send | what happens |
| --- | --- |
| `/watch` | shows buttons for every combination; tap one to switch |
| `/status` | today's full report, on demand |
| `/stop` | stops a ringing alarm |

The choice is remembered across restarts and takes effect on the next poll, within
about 15 seconds. It overrides `config.json` until you pick something else.
Switching to **COE** is the easy way to get a real alert, since COE opens more
often than the short-stay Representative calendar.

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
either group is logged and appears in the daily report instead.

## How you are told

There are only **three** kinds of message.

**1. 🔔 SLOT AVAILABLE** — the only one that rings. The calendar, the dates, the
link. No times, no seat counts; those are on the site, one tap away.

```
🔔 SLOT AVAILABLE
────────────
REPRESENTATIVE

27 August 2026
28 August 2026

https://uzembassyryouji.rsvsys.jp/reservations/calendar
```

Repeats on Telegram and ntfy every 10 seconds until you press **STOP ALARM**,
stopping by itself after 15 minutes. Each buzz replaces the previous one, so
ninety buzzes leave one message in the chat. Several days opening at once go in
one alarm, not one alarm each. Tapping the ntfy notification opens the calendar.

**2. ⚠ NEEDS ATTENTION** — sent the moment something needs a human: the wrong
calendar, the site unreadable, a layout change it cannot judge, a crash, a dead
runner, or a failed alarm drill. A fault that persists repeats at most every 30
minutes so a stuck problem cannot flood the chat, and it always appears in the
day's report regardless.

**3. 📊 DAILY REPORT** — one message a day at 21:00 Tashkent, built to be scanned
rather than read. It merges the day's activity, the problems with their times, and
a live self-check:

```
📊 DAILY REPORT
────────────
17 August 2026

🔔 SLOTS FOUND  1
   27 August 2026 - REPRESENTATIVE
   alerted 11:42

🔍 MONITORING
   Short stay - Representative or Travel Agency
   Short stay - Applicant (individual)
   1284 checks, 31 failed reads

⚠ PROBLEMS
   11:04-11:09  could not read site (HTTP 500) x27
   14:22  wrong calendar served (REPRESENTATIVE)

🧪 SELF-CHECK
   site reachable     OK
   correct calendar   OK
   phone alarm (ntfy) OK
   telegram           OK
   weekly alarm drill OK (17 Aug)

────────────
Everything working.
```

The last line is the whole report in one glance: *Everything working.* or
*NEEDS YOUR ATTENTION - see above.*

The self-check is real, not a checkbox. It reads the live site and confirms the
calendar served is the one asked for, then sends one **silent** ntfy message and
reads it back off the server to prove your phone channel still works. Telegram
needs no separate test — the report arriving *is* the test. Send `/status` to get
the same report on demand at any time.

Once a week a louder drill fires a genuine repeating alarm on both channels, since
that is the only thing that proves the alarm actually rings. Passing is not news:
it is recorded and shown in the next daily report. Only a failure sends a message.

No send is trusted just because the server said "200". Telegram is confirmed by
reading back the message id and the chat it landed in; ntfy is confirmed by
re-reading the topic and finding the message. If a channel cannot be confirmed,
you are told on the channel that worked.

## Why silence can be trusted

The previous monitor could be broken for a week and look exactly like "no slots".
This one separates those two states:

| what happened | what you get |
| --- | --- |
| read succeeded, nothing open | silence, then a daily report saying "SLOTS FOUND none" |
| site returned an error | retried within seconds; never blind for more than about 30 |
| no successful read for 3 minutes | ⚠ NEEDS ATTENTION, and the times land in the report |
| site served the wrong calendar | ⚠ NEEDS ATTENTION naming the calendar; it refuses to say "no slots" for it |
| day squares in a shape it does not recognise | ⚠ NEEDS ATTENTION rather than a guess |
| the run crashed | ⚠ NEEDS ATTENTION from the workflow itself |
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
- `selftest.yml` — Mondays, the loud alarm drill. Silent unless it fails.

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
