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
import threading
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
    FRESH_DAY = {"checks": 0, "failures": 0, "wrong_calendar": 0,
                 "found": [], "alerted": {}, "events": [], "transitions": []}

    def __init__(self, path):
        self.path = path
        # "seen" is the calendar's own state, not a daily tally, so it must not
        # be cleared at midnight or every day would start by re-reporting itself.
        self.data = {"date": None, "summary_sent_for": None, "watch_override": None,
                     "seen": {}}
        self.data.update(dict(self.FRESH_DAY))
        self.load()

    def note_event(self, kind, detail):
        """Remember a fault with its Tashkent time, for the day's report."""
        events = self.data.setdefault("events", [])
        stamp = time.strftime("%H:%M", time.gmtime(time.time() + TASHKENT_OFFSET))
        if events and events[-1]["kind"] == kind and events[-1]["detail"] == detail:
            events[-1]["count"] = events[-1].get("count", 1) + 1
            events[-1]["until"] = stamp
        else:
            events.append({"at": stamp, "kind": kind, "detail": detail, "count": 1})
        del events[:-20]

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
            self.data["date"] = today
            self.data.update({k: (list(v) if isinstance(v, list) else
                                  dict(v) if isinstance(v, dict) else v)
                              for k, v in self.FRESH_DAY.items()})
            self.save()


# --------------------------------------------------------------------------
# one pass over the calendar
# --------------------------------------------------------------------------

class WrongCalendarAlert(Exception):
    pass


def ensure_selected(s, tgt):
    """Select the calendar only when the session is not already on it.

    Re-selecting every cycle cost a request per calendar per cycle -- about six
    seconds of a thirty-five second cycle, spent re-answering a question the
    session had already answered.
    """
    if (s.plan is None or s.category != str(tgt["category"])
            or s.event != str(tgt["event"])):
        s.select(tgt["category"], tgt["event"], tgt.get("plan"))


def scan_target(cfg, s, tgt):
    """Read every month of one calendar at once.

    The months are fetched in parallel because they are independent and the site
    answers in about three and a half seconds each; one after another that is the
    difference between seeing a slot and reading about it afterwards.

    Returns (states, unknown) where states maps YYYY-MM-DD to open/full/none.
    Raises whatever the fetches raised, so a wrong calendar still stops the scan.
    """
    ensure_selected(s, tgt)
    months = months_to_scan(cfg["months_ahead"] + 1)

    pages, errors = {}, []
    lock = threading.Lock()

    def fetch(y, m):
        try:
            html = s.month(y, m)
            with lock:
                pages[(y, m)] = html
        except Exception as e:                       # noqa: BLE001 - re-raised below
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=fetch, args=(y, m)) for (y, m) in months]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        wrong = [e for e in errors if isinstance(e, visasite.WrongCalendar)]
        raise (wrong[0] if wrong else errors[0])

    states, unknown = {}, []
    for (y, m), html in pages.items():
        for d in visasite.parse_month(html, y, m):
            if not d.date:
                continue
            if d.state == visasite.Day.UNKNOWN:
                unknown.append("%s: %s" % (d.date, d.note))
            # A date can appear in two grids (trailing days of the next month).
            # Bookable always wins so a day is never hidden by its other copy.
            if states.get(d.date) != visasite.Day.OPEN:
                states[d.date] = d.state
    return states, unknown


def pretty_date(iso):
    """2026-08-27 -> '27 August 2026'. Falls back to the raw value."""
    try:
        t = time.strptime(iso, "%Y-%m-%d")
    except ValueError:
        return iso
    return "%d %s %d" % (t.tm_mday, time.strftime("%B", t), t.tm_year)


def describe(dates, cfg, tgt):
    """The dates that opened, and the link. Nothing else.

    An alarm is read half-awake on a lock screen, so times and seat counts are
    deliberately left out -- they are on the site, one tap away.
    """
    return "%s\n\n%s\n\n%s" % (tgt["headline"],
                               "\n".join(pretty_date(d) for d in dates),
                               visasite.CALENDAR_PAGE)


# How a day changed, in words worth reading on a phone.
TRANSITIONS = {
    ("none", "full"): "opened and was taken before we saw it",
    ("none", "open"): "OPENED",
    ("full", "open"): "seats freed up",
    ("open", "full"): "filled up",
    ("open", "none"): "withdrawn",
    ("full", "none"): "reception withdrawn",
}


