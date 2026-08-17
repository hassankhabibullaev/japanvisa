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
    watch = cfg.get("watch") or []
    if not watch:
        raise SystemExit("config 'watch' is empty - nothing would be monitored")
    targets = []
    for key in watch:
        if key not in cfg["targets"]:
            raise SystemExit("config watch entry '%s' is not one of: %s"
                             % (key, ", ".join(cfg["targets"])))
        t = dict(cfg["targets"][key])
        t["key"] = key
        targets.append(t)
    cfg["_targets"] = targets
    if not cfg.get("groups"):
        raise SystemExit("config 'groups' is empty - no group size to look for")
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


def scan_once(cfg, s, notifier, tgt):
    """Return (openings, days_seen). Raises on a verified-wrong calendar."""
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
        notifier.warn(
            "Calendar markup changed on the %s calendar - the monitor saw day "
            "squares it does not recognise and cannot judge:\n" % tgt["label"] +
            "\n".join(unknown[:8]) +
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


def useful(opening, groups):
    """Worth waking someone only if a slot could hold the smallest group.

    A slot whose seat count the site did not state also counts: a slot we cannot
    measure is never silently discarded.
    """
    need = min(groups)
    for sl in opening["slots"]:
        if sl.seats is None or sl.seats >= need:
            return True
    return False


def opening_key(opening):
    return opening["date"] + "|" + ",".join(
        "%s:%s" % (sl.time, "?" if sl.seats is None else sl.seats) for sl in opening["slots"])


def pretty_date(iso):
    """2026-08-27 -> 'Thu, 27 August 2026'. Falls back to the raw value."""
    try:
        t = time.strptime(iso, "%Y-%m-%d")
    except ValueError:
        return iso
    return time.strftime("%a, %d %B %Y", t)


def describe(openings, cfg, tgt):
    """The dates that opened, and the link. Nothing else.

    An alarm is read half-awake on a lock screen, so times and seat counts are
    deliberately left out -- they are on the site, one tap away.
    """
    dates = "\n".join(pretty_date(o["date"]) for o in openings)
    return "%s\n\n%s" % (dates, visasite.CALENDAR_PAGE)


# --------------------------------------------------------------------------
# changing what is watched, from Telegram
# --------------------------------------------------------------------------
#
# The choice lives in the state file, so it survives the five-hourly restart and
# does not need a commit. config.json is the fallback when nothing was chosen.

PRESETS = [
    ("Short stay - both", ["short_stay_representative", "short_stay_applicant"]),
    ("Short stay - representative", ["short_stay_representative"]),
    ("Short stay - individual", ["short_stay_applicant"]),
    ("COE - both", ["coe_representative", "coe_applicant"]),
    ("COE - representative", ["coe_representative"]),
    ("COE - individual", ["coe_applicant"]),
    ("Gov documents - both", ["govdocs_representative", "govdocs_applicant"]),
    ("Everything (all six)", ["short_stay_representative", "short_stay_applicant",
                              "coe_representative", "coe_applicant",
                              "govdocs_representative", "govdocs_applicant"]),
]


def menu_buttons():
    return [[{"text": label, "callback_data": "set:%d" % i}]
            for i, (label, _) in enumerate(PRESETS)]


def active_targets(cfg, state):
    """What to watch right now: the Telegram choice, else config.json."""
    chosen = state.data.get("watch_override")
    keys = chosen or [t["key"] for t in cfg["_targets"]]
    out = []
    for key in keys:
        if key in cfg["targets"]:
            t = dict(cfg["targets"][key])
            t["key"] = key
            out.append(t)
    return out or list(cfg["_targets"])


def watching_line(targets):
    return "Watching now:\n" + "\n".join("  - " + t["label"] for t in targets)


def handle_commands(cfg, notifier, state):
    """Act on anything typed or tapped in Telegram. Returns True if it changed."""
    changed = False
    for kind, value in notifier.tg.poll_replies():
        value = (value or "").strip().lower().lstrip("/")

        if value.startswith("set:"):
            try:
                label, keys = PRESETS[int(value.split(":", 1)[1])]
            except (ValueError, IndexError):
                continue
            state.data["watch_override"] = keys
            state.data["alerted"] = {}        # a new calendar starts fresh
            state.save()
            changed = True
            notifier.status("Switched to: %s\n\n%s"
                            % (label, watching_line(active_targets(cfg, state))))

        elif value in ("watch", "switch", "menu", "change", "start", "help"):
            notifier.menu(
                "%s\n\nTap a line to change it. Send 'status' any time to see what "
                "it is doing." % watching_line(active_targets(cfg, state)),
                menu_buttons())

        elif value == "status":
            d = state.data
            notifier.status(
                "%s\n\nToday so far:\n  checks %d\n  failed reads %d\n"
                "  wrong-calendar events %d\n  slots found %d"
                % (watching_line(active_targets(cfg, state)), d.get("checks", 0),
                   d.get("failures", 0), d.get("wrong_calendar", 0),
                   len(d.get("found") or [])))

    return changed


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------

def run(cfg, notifier, state, deadline):
    targets = active_targets(cfg, state)
    # One session per calendar, so switching between them can never leak state.
    sessions = {t["key"]: visasite.Site() for t in targets}
    last_success = time.time()
    stale_warned = False
    busy_lo, busy_hi = cfg["busy_hours_utc"]

    while time.time() < deadline:
        now = time.time()
        today = time.strftime("%Y-%m-%d", time.gmtime(now + TASHKENT_OFFSET))
        state.roll_day(today)

        if handle_commands(cfg, notifier, state):
            targets = active_targets(cfg, state)
            sessions = {t["key"]: visasite.Site() for t in targets}
            log("retargeted: %s" % ", ".join(t["key"] for t in targets))

        hour = time.gmtime(now).tm_hour
        interval = cfg["poll_seconds_busy"] if busy_lo <= hour < busy_hi else cfg["poll_seconds_quiet"]
        trouble = False

        for tgt in targets:
            key = tgt["key"]
            try:
                openings, seen, open_dates = scan_once(cfg, sessions[key], notifier, tgt)
                state.data["checks"] += 1
                last_success = time.time()
                log("ok [%s]: %d squares, open days %s"
                    % (key, seen, open_dates or "none"))
                alert_openings(cfg, notifier, state, tgt, openings)

            except visasite.WrongCalendar as e:
                trouble = True
                state.data["wrong_calendar"] += 1
                state.data["failures"] += 1
                state.save()
                log("WRONG CALENDAR [%s]: %s" % (key, e))
                notifier.error(
                    "The site served the WRONG CALENDAR - this is not what you asked "
                    "for.\n\n"
                    "Calendar: %s\n%s\n\nIt will keep retrying, and it will not report "
                    "'no slots' for this calendar while this is happening."
                    % (tgt["label"], e))
                sessions[key] = visasite.Site()      # fresh session, straight back in

            except visasite.FetchFailed as e:
                trouble = True
                state.data["failures"] += 1
                log("read failed [%s] (%s) - retrying in a moment" % (key, e))
                blind = time.time() - last_success
                if blind > cfg["stale_read_warning_seconds"] and not stale_warned:
                    stale_warned = True
                    notifier.warn(
                        "No successful read of any calendar for %d minutes.\n"
                        "Last error: %s\nStill trying every few seconds - this is NOT "
                        "'no slots'." % (blind / 60, e))
                if blind > 60:
                    sessions[key] = visasite.Site()  # session may have gone sour

        if stale_warned and not trouble:
            notifier.status("Reading the calendars again normally.")
            stale_warned = False

        state.save()
        maybe_daily_summary(cfg, notifier, state)
        time.sleep(5 if trouble else interval)


def alert_openings(cfg, notifier, state, tgt, openings):
    """One alarm per calendar, listing every date that opened.

    Grouped deliberately: three open days used to mean three alarms queued back to
    back, three quarters of an hour of ringing for one piece of news.
    """
    fresh = []
    for op in openings:
        key = opening_key(op)
        seen_key = "%s|%s" % (tgt["key"], op["date"])
        if not useful(op, cfg["groups"]):
            log("open but too small for either group [%s]: %s" % (tgt["key"], key))
            continue
        if state.data["alerted"].get(seen_key) == key:
            continue
        fresh.append((seen_key, key, op))

    if not fresh:
        return

    text = describe([op for _, _, op in fresh], cfg, tgt)
    result = notifier.alarm("%s slots available" % tgt["headline"], text,
                            cfg["alarm_repeat_seconds"], cfg["alarm_max_seconds"],
                            click=visasite.CALENDAR_PAGE)
    for seen_key, key, op in fresh:
        state.data["alerted"][seen_key] = key
        state.data["found"].append({
            "calendar": tgt["headline"],
            "date": op["date"],
            "slots": [sl.describe() for sl in op["slots"]],
            "alerted_at": time.strftime("%H:%M", time.gmtime(time.time() + TASHKENT_OFFSET)),
            "telegram": result["telegram_confirmed"],
            "ntfy": result["ntfy_confirmed"],
        })
    state.save()


def maybe_daily_summary(cfg, notifier, state):
    tash = time.gmtime(time.time() + TASHKENT_OFFSET)
    today = time.strftime("%Y-%m-%d", tash)
    if tash.tm_hour < cfg["daily_summary_hour_tashkent"]:
        return
    if state.data.get("summary_sent_for") == today:
        return
    notifier.summary(daily_summary_text(cfg, state, today))
    state.data["summary_sent_for"] = today
    state.save()


def daily_summary_text(cfg, state, today):
    d = state.data
    lines = ["%s (Tashkent)" % pretty_date(today),
             "",
             watching_line(active_targets(cfg, state)),
             "",
             "Checks completed: %d" % d.get("checks", 0),
             "Failed reads: %d" % d.get("failures", 0),
             "Wrong-calendar events: %d" % d.get("wrong_calendar", 0),
             ""]
    found = d.get("found") or []
    if not found:
        lines.append("No slots today on any watched calendar.")
    else:
        lines.append("Slots found: %d" % len(found))
        for f in found:
            lines.append("  %s - %s (alerted %s)"
                         % (f.get("calendar", "?"), pretty_date(f["date"]), f["alerted_at"]))
            if not (f["telegram"] and f["ntfy"]):
                lines.append("    delivery: telegram=%s ntfy=%s"
                             % ("yes" if f["telegram"] else "NO",
                                "yes" if f["ntfy"] else "NO"))
    return "\n".join(lines)


# --------------------------------------------------------------------------

def retrying(work, attempts, pause):
    """Run work(), tolerating this site's routine 500s. WrongCalendar never retries."""
    for i in range(attempts):
        try:
            return work()
        except visasite.FetchFailed as e:
            if i == attempts - 1:
                raise
            log("attempt %d/%d failed (%s), trying again" % (i + 1, attempts, e))
            time.sleep(pause)


def selftest(cfg, notifier):
    """Fire a real alert end to end and report honestly whether it worked."""
    text = ("This is a drill, not a real slot.\n\n%s\n\n%s"
            % (pretty_date(time.strftime("%Y-%m-%d")), visasite.CALENDAR_PAGE))
    result = notifier.alarm("Self-test (drill)", text,
                            repeat_seconds=cfg["alarm_repeat_seconds"], max_seconds=120)
    ok = result["telegram_confirmed"] and (notifier.ntfy is None or result["ntfy_confirmed"])
    notifier.test(
        "Weekly self-test %s.\n\nTelegram delivered: %s\nntfy delivered: %s\n"
        "Buzzes sent: %d\nEnded by: %s%s"
        % ("PASSED" if ok else "FAILED",
           "yes" if result["telegram_confirmed"] else "NO",
           "yes" if result["ntfy_confirmed"] else "NO",
           result["rounds"], result["stopped_by"],
           "" if not result["problems"] else "\n\nProblems: " + "; ".join(result["problems"][:4])))
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
        report = []
        for tgt in active_targets(cfg, State(args.state)):
            # A one-shot check gets one chance, and this site throws a 500 at
            # roughly one request in thirteen. Keep trying for a minute before
            # calling it a real failure; the live loop retries forever anyway.
            openings, seen, open_dates = retrying(
                lambda: scan_once(cfg, visasite.Site(), notifier, tgt), attempts=8, pause=8)
            report.append({
                "calendar": tgt["headline"],
                "label": tgt["label"],
                "category": tgt["category"], "event": tgt["event"],
                "squares_seen": seen,
                "open_days": open_dates,
                "openings": [{"date": o["date"], "slots": [x.describe() for x in o["slots"]],
                              "max_group": o["max_group"]} for o in openings],
            })
        print(json.dumps(report, indent=2))
        return 0

    state = State(args.state)
    log("discarded %d stale Telegram updates" % notifier.tg.drain())
    notifier.tg.register_commands([
        ("watch", "Change which calendars are watched"),
        ("status", "What it is watching and today's counts"),
        ("stop", "Stop a ringing alarm"),
    ])
    minutes = args.minutes if args.minutes is not None else cfg["run_minutes"]
    deadline = time.time() + minutes * 60
    log("monitor starting: watching %s | groups %s | %d months | poll %ss/%ss"
        % (", ".join(t["headline"] for t in active_targets(cfg, state)),
           cfg["groups"], cfg["months_ahead"] + 1,
           cfg["poll_seconds_busy"], cfg["poll_seconds_quiet"]))
    try:
        run(cfg, notifier, state, deadline)
    except KeyboardInterrupt:
        return 0
    except Exception:
        tb = traceback.format_exc()
        log(tb)
        notifier.crash("The monitor hit an error and has stopped watching.\n"
                       "A fresh run should take over within 15 minutes.\n\n%s"
                       % tb.strip().splitlines()[-1][:400])
        return 1
    log("run window finished cleanly; a fresh run takes over")
    return 0


if __name__ == "__main__":
    sys.exit(main())
