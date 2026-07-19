# Japan Visa Slot Monitor (Embassy of Japan in Uzbekistan)

Watches https://uzembassyryouji.rsvsys.jp/reservations/calendar for open
short-stay visa appointment days and alerts your phone within seconds via a
Telegram message **and** a ringing Telegram call. Notify-only — it never books
anything and never touches your personal data.

## Stack, and why it satisfies the two hard constraints

**One ~300-line stdlib-only Python script, running as a persistent `launchd`
service on your own Mac.** No browser, no dependencies, no cloud.

**Low latency.** Reverse-engineering the page showed the calendar is loaded by
a plain form-encoded POST to the site's own AJAX endpoint
(`/ajax/reservations/calendar`) which returns the month grid as JSON-wrapped
HTML. So each check is just 2 tiny HTTP POSTs (~30 KB, one per month) — a full
check-detect-notify cycle measures ~3–8 s. The loop polls every **35–75 s
(randomized)**, so worst-case detection lag is ~80 s and the median is ~40 s.
Running locally beats any free scheduler by an order of magnitude: GitHub
Actions cron has a 5-min minimum and frequently fires 10–20 min late.

**Low block-risk.** One long-lived cookie session (bootstrapped once, reused
for every check) hitting the same lightweight endpoint the site's own
JavaScript calls, with a realistic Chrome User-Agent, matching
Referer/X-Requested-With headers, randomized jitter between checks, a 1–3 s
human-like gap between the two month requests, and automatic exponential
backoff (2 min → 30 min) on any HTTP error, block page, or non-JSON response.
No headless-browser fingerprint to detect, and total traffic is far below what
one person clicking around generates per pageview (a single real page load
pulls dozens of assets; a check here is 2 requests).

## Critical detection finding (verified live 2026-07-19)

The spec "detect OPEN via the img alt text `Qabul qilinmoqda`" is **wrong on
the live site — the month grid's alt texts are swapped**:

| Day state (truth) | Icon | Clickable | alt text (misleading!) |
|---|---|---|---|
| Bookable | ⭕ `icon_circle.svg` | yes (`js_change_date`, `data-date`) | " Not available / Qabul tugadi / Приём окончен" |
| Closed | ❌ `icon_disabled.svg` | no | " Available / Qabul qilinmoqda / Приём ведётся" |

Proof: on 2026-07-19 the COE category showed circle days (July 22/23/24/28)
whose **day view contained a real bookable 14:30 slot**, while their month-grid
alt said "Not available". Meanwhile the short-stay category showed X-icon,
non-clickable days whose alt said "Available". Alt-based detection would have
produced 5 false alarms and missed 4 real openings on day one.

So the detector ignores alt text entirely: **OPEN = `icon_circle` in a
clickable (`js_change_date`) day cell.** (In the *day* view the alts are
correct, but the monitor only needs the month grid.)

## Setup (one-time, ~5 minutes)