def diff_states(before, after):
    """Every day whose state changed, as (date, from, to, wording)."""
    out = []
    for date in sorted(set(before) | set(after)):
        was, now = before.get(date), after.get(date)
        if was is None or now is None or was == now:
            continue
        out.append((date, was, now, TRANSITIONS.get((was, now), "%s -> %s" % (was, now))))
    return out


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
            if state.data.get("watch_override") == keys:
                continue                      # already watching that; say nothing
            state.data["watch_override"] = keys
            state.data["alerted"] = {}        # a new calendar starts fresh
            state.save()
            changed = True
            notifier.menu("Now watching:\n%s"
                          % watching_line(active_targets(cfg, state)), menu_buttons())

        elif value in ("watch", "switch", "menu", "change", "start", "help"):
            notifier.menu(
                "%s\n\nTap to change. Send /status for today's report."
                % watching_line(active_targets(cfg, state)), menu_buttons())

        elif value == "status":
            # The same report the daily message uses, on demand.
            check = self_check(cfg, notifier, state)
            notifier.report(
                daily_report_text(cfg, state, state.data.get("date") or
                                  time.strftime("%Y-%m-%d",
                                                time.gmtime(time.time() + TASHKENT_OFFSET)),
                                  check),
                title="REPORT - TODAY SO FAR")

    return changed


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------

def poll_interval(cfg, now=None):
    """Seconds to wait before looking again.

    Slots were observed appearing around 19:00 Tashkent and being gone inside two
    minutes, so the hot window is checked several times a minute.
    """
    hour = time.gmtime(now if now is not None else time.time()).tm_hour
    hot_lo, hot_hi = cfg["hot_hours_utc"]
    day_lo, day_hi = cfg["day_hours_utc"]
    if hot_lo <= hour < hot_hi:
        return cfg["poll_seconds_hot"]
    if day_lo <= hour < day_hi:
        return cfg["poll_seconds_day"]
    return cfg["poll_seconds_night"]


def run(cfg, notifier, state, deadline):
    targets = active_targets(cfg, state)
    # One session per calendar, kept alive across cycles so the calendar is
    # selected once rather than re-selected on every pass.
    sessions = {t["key"]: visasite.Site() for t in targets}
    last_success = time.time()
    stale_warned = False

    while time.time() < deadline:
        now = time.time()
        today = time.strftime("%Y-%m-%d", time.gmtime(now + TASHKENT_OFFSET))
        state.roll_day(today)

        acted = handle_commands(cfg, notifier, state)
        if notifier.tg.offset != state.data.get("tg_offset"):
            state.data["tg_offset"] = notifier.tg.offset
            state.save()
        if acted:
            targets = active_targets(cfg, state)
            sessions = {t["key"]: visasite.Site() for t in targets}
            log("retargeted: %s" % ", ".join(t["key"] for t in targets))

        interval = poll_interval(cfg, now)
        trouble = False

        # Scan every calendar at once. Run one after another they simply add up,
        # and the whole point is to be looking at all of them at the same moment.
        cycle0 = time.time()
        results = {}

        def scan_into(t):
            try:
                t0 = time.time()
                results[t["key"]] = (scan_target(cfg, sessions[t["key"]], t),
                                     time.time() - t0, None)
            except Exception as e:                   # noqa: BLE001 - handled below
                results[t["key"]] = (None, 0, e)

        workers = [threading.Thread(target=scan_into, args=(t,)) for t in targets]
        for w in workers:
            w.start()
        for w in workers:
            w.join()

        for tgt in targets:
            key = tgt["key"]
            payload, took, failure = results.get(key, (None, 0, None))
            try:
                if failure:
                    raise failure
                states, unknown = payload
                state.data["checks"] += 1
                last_success = time.time()

                if unknown:
                    notifier.attention(
                        "The calendar layout changed and cannot be read reliably.",
                        "Calendar: %s\n\n%s\n\nStill running, but do not trust "
                        "\"no slots\" until this is looked at."
                        % (tgt["label"], "\n".join(unknown[:6])),
                        throttle_key="markup-%s" % key)

                open_dates = sorted(d for d, st in states.items()
                                    if st == visasite.Day.OPEN)
                log("ok [%s] %.1fs: %d days, open %s"
                    % (key, took, len(states), open_dates or "none"))

                handle_changes(cfg, notifier, state, tgt, states)
                alert_openings(cfg, notifier, state, tgt, open_dates)

            except visasite.WrongCalendar as e:
                trouble = True
                state.data["wrong_calendar"] += 1
                state.data["failures"] += 1
                state.save()
                log("WRONG CALENDAR [%s]: %s" % (key, e))
                state.note_event("wrong-calendar", "wrong calendar served (%s)" % tgt["headline"])
                notifier.attention(
                    "The site served the WRONG calendar.",
                    "Asked for: %s\n\n%s\n\nIt keeps retrying and will NOT report "
                    "\"no slots\" for this calendar while this lasts."
                    % (tgt["label"], e),
                    throttle_key="wrong-%s" % tgt["key"])
                sessions[key] = visasite.Site()      # fresh session, straight back in

            except visasite.FetchFailed as e:
                trouble = True
                state.data["failures"] += 1
                state.note_event("read-failed", "could not read site (%s)" % e)
                log("read failed [%s] (%s) - retrying in a moment" % (key, e))
                blind = time.time() - last_success
                if blind > cfg["stale_read_warning_seconds"] and not stale_warned:
                    stale_warned = True
                    notifier.attention(
                        "Cannot read the calendar right now.",
                        "Nothing has been read for %d minutes.\nLast error: %s\n\n"
                        "Still retrying every few seconds. This is NOT \"no slots\"."
                        % (blind / 60, e),
                        throttle_key="stale")
                if blind > 60:
                    sessions[key] = visasite.Site()  # session may have gone sour

        if stale_warned and not trouble:
            log("reading normally again after a stale spell")
            stale_warned = False

        state.save()
        maybe_daily_report(cfg, notifier, state)
        log("cycle %.1fs, next look in %ss" % (time.time() - cycle0,
                                               5 if trouble else interval))
        time.sleep(5 if trouble else interval)


