"""The monitor loop.

Design rule behind every choice in here: silence must never be ambiguous.
"No slots" is a thing this program is allowed to say only after a verified read
of the correct calendar. Anything else -- a failed read, a calendar the site
swapped on us, a crash -- goes to Telegram instead.
"""

import argparse
import json
import os
import sys
import time
import traceback

import notify
import rsvsys as visasite

TASHKENT_OFFSET = 5 * 3600


def log(msg):
    print("%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), msg), flush=True)


def load_config(path="config.json"):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    key = cfg["target"]
    if key not in cfg["targets"]:
        raise SystemExit("config target '%s' is not one of: %s"
                         % (key, ", ".join(cfg["targets"])))
    cfg["_target"] = dict(cfg["targets"][key])
    cfg["_target"]["key"] = key
    return cfg


def months_to_scan(count, now=None):
    t = time.gmtime(now if now is not None else time.time())
    y, m = t.tm_year, t.tm_mon
    out = []
    for _ in range(count):
        out.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


# --------------------------------------------------------------------------
# state that survives a runner restart
# --------------------------------------------------------------------------

class State:
    def __init__(self, path):
        self.path = path
        self.data = {"date": None, "checks": 0, "failures": 0, "wrong_calendar": 0,
                     "found": [], "summary_sent_for": None, "alerted": {}}
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except Exception:
            pass

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
        except Exception as e:
            log("could not save state: %s" % e)

    def roll_day(self, today):
        if self.data.get("date") != today:
            self.data.update({"date": today, "checks": 0, "failures": 0,
                              "wrong_calendar": 0, "found": [], "alerted": {}})
            self.save()


# --------------------------------------------------------------------------
# one pass over the calendar
# --------------------------------------------------------------------------

class WrongCalendarAlert(Exception):
    pass


def scan_once(cfg, s, notifier):
    """Return (openings, days_seen). Raises on a verified-wrong calendar."""
    tgt = cfg["_target"]
    s.select(tgt["category"], tgt["event"], tgt.get("plan"))

    seen = 0
    open_dates = []
    unknown = []
    for (y, m) in months_to_scan(cfg["months_ahead"] + 1):
        html = s.month(y, m)
        for d in visasite.parse_month(html):
            seen += 1
            if d.state == visasite.Day.OPEN and d.date not in open_dates:
                open_dates.append(d.date)
            elif d.state == visasite.Day.UNKNOWN:
                unknown.append("%s: %s" % (d.date or "%04d-%02d" % (y, m), d.note))

    if unknown:
        notifier.notice(
            "Calendar markup changed - the monitor saw day squares it does not "
            "recognise and cannot judge:\n" + "\n".join(unknown[:8]) +
            "\n\nStill running, but treat 'no slots' with suspicion until this is fixed.")

    openings = []
    for date in open_dates:
        html = s.day(date)
        slots = visasite.parse_day(html)
        cap = visasite.max_group_size(html)
        free = [sl for sl in slots if sl.available]
        if free:
            openings.append({"date": date, "slots": free, "max_group": cap})
    return openings, seen, open_dates


def useful(opening, min_seats):
    """A day is worth waking someone for only if a slot could hold the group."""
    for sl in opening["slots"]:
        if sl.seats is None or sl.seats >= min_seats:
            return True
    return False


def opening_key(opening):
    return opening["date"] + "|" + ",".join(
        "%s:%s" % (sl.time, "?" if sl.seats is None else sl.seats) for sl in opening["slots"])


def describe(opening, cfg):
    tgt = cfg["_target"]
    lines = ["SLOT OPEN - %s" % tgt["label"], "", "Date: %s" % opening["date"], "Times:"]
    for sl in opening["slots"]:
        lines.append("  - " + sl.describe())
    best = [sl.seats for sl in opening["slots"] if sl.seats is not None]
    if best:
        top = max(best)
        lines.append("")
        lines.append("Most seats in a single slot: %d" % top)
        lines.append("Your group of %d %s" % (
            cfg["group_size"],
            "FITS in one booking." if top >= cfg["group_size"]
            else "does NOT fit one slot - largest is %d." % top))
    else:
        lines.append("")
        lines.append("The site did not state seat counts. Check manually - "
                     "the monitor is not guessing.")
    if opening.get("max_group"):
        lines.append("Site allows up to %d applicants per booking." % opening["max_group"])
    lines.append("")
    lines.append("https://uzembassyryouji.rsvsys.jp/reservations/calendar")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------

def run(cfg, notifier, state, deadline):
    s = visasite.Site()
    last_success = time.time()
    stale_warned = False
    busy_lo, busy_hi = cfg["busy_hours_utc"]

    while time.time() < deadline:
        now = time.time()
        today = time.strftime("%Y-%m-%d", time.gmtime(now + TASHKENT_OFFSET))
        state.roll_day(today)

        hour = time.gmtime(now).tm_hour
        interval = cfg["poll_seconds_busy"] if busy_lo <= hour < busy_hi else cfg["poll_seconds_quiet"]

        try:
            openings, seen, open_dates = scan_once(cfg, s, notifier)
            state.data["checks"] += 1
            last_success = time.time()
            if stale_warned:
                notifier.notice("Reading the calendar again normally.")
                stale_warned = False

            log("ok: %d squares, open days %s" % (seen, open_dates or "none"))

            for op in openings:
                key = opening_key(op)
                if not useful(op, cfg["min_useful_seats"]):
                    log("open but too small for the group: %s" % key)
                    continue
                if state.data["alerted"].get(op["date"]) == key:
                    continue
                text = describe(op, cfg)
                result = notifier.alarm("Visa slot open", text,
                                        cfg["alarm_repeat_seconds"], cfg["alarm_max_seconds"])
                state.data["alerted"][op["date"]] = key
                state.data["found"].append({
                    "date": op["date"],
                    "slots": [sl.describe() for sl in op["slots"]],
                    "alerted_at": time.strftime("%H:%M", time.gmtime(time.time() + TASHKENT_OFFSET)),
                    "telegram": result["telegram_confirmed"],
                    "ntfy": result["ntfy_confirmed"],
                })
                state.save()

        except visasite.WrongCalendar as e:
            state.data["wrong_calendar"] += 1
            state.data["failures"] += 1
            state.save()
            log("WRONG CALENDAR: %s" % e)
            notifier.notice(
                "WRONG CALENDAR - the monitor is NOT watching what you asked for.\n\n"
                "%s\n\nIt will keep retrying, and it will not report 'no slots' "
                "while this is happening." % e)
            s = visasite.Site()          # fresh session, then straight back in
            time.sleep(5)
            continue

        except visasite.FetchFailed as e:
            state.data["failures"] += 1
            log("read failed (%s) - retrying in a moment" % e)
            blind = time.time() - last_success
            if blind > cfg["stale_read_warning_seconds"] and not stale_warned:
                stale_warned = True
                notifier.notice(
                    "No successful read of the calendar for %d minutes.\n"
                    "Last error: %s\nStill trying every few seconds - this is NOT "
                    "'no slots'." % (blind / 60, e))
            if blind > 60:
                s = visasite.Site()      # session may have gone sour; rebuild it
            time.sleep(5)
            continue

        state.save()
        maybe_daily_summary(cfg, notifier, state)
        time.sleep(interval)


def maybe_daily_summary(cfg, notifier, state):
    tash = time.gmtime(time.time() + TASHKENT_OFFSET)
    today = time.strftime("%Y-%m-%d", tash)
    if tash.tm_hour < cfg["daily_summary_hour_tashkent"]:
        return
    if state.data.get("summary_sent_for") == today:
        return
    notifier.notice(daily_summary_text(cfg, state, today))
    state.data["summary_sent_for"] = today
    state.save()


def daily_summary_text(cfg, state, today):
    d = state.data
    lines = ["Daily summary - %s (Tashkent)" % today,
             "Watching: %s" % cfg["_target"]["label"],
             "",
             "Checks completed: %d" % d.get("checks", 0),
             "Failed reads: %d" % d.get("failures", 0),
             "Wrong-calendar events: %d" % d.get("wrong_calendar", 0),
             ""]
    found = d.get("found") or []
    if not found:
        lines.append("No slots today.")
    else:
        lines.append("Slots found: %d" % len(found))
        for f in found:
            lines.append("  %s at %s - %s" % (f["date"], f["alerted_at"], "; ".join(f["slots"])))
            lines.append("    alerted: telegram=%s ntfy=%s"
                         % ("yes" if f["telegram"] else "NO", "yes" if f["ntfy"] else "NO"))
    return "\n".join(lines)


# --------------------------------------------------------------------------

def selftest(cfg, notifier):
    """Fire a real alert end to end and report honestly whether it worked."""
    text = ("SELF-TEST - this is not a real slot.\n\n"
            "Date: %s\nTimes:\n  - 09:30 (4 seats)\n\n"
            "If this reached both your phone and Telegram, the alarm path works.\n"
            "Press STOP ALARM to end it." % time.strftime("%Y-%m-%d"))
    result = notifier.alarm("Visa monitor self-test", text, repeat_seconds=60, max_seconds=180)
    ok = result["telegram_confirmed"] and (notifier.ntfy is None or result["ntfy_confirmed"])
    notifier.notice(
        "Self-test %s.\nTelegram confirmed: %s\nntfy confirmed: %s\nRounds: %d\nStopped by: %s%s"
        % ("PASSED" if ok else "FAILED",
           result["telegram_confirmed"], result["ntfy_confirmed"],
           result["rounds"], result["stopped_by"],
           "" if not result["problems"] else "\nProblems: " + "; ".join(result["problems"][:4])))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--state", default=os.environ.get("STATE_FILE", "state.json"))
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--once", action="store_true", help="one scan, print result, no alarm")
    ap.add_argument("--minutes", type=float, default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    notifier = notify.from_env(log=log)

    if args.selftest:
        return selftest(cfg, notifier)

    if args.once:
        s = visasite.Site()
        openings, seen, open_dates = scan_once(cfg, s, notifier)
        print(json.dumps({
            "target": cfg["_target"],
            "squares_seen": seen,
            "open_days": open_dates,
            "openings": [{"date": o["date"], "slots": [x.describe() for x in o["slots"]],
                          "max_group": o["max_group"]} for o in openings],
        }, indent=2))
        return 0

    state = State(args.state)
    minutes = args.minutes if args.minutes is not None else cfg["run_minutes"]
    deadline = time.time() + minutes * 60
    log("monitor starting: %s, %d months, poll %ss/%ss"
        % (cfg["_target"]["label"], cfg["months_ahead"] + 1,
           cfg["poll_seconds_busy"], cfg["poll_seconds_quiet"]))
    try:
        run(cfg, notifier, state, deadline)
    except KeyboardInterrupt:
        return 0
    except Exception:
        tb = traceback.format_exc()
        log(tb)
        notifier.notice("MONITOR CRASHED - it has stopped watching.\n\n%s"
                        % tb.strip().splitlines()[-1][:400])
        return 1
    log("run window finished cleanly; a fresh run takes over")
    return 0


if __name__ == "__main__":
    sys.exit(main())