1. **Telegram bot message** (instant push):
   - Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
   - Message [@userinfobot](https://t.me/userinfobot) → copy your numeric id.
   - Send your new bot any message once (bots can't message you first).
2. **Ringing call** via CallMeBot (free, no signup):
   - Make sure you have a Telegram @username (Settings → Username).
   - Open [t.me/CallMeBot_txtbot](https://t.me/CallMeBot_txtbot) and press
     **Start** once — that authorizes it to call you.
3. Credentials go in GitHub Actions secrets (cloud) or a local `.env` copied
   from `.env.example` (never committed).

## How to test

```sh
# 1. Detection unit tests (real captured fixtures, incl. the swapped-alt trap)
python3 test_detector.py
# expect three PASS lines

# 2. Live smoke test — hits the real site once, category 12 → first event/plan,
#    prints every day's status for this month + next, saves artifacts/
python3 monitor.py --once
# expect "open dates: none" (currently) and a per-day list; open
# artifacts/calendar_2026-XX.html in a browser to eyeball the parsed grid

# 3. Forced-positive end-to-end — injects a fake open date and sends REAL alerts
SELF_TEST=1 python3 monitor.py
# expect: your phone RINGS (Telegram call) + a Telegram message with dates+link,
# and a printed total latency figure (typically < 10 s)

# 3b. Full fire drill, no computer needed: repo Actions tab -> "drill" ->
#     Run workflow. For up to 5 min it behaves exactly like a real opening:
#     ring + [TEST] message every ~minute until you press the STOP button
#     in the Telegram message (expect a "✅ Alerts stopped" confirmation).

# 4. Cadence check while running: every check logs a timestamp + duration —
#    see the live job log in the repo's Actions tab (cloud) or
#    tail -f ~/Library/Logs/visa-monitor.log (local launchd)
```

## Run it permanently — in the cloud, no devices needed

It runs 24/7 on **GitHub Actions** ([.github/workflows/monitor.yml](.github/workflows/monitor.yml)):
each job runs the monitor continuously for 5h40m (polling every 35–75 s inside
the job, so the persistent-session + jitter design is preserved), then queues
its own successor; a every-30-min scheduled watchdog restarts the chain if a
job ever dies. Credentials live in repo **Settings → Secrets and variables →
Actions** (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `CALLMEBOT_TG_USER`).
Start/stop it from the repo's **Actions → monitor** page (Run workflow /
Disable workflow). The **Actions tab is your health dashboard** — a green
running job means you're covered, and its live log shows every timestamped
check.

Actions minutes are **unlimited free on public repos** (the repo holds no
secrets, so public is safe); a private repo's 2,000 free min/month would burn
out in under two days.

Local alternative (lower block-risk, residential IP): fill `.env`, then
`cp com.khassanboi.visamonitor.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.khassanboi.visamonitor.plist`
(Mac must stay awake).

## Honest limitations

- **GitHub Actions caveats:** runners use datacenter (Azure) IPs, which a
  government site could treat less kindly than a home connection — the
  backoff handles throttling, but a hard datacenter-IP ban would require
  switching to running it locally/on a VPS. There's a ~10–30 s coverage gap
  at each 5h40m job handoff. `state.json` doesn't persist across jobs, so if
  a slot stays open you'll be re-alerted when a new job starts (harmless —
  arguably a feature). GitHub disables the *scheduled* watchdog after 60 days
  with no repo commits (the self-chaining continues regardless); if alerts
  ever stop, open the Actions tab and press Run workflow.
- **Detection lag is bounded by the poll interval** (35–75 s). Tightening it
  raises block-risk; ~2 checks/min from one residential IP is a defensible
  human-ish rate, ~2.5k requests/day. If you see backoff warnings in the log,
  raise the interval.
- **Backoff trade-off:** after repeated errors the monitor deliberately slows
  to up to 30 min between tries. That's the design — staying unbanned beats
  hammering — but it means an opening during a backoff window can be missed.
- **Alarm behavior:** when an opening appears you're rung + messaged every
  ~65 s (`ALARM_REPEAT_SECONDS`) until you press the 🛑 STOP button in the
  Telegram message (or reply "stop" to the bot). After stopping, only a NEW
  date (or a close-and-reopen) restarts the alarm. Pressing STOP can take up
  to one check cycle (~1 min) to register — you may get one extra ring.
  Acknowledgements don't survive the ~5.7 h job handoff, so a still-open slot
  re-alerts once per handoff.
- **Site changes:** if rsvsys changes markup or fixes their swapped alt texts,
  detection still works (icons/clickability, not text), but a structural
  redesign would need a parser tweak. If the monitor starts logging repeated
  errors, re-run `python3 monitor.py --once` to diagnose.
- **CallMeBot** is a free third-party service with no SLA; that's why the
  Telegram bot message (Telegram's own API, very reliable) is always sent too.
  Set your Telegram notification sound for the bot chat to something loud.