def handle_changes(cfg, notifier, state, tgt, states):
    """Record every state change, and say so when a release happens.

    A day going straight from "no reception" to "full" is how a release that was
    taken within a minute looks afterwards. Reporting it is worth doing even
    though the seats are gone: it means a release is happening right now, and
    there may be a leftover seat a refresh away.
    """
    seen = state.data.setdefault("seen", {})
    before = seen.get(tgt["key"])
    seen[tgt["key"]] = states

    if before is None:
        return                      # first look after a restart is the baseline

    changes = diff_states(before, states)
    if not changes:
        return

    stamp = time.strftime("%H:%M:%S", time.gmtime(time.time() + TASHKENT_OFFSET))
    for date, was, now, wording in changes:
        state.data.setdefault("transitions", []).append(
            {"at": stamp, "calendar": tgt["headline"], "date": date,
             "from": was, "to": now, "what": wording})
        log("CHANGE [%s] %s %s -> %s" % (tgt["key"], date, was, now))
    del state.data["transitions"][:-40]
    state.save()

    # Days that became bookable are handled by the alarm; anything else is news
    # you want quickly but should not be woken by.
    quiet = [c for c in changes if c[2] != visasite.Day.OPEN]
    if quiet:
        notifier.changed(
            "%s\n\n%s\n\n%s" % (tgt["headline"],
                                "\n".join("%s  -  %s" % (pretty_date(d), w)
                                          for d, _, _, w in quiet),
                                visasite.CALENDAR_PAGE))


def alert_openings(cfg, notifier, state, tgt, open_dates):
    """One alarm per calendar, listing every date that opened.

    Fired straight off the month grid. Opening each day to read its seat counts
    first cost another few seconds per day on the one path where seconds decide
    whether the slot is still there, and the alert does not mention seats anyway.
    """
    fresh = [d for d in open_dates if state.data["alerted"].get("%s|%s" % (tgt["key"], d)) != "open"]
    if not fresh:
        return

    text = describe(fresh, cfg, tgt)
    result = notifier.alarm("%s slots available" % tgt["headline"], text,
                            cfg["alarm_repeat_seconds"], cfg["alarm_max_seconds"],
                            click=visasite.CALENDAR_PAGE)
    for date in fresh:
        state.data["alerted"]["%s|%s" % (tgt["key"], date)] = "open"
        state.data["found"].append({
            "calendar": tgt["headline"],
            "date": date,
            "alerted_at": time.strftime("%H:%M", time.gmtime(time.time() + TASHKENT_OFFSET)),
            "telegram": result["telegram_confirmed"],
            "ntfy": result["ntfy_confirmed"],
        })
    state.save()


