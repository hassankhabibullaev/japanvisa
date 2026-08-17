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

Change **one line** in `config.json` and nothing else:

```json
"watch": ["short_stay_representative", "short_stay_applicant"]
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

Seat counts were confirmed live on 17 August on an open COE day: the slot cell
carries `data-stock="15"`, and other days showed 20. So the count is real, and a
day typically opens with far more seats than one group needs.

**Alerts no longer mention seats, and the monitor no longer reads them.** Opening
each day to check cost several seconds per day on the one path where seconds
decide whether the slot still exists — and with a maximum of five applicants per
booking against fifteen-to-twenty seats on release, the number was never going to
change the answer. If a day is bookable, you are told immediately; the seats are
on the site.

`tools/fixtures/real_month_open_coe.html` and `real_day_open_coe.html` are genuine
captures of an open month and an open day. The other `*_open_*.html` files are
hand-made variants used to test shapes the site has not shown us.

## Speed, and why it matters

Slots on the short-stay calendar are gone in **one to two minutes** — other people
are clearly running booking bots. On 17 August a day opened and was taken before
the monitor looked twice.

What one check costs, measured against the live site:

| | before | now |
| --- | --- | --- |
| one calendar, three months | ~17s | ~5s |
| both calendars | ~35s | ~7s |
| wait between checks (release window) | 55s | 5s |
| **gap between looks** | **~90s** | **~12s** |

Three things bought that:

- **The calendar is selected once**, not re-selected on every pass. That request
  alone was six seconds per calendar per cycle.
- **Months and calendars are fetched at the same time** instead of one after
  another. The site answers in about 3.5s regardless, so waiting in sequence was
  pure loss.
- **Alerts fire straight off the month grid.** It used to open each day to read
  seat counts before alerting — several more seconds on the one path where
  seconds decide whether the slot still exists, for numbers the alert does not
  even mention.

The release window is **16:00–21:00 Tashkent**, where days have been seen to
appear — one new weekday at a time, roughly eleven days ahead. Inside it the
monitor checks every 5 seconds; outside it, once a minute, which keeps the load
off a site that already falls over on its own. `hot_hours_utc` in `config.json`
is where to change that as we learn more.

During the rush only **this month and next** are read, since a release eleven days
out cannot land anywhere else. That makes each check a third faster and a third
lighter. All three months are still swept once a minute so nothing can hide in
the one we skip.

**Are we being blocked?** No — measured, not assumed. Forty requests as fast as the
site could answer gave thirty successes, ten "server busy", and zero refusals.
Being refused (HTTP 403 or 429) is now reported separately from the site merely
failing, so if that ever starts you hear about it instead of it hiding among the
usual errors.

**An honest limit.** This is notify-only, by design — it never books. It can stop
missing the moment and tell you the instant something moves, but it cannot
out-click a bot that books automatically. If slots keep vanishing inside a minute,
winning one means being parked on the page during the window with the userscript
already run.

## How you are told

There are **four** kinds of message, and only one of them rings.

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

**Each calendar has its own alarm, and they ring independently.** If the applicant
calendar opens and the representative one opens seconds later, you get two
separate alarms with two separate STOP buttons; neither deletes the other's
messages. Crucially the alarms ring on their own threads, so **watching never
pauses while something is ringing** — previously an alarm blocked the monitor for
up to fifteen minutes, which meant the moment a slot opened was exactly the moment
it stopped looking for the next one.

**2. 👀 CALENDAR CHANGED** — Telegram only, no ringing. A day appearing or closing
without ever being catchable, which is what a release taken inside a minute looks
like afterwards. Worth knowing in seconds — there may be a leftover seat a refresh
away — but not worth being woken for something unbookable.

```
👀 CALENDAR CHANGED
────────────
REPRESENTATIVE

28 August 2026  -  opened and was taken before we saw it

https://uzembassyryouji.rsvsys.jp/reservations/calendar
```

**3. ⚠ NEEDS ATTENTION** — sent the moment something needs a human: the wrong
calendar, the site unreadable, a layout change it cannot judge, a crash, a dead
runner, or a failed alarm drill. A fault that persists repeats at most every 30
minutes so a stuck problem cannot flood the chat, and it always appears in the
day's report regardless.

**4. 📊 DAILY REPORT** — one message a day at 21:00 Tashkent, built to be scanned
rather than read. It merges the day's activity, the problems with their times, and
a live self-check:

```
📊 DAILY REPORT
────────────
17 August 2026

👁 WATCHING
   Short stay - Representative or Travel Agency
   Short stay - Applicant (individual)

🔔 SLOTS FOUND
   none

🔄 CALENDAR CHANGES
   19:01:22  28 August 2026  opened and was taken before we saw it

🔍 ACTIVITY
   2140 checks
   47 failed reads

⚠ PROBLEMS
   19:00-19:04  could not read site (HTTP 500)  x31

🧪 SELF-CHECK
   site            OK
   right calendar  OK
   phone alarm     OK
   telegram        OK
   alarm drill     OK  (17 Aug)

────────────
✅ Everything working
```

The last line is the whole report in one glance. **CALENDAR CHANGES** is the
important one over time: every state change with the time it happened, which is
how the real release schedule gets pinned down instead of guessed.

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
  run takes over. Checks every **5s** in the release window (11:00–16:00 UTC =
  **16:00–21:00 Tashkent**) and once a minute the rest of the day.
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