def maybe_daily_report(cfg, notifier, state):
    tash = time.gmtime(time.time() + TASHKENT_OFFSET)
    today = time.strftime("%Y-%m-%d", tash)
    if tash.tm_hour < cfg["daily_report_hour_tashkent"]:
        return
    if state.data.get("summary_sent_for") == today:
        return
    check = self_check(cfg, notifier, state)
    notifier.report(daily_report_text(cfg, state, today, check))
    state.data["summary_sent_for"] = today
    state.save()


def self_check(cfg, notifier, state):
    """Prove the whole chain still works, without ringing anybody.

    Reads the live site and confirms it serves the calendar asked for, then sends
    one silent ntfy message and reads it back off the server. Telegram needs no
    separate test: the report itself is the test.
    """
    result = {"site": False, "calendar": False, "ntfy": False, "detail": ""}
    tgt = active_targets(cfg, state)[0]
    try:
        s = visasite.Site()
        s.select(tgt["category"], tgt["event"], tgt.get("plan"))
        retrying(lambda: s.month(*months_to_scan(1)[0]), attempts=4, pause=6)
        result["site"] = result["calendar"] = True
    except visasite.WrongCalendar as e:
        result["site"] = True
        result["detail"] = "wrong calendar: %s" % e
    except visasite.FetchFailed as e:
        result["detail"] = "site unreadable: %s" % e

    if notifier.ntfy:
        d = notifier.ntfy.send("Daily check", "Monitor is alive. No action needed.",
                               priority="min", tags="white_check_mark")
        result["ntfy"] = d.ok
        if not d.ok:
            result["detail"] = (result["detail"] + "; " if result["detail"] else "") + \
                               "ntfy: %s" % d.detail
    else:
        result["ntfy"] = None
    return result


def tick(ok):
    return "OK" if ok else ("skipped" if ok is None else "FAILED")


def daily_report_text(cfg, state, today, check):
    d = state.data
    L = [pretty_date(today), ""]

    # What it is watching goes first: it is the question worth answering fastest,
    # and a switch that failed to apply shows up here immediately.
    L.append("\U0001F441 WATCHING")
    for t in active_targets(cfg, state):
        L.append("   %s" % t["label"])
    L.append("")

    found = d.get("found") or []
    L.append("\U0001F514 SLOTS FOUND")
    if not found:
        L.append("   none")
    else:
        for f in found:
            L.append("   %s  %s" % (pretty_date(f["date"]), f.get("calendar", "")))
            L.append("      alerted %s%s" % (f["alerted_at"],
                     "" if f["telegram"] and f["ntfy"] else "   DELIVERY PROBLEM"))
    L.append("")

    # Every state change with the time it happened. Over a few days this is what
    # tells us when the embassy actually releases, instead of guessing from one
    # observation.
    trans = d.get("transitions") or []
    L.append("\U0001F504 CALENDAR CHANGES")
    if not trans:
        L.append("   none")
    else:
        for t in trans[-10:]:
            L.append("   %s  %s  %s" % (t["at"], pretty_date(t["date"]), t["what"]))
    L.append("")

    L.append("\U0001F50D ACTIVITY")
    L.append("   %s checks" % d.get("checks", 0))
    L.append("   %s failed reads" % d.get("failures", 0))
    L.append("")

    events = d.get("events") or []
    L.append("⚠ PROBLEMS")
    if not events:
        L.append("   none")
    else:
        for e in events[-8:]:
            span = e["at"] if not e.get("until") else "%s-%s" % (e["at"], e["until"])
            times = "" if e.get("count", 1) == 1 else "  x%d" % e["count"]
            L.append("   %s  %s%s" % (span, e["detail"], times))
    L.append("")

    L.append("\U0001F9EA SELF-CHECK")
    L.append("   site            %s" % tick(check["site"]))
    L.append("   right calendar  %s" % tick(check["calendar"]))
    L.append("   phone alarm     %s" % tick(check["ntfy"]))
    L.append("   telegram        OK")
    drill = d.get("drill")
    if drill:
        L.append("   alarm drill     %s  (%s)"
                 % ("OK" if drill.get("passed") else "FAILED", drill.get("when", "?")))
    if check["detail"]:
        L.append("")
        L.append("   %s" % check["detail"])

    healthy = check["site"] and check["calendar"] and check["ntfy"] is not False
    L.append("")
    L.append("─" * 12)
    L.append("✅ Everything working" if healthy else "❗ Needs your attention")
    return "\n".join(L)


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


def selftest(cfg, notifier, state=None):
    """Fire a real alert end to end and report honestly whether it worked."""
    text = ("This is a drill, not a real slot.\n\n%s\n\n%s"
            % (pretty_date(time.strftime("%Y-%m-%d")), visasite.CALENDAR_PAGE))
    result = notifier.alarm("Self-test (drill)", text,
                            repeat_seconds=cfg["alarm_repeat_seconds"], max_seconds=120)
    ok = result["telegram_confirmed"] and (notifier.ntfy is None or result["ntfy_confirmed"])

    # A passing drill is not news -- it is recorded and shown in the daily report.
    # Only a failure is worth interrupting anybody for.
    if state is not None:
        state.data["drill"] = {
            "passed": ok,
            "when": time.strftime("%d %b", time.gmtime(time.time() + TASHKENT_OFFSET)),
        }
        state.save()
    if not ok:
        notifier.attention(
            "The weekly alarm drill FAILED - you might not be reachable.",
            "Telegram delivered: %s\nntfy delivered: %s\nBuzzes sent: %d%s"
            % ("yes" if result["telegram_confirmed"] else "NO",
               "yes" if result["ntfy_confirmed"] else "NO", result["rounds"],
               "" if not result["problems"] else
               "\n\n" + "; ".join(result["problems"][:3])))
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
        return selftest(cfg, notifier, State(args.state))

    if args.once:
        report = []
        for tgt in active_targets(cfg, State(args.state)):
            # A one-shot check gets one chance, and this site throws a 500 at
            # roughly one request in thirteen. Keep trying for a minute before
            # calling it a real failure; the live loop retries forever anyway.
            sess = visasite.Site()
            states, unknown = retrying(lambda: scan_target(cfg, sess, tgt),
                                       attempts=8, pause=8)
            open_dates = sorted(d for d, st in states.items() if st == visasite.Day.OPEN)
            openings = []
            for date in open_dates:                 # diagnostics only, never in the loop
                html = sess.day(date)
                free = [x for x in visasite.parse_day(html) if x.available]
                openings.append({"date": date, "slots": free,
                                 "max_group": visasite.max_group_size(html)})
            report.append({
                "calendar": tgt["headline"],
                "label": tgt["label"],
                "category": tgt["category"], "event": tgt["event"],
                "days_read": len(states),
                "reception_days": sorted(d for d, st in states.items() if st != "none"),
                "open_days": open_dates,
                "unreadable": unknown,
                "openings": [{"date": o["date"], "slots": [x.describe() for x in o["slots"]],
                              "max_group": o["max_group"]} for o in openings],
            })
        print(json.dumps(report, indent=2))
        return 0

    state = State(args.state)
    notifier.tg._offset = state.data.get("tg_offset")
    log("resuming Telegram reads at offset %s" % notifier.tg.offset)
    notifier.tg.register_commands([
        ("watch", "Change which calendars are watched"),
        ("status", "What it is watching and today's counts"),
        ("stop", "Stop a ringing alarm"),
    ])
    minutes = args.minutes if args.minutes is not None else cfg["run_minutes"]
    deadline = time.time() + minutes * 60
    log("monitor starting: watching %s | %d months | poll %ss hot / %ss day / %ss night"
        % (", ".join(t["headline"] for t in active_targets(cfg, state)),
           cfg["months_ahead"] + 1, cfg["poll_seconds_hot"],
           cfg["poll_seconds_day"], cfg["poll_seconds_night"]))
    try:
        run(cfg, notifier, state, deadline)
    except KeyboardInterrupt:
        return 0
    except Exception:
        tb = traceback.format_exc()
        log(tb)
        notifier.attention("The monitor crashed and has stopped watching.",
                           "A fresh run should take over within 15 minutes.\n\n%s"
                           % tb.strip().splitlines()[-1][:300])
        return 1
    log("run window finished cleanly; a fresh run takes over")
    return 0


if __name__ == "__main__":
    sys.exit(main())
