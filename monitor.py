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
                 "found": [], "events": [], "transitions": []}

    def __init__(self, path):
        self.path = path
        # "seen" and "alerted" describe the calendar, not the day's tally, so
        # neither may be cleared at midnight. Clearing "alerted" is what made six
        # still-open days ring again at 00:00 Tashkent, waking someone for news
        # they already had.
        self.data = {"date": None, "summary_sent_for": None, "watch_override": None,
                     "seen": {}, "alerted": {}, "notifications": [], "ringing_for": {},
                     "seatlog": {}, "journey": {}}
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

    def prune_alerted(self, today):
        """Forget dates that have already passed, so the record cannot grow forever."""
        for name in ("alerted", "seatlog", "journey"):
            book = self.data.get(name) or {}
            for k in [k for k in book if k.rsplit("|", 1)[-1] < today]:
                del book[k]

    def roll_day(self, today):
        if self.data.get("date") != today:
            self.prune_alerted(today)
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


def scan_target(cfg, s, tgt, months=None):
    """Read every month of one calendar at once.

    The months are fetched in parallel because they are independent and the site
    answers in about three and a half seconds each; one after another that is the
    difference between seeing a slot and reading about it afterwards.

    Returns (states, unknown) where states maps YYYY-MM-DD to open/full/none.
    Raises whatever the fetches raised, so a wrong calendar still stops the scan.
    """
    ensure_selected(s, tgt)
    if months is None:
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
    ("waitlist", "open"): "SEATS RELEASED off the waiting list",
    ("open", "full"): "filled up",
    ("open", "none"): "withdrawn",
    ("full", "none"): "reception withdrawn",
    ("none", "waitlist"): "waiting list opened (no seats to book)",
    ("full", "waitlist"): "moved to waiting list (no seats to book)",
    ("open", "waitlist"): "seats gone, waiting list only",
    ("waitlist", "full"): "waiting list closed",
    ("waitlist", "none"): "waiting list withdrawn",
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

        if value.startswith("stop:"):
            who = value.split(":", 1)[1]
            stopped = notifier.stop_ringing(who)
            log("stopped alarms: %s" % (stopped or "none were ringing"))

        elif value in notifier.STOP_WORDS:
            stopped = notifier.stop_ringing(None)
            log("stopped all alarms: %s" % (stopped or "none were ringing"))

        elif value.startswith("set:"):
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

        elif value in ("log", "history", "notifications"):
            placeholder = notifier.working("Fetching the notification log")
            notifier.replace(placeholder.ident, notifier.REPORT, "NOTIFICATION LOG",
                             notification_log_text(state), journal_as="REPORT")

        elif value == "status":
            # Answered from what the loop already knows. It re-read the calendar
            # seconds ago, so going back to the site here only added ten seconds
            # of silence to a question that was already answered.
            placeholder = notifier.working("Checking")
            today = state.data.get("date") or time.strftime(
                "%Y-%m-%d", time.gmtime(time.time() + TASHKENT_OFFSET))
            notifier.replace(placeholder.ident, notifier.REPORT, "STATUS UPDATE",
                             daily_report_text(cfg, state, today, None),
                             journal_as="REPORT")

    return changed


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------

def is_hot(cfg, now=None):
    """Are we inside the hours when the embassy actually releases days?

    Observed between 16:00 and 21:00 Tashkent. Outside it we look once a minute,
    which keeps the load off a site that already falls over on its own.
    """
    hour = time.gmtime(now if now is not None else time.time()).tm_hour
    lo, hi = cfg["hot_hours_utc"]
    return lo <= hour < hi


def poll_interval(cfg, now=None):
    return cfg["poll_seconds_hot"] if is_hot(cfg, now) else cfg["poll_seconds_rest"]


def months_for(cfg, hot, due_full):
    """Which months this pass should read.

    A release lands about eleven days ahead, so during the rush only this month
    and next can possibly hold one. Reading the third month too made every check
    a third slower and a third heavier for nothing; it is swept on a timer
    instead so nothing unusual can hide there.
    """
    everything = months_to_scan(cfg["months_ahead"] + 1)
    if not hot or due_full:
        return everything
    return everything[:max(1, cfg.get("hot_months", 2))]


def run(cfg, notifier, state, deadline):
    targets = active_targets(cfg, state)
    # One session per calendar, kept alive across cycles so the calendar is
    # selected once rather than re-selected on every pass.
    sessions = {t["key"]: visasite.Site() for t in targets}
    last_success = time.time()
    stale_warned = False
    last_full_sweep = 0.0

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

        hot = is_hot(cfg, now)
        due_full = (now - last_full_sweep) >= cfg["full_sweep_seconds"]
        months = months_for(cfg, hot, due_full)
        if len(months) == cfg["months_ahead"] + 1:
            last_full_sweep = now
        interval = poll_interval(cfg, now)
        trouble = False

        # Everything the bot sent since the last pass, with the second it went
        # out. Alarm buzzes delete themselves, so this is the only record of
        # which message came first.
        for entry in notifier.take_journal():
            state.data.setdefault("notifications", []).append(entry)
        del state.data.setdefault("notifications", [])[:-120]

        # Alarms that finished since the last pass: record how delivery went.
        for done in notifier.take_finished():
            log("alarm '%s' ended after %ds (%s), telegram=%s ntfy=%s"
                % (done.get("key"), done["seconds"], done["stopped_by"],
                   done["telegram_confirmed"], done["ntfy_confirmed"]))
            for f in state.data.get("found", []):
                if f.get("telegram") is None:
                    f["telegram"] = done["telegram_confirmed"]
                    f["ntfy"] = done["ntfy_confirmed"]

        # Scan every calendar at once. Run one after another they simply add up,
        # and the whole point is to be looking at all of them at the same moment.
        cycle0 = time.time()
        results = {}

        def scan_into(t):
            try:
                t0 = time.time()
                results[t["key"]] = (scan_target(cfg, sessions[t["key"]], t, months),
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
                state.data["last_read_at"] = last_success

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

                handle_changes(cfg, notifier, state, tgt, states)   # months-safe
                stop_if_gone(cfg, notifier, state, tgt, open_dates)
                alert_openings(cfg, notifier, state, tgt, open_dates)
                probe_seats(cfg, sessions, state, tgt, open_dates)

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

            except visasite.Blocked as e:
                trouble = True
                state.data["failures"] += 1
                state.note_event("blocked", "site refused us (%s)" % e)
                log("BLOCKED [%s]: %s" % (key, e))
                notifier.attention(
                    "The site is REFUSING us, not just failing.",
                    "%s\n\nThis is different from the usual server errors: it "
                    "means we are being turned away. Worth slowing down or "
                    "checking from somewhere else." % e,
                    throttle_key="blocked")

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
        log("cycle %.1fs over %d month(s)%s, next look in %ss"
            % (time.time() - cycle0, len(months), " [hot]" if hot else "",
               5 if trouble else interval))
        wait_and_listen(cfg, notifier, state, 5 if trouble else interval)


def handle_changes(cfg, notifier, state, tgt, states):
    """Record every state change, and say so when a release happens.

    A day going straight from "no reception" to "full" is how a release that was
    taken within a minute looks afterwards. Reporting it is worth doing even
    though the seats are gone: it means a release is happening right now, and
    there may be a leftover seat a refresh away.
    """
    seen = state.data.setdefault("seen", {})
    before = seen.get(tgt["key"])

    # Merge rather than replace: a hot pass reads only the near months, and
    # overwriting would throw away what we know about the rest. diff_states
    # ignores any date missing from either side, so a partial pass can never
    # invent a change for a month it did not look at.
    merged = dict(before or {})
    merged.update(states)
    seen[tgt["key"]] = merged

    if before is None:
        return                      # first look after a restart is the baseline

    changes = diff_states(before, states)
    if not changes:
        return

    stamp = time.strftime("%H:%M:%S", time.gmtime(time.time() + TASHKENT_OFFSET))
    reasons = {}
    for date, was, now, wording in changes:
        note_journey(state, tgt["key"], date, was)     # seed, if this is the first we saw
        note_journey(state, tgt["key"], date, now)
        why = ""
        if was == visasite.Day.OPEN:
            why = explain_loss(state, tgt["key"], date,
                               seat_trail(state, tgt["key"], date), now)
            reasons[date] = why
        verdict = settled_verdict(state, tgt["key"], date)
        if verdict:
            reasons[date] = (reasons.get(date) + "\n   " if reasons.get(date) else "") + verdict
        state.data.setdefault("transitions", []).append(
            {"at": stamp, "calendar": tgt["headline"], "date": date,
             "from": was, "to": now, "what": wording, "why": why})
        log("CHANGE [%s] %s %s -> %s%s"
            % (tgt["key"], date, was, now, "  (%s)" % why if why else ""))
    del state.data["transitions"][:-40]
    state.save()

    # Days that became bookable are handled by the alarm; anything else is news
    # you want quickly but should not be woken by.
    quiet = [c for c in changes if c[2] != visasite.Day.OPEN]
    if quiet:
        lines = []
        for d, _, _, w in quiet:
            lines.append("%s  -  %s" % (pretty_date(d), w))
            if reasons.get(d):
                lines.append("   %s" % reasons[d])
            hist = journey_lines(state, tgt["key"], d)
            if len(hist) > 1:
                lines.append("   how it got here:")
                lines.extend(hist)
            lines.append("")
        notifier.changed("%s\n\n%s\n%s"
                         % (tgt["headline"], "\n".join(lines).strip(),
                            visasite.CALENDAR_PAGE))


def wait_and_listen(cfg, notifier, state, seconds):
    """Sleep, but keep listening while an alarm is ringing.

    Button presses are read in one place only, so two alarms can never swallow
    each other's STOP. The cost is that a press waits for the next look -- so
    while something is actually ringing, we check every couple of seconds.
    """
    end = time.time() + seconds
    while True:
        left = end - time.time()
        if left <= 0:
            return
        if not notifier.ringing():
            time.sleep(min(left, 1.0))
            continue
        handle_commands(cfg, notifier, state)
        time.sleep(min(left, 2.0))


def probe_seats(cfg, sessions, state, tgt, open_dates):
    """While a day is bookable, keep a note of how many seats it has left.

    Runs in the background so it never delays an alarm. The point is not the
    number itself -- alerts do not mention seats -- but the trail it leaves. A
    day whose seats fall 20, 14, 6 was being booked by people; a day that still
    had twenty seats and then vanished was taken off the calendar by the embassy.
    Without this the two are indistinguishable afterwards.
    """
    if not open_dates:
        return
    every = cfg.get("seat_probe_seconds", 15)
    now = time.time()
    trails = state.data.setdefault("seatlog", {})

    def probe(date):
        slot = "%s|%s" % (tgt["key"], date)
        trail = trails.setdefault(slot, [])
        if trail and now - trail[-1]["t"] < every:
            return
        try:
            html = sessions[tgt["key"]].day(date)
            free = [x for x in visasite.parse_day(html) if x.available]
            seats = sum(x.seats for x in free if x.seats is not None) or None
            if seats is None and free:
                seats = -1                      # bookable but the site did not say
        except Exception:
            return                              # a failed probe is never fatal
        trail.append({"t": now, "seats": seats})
        del trail[:-12]

    for d in open_dates[:4]:                    # a handful is plenty; stay light
        threading.Thread(target=probe, args=(d,), daemon=True).start()


def seat_trail(state, tgt_key, date):
    return (state.data.get("seatlog") or {}).get("%s|%s" % (tgt_key, date)) or []


PLAIN_STATE = {
    visasite.Day.OPEN: "bookable",
    visasite.Day.FULL: "reception, no seats",
    visasite.Day.WAITLIST: "waiting list only",
    visasite.Day.NONE: "not on the calendar",
}


def note_journey(state, tgt_key, date, new_state):
    """Keep each date's own history of states, with the moment of each change."""
    j = state.data.setdefault("journey", {}).setdefault("%s|%s" % (tgt_key, date), [])
    if j and j[-1]["state"] == new_state:
        return j
    j.append({"t": time.time(), "state": new_state})
    del j[:-16]
    return j


def journey_lines(state, tgt_key, date):
    j = (state.data.get("journey") or {}).get("%s|%s" % (tgt_key, date)) or []
    return ["   %s  %s" % (notify.stamp(p["t"]), PLAIN_STATE.get(p["state"], p["state"]))
            for p in j[-6:]]


def explain_loss(state, tgt_key, date, trail, now_state):
    """Say what is known about a day leaving the bookable state.

    Earlier this announced "the embassy took the day off the calendar" the moment
    a day went missing. That was wrong: on 23 August both calendars dropped
    3 September and brought it straight back as waiting list, so "gone" was a
    step in a reconfiguration, not the end of the story. Nothing is now concluded
    from a single hop -- a day that vanishes is reported as vanished, and the
    verdict waits until it settles.
    """
    seats = [p["seats"] for p in trail if p.get("seats") not in (None, -1)]
    trail_txt = " to ".join(str(x) for x in seats[-4:]) if seats else ""

    if len(seats) >= 2 and seats[-1] < seats[0]:
        return "applicants were booking it - seats fell %s" % trail_txt

    if now_state == visasite.Day.NONE:
        return ("off the calendar for the moment%s - it may come back as a waiting "
                "list, so this is not yet a removal"
                % (" (it still had %s seats)" % seats[-1] if seats else ""))

    if now_state == visasite.Day.WAITLIST:
        base = "switched to waiting list"
        if seats and seats[-1] >= 3:
            return base + " while it still had %d seats, so it was closed rather than filled" % seats[-1]
        return base
    if seats and seats[-1] >= 3:
        return "still had %d seats at the last look, so it was closed rather than filled" % seats[-1]
    if seats and seats[-1] == 0:
        return "seats ran out - applicants took them"
    return "no seat reading before it went, so the cause cannot be told from here"


def settled_verdict(state, tgt_key, date, within=600):
    """What the whole sequence says, once a date has stopped moving.

    A day that goes bookable, vanishes, then reappears as a waiting list inside a
    few minutes was being reconfigured. Booking cannot do that; only the embassy
    can. This is the conclusion the single-hop version got wrong.
    """
    j = (state.data.get("journey") or {}).get("%s|%s" % (tgt_key, date)) or []
    if len(j) < 3:
        return ""
    recent = [p for p in j if time.time() - p["t"] <= within]
    states = [p["state"] for p in recent]
    if visasite.Day.OPEN in states and states[-1] in (visasite.Day.WAITLIST, visasite.Day.FULL):
        if visasite.Day.NONE in states:
            return ("it opened, dropped off the calendar and came back as %s within "
                    "minutes - that is the embassy reconfiguring the day, not "
                    "applicants booking it" % PLAIN_STATE[states[-1]])
        return "it opened and went straight to %s" % PLAIN_STATE[states[-1]]
    return ""


def stop_if_gone(cfg, notifier, state, tgt, open_dates):
    """Silence an alarm the instant the thing it is ringing about disappears.

    A slot that opens and vanishes within a minute used to leave the phone
    ringing for the full fifteen, long after there was anything to book. The
    dates each alarm was raised for are remembered, so when none of them is still
    open the ringing stops and says why.
    """
    key = tgt["key"]
    was_for = (state.data.get("ringing_for") or {}).get(key) or []
    if not was_for or key not in notifier.ringing():
        return

    still = [d for d in was_for if d in open_dates]
    if still:
        return

    notifier.stop_ringing(key, reason="the slot disappeared")
    state.data.get("ringing_for", {}).pop(key, None)
    # Let it ring again if the same date genuinely re-opens later.
    for d in was_for:
        state.data["alerted"].pop("%s|%s" % (key, d), None)
    state.save()
    log("STOPPED ALARM [%s]: %s no longer open" % (key, was_for))
    lines = []
    for d in was_for:
        lines.append(pretty_date(d))
        why = explain_loss(state, key, d, seat_trail(state, key, d),
                           visasite.Day.NONE if d not in open_dates
                           else visasite.Day.OPEN)
        if why:
            lines.append("   %s" % why)
    notifier.changed(
        "%s\n\nAlarm stopped - the slot is gone.\n%s\n\nIf it comes back you "
        "will be rung again.\n\n%s"
        % (tgt["headline"], "\n".join(lines), visasite.CALENDAR_PAGE),
        throttle_key="gone-%s" % key, throttle_seconds=0)


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
    # Rings on its own thread. Each calendar has its own alarm and its own STOP
    # button, so the applicant calendar opening never silences or delays the
    # representative one, and watching carries on while both are ringing.
    notifier.ring(tgt["key"], "%s slots available" % tgt["headline"], text,
                  cfg["alarm_repeat_seconds"], cfg["alarm_max_seconds"],
                  click=visasite.CALENDAR_PAGE,
                  subject="%s  %s" % (tgt["headline"],
                                      ", ".join(pretty_date(d) for d in fresh)))
    # Remember what this alarm is about, so it can be silenced when it is over.
    state.data.setdefault("ringing_for", {})[tgt["key"]] = list(fresh)
    for date in fresh:
        note_journey(state, tgt["key"], date, visasite.Day.OPEN)
        state.data["alerted"]["%s|%s" % (tgt["key"], date)] = "open"
        state.data["found"].append({
            "calendar": tgt["headline"],
            "date": date,
            "alerted_at": time.strftime("%H:%M", time.gmtime(time.time() + TASHKENT_OFFSET)),
            "telegram": None, "ntfy": None,     # filled in when the alarm ends
        })
    state.save()


def maybe_daily_report(cfg, notifier, state):
    """Once a day, prove the whole chain still works -- and say nothing if it does.

    The daily report itself is off: it arrived whether or not anything had
    happened, and /status answers the same question on demand. What remains is
    the check behind it, because a dead ntfy channel or a calendar that stopped
    switching would otherwise sit unnoticed until the morning it mattered. Only a
    failure now produces a message.
    """
    hour = cfg.get("daily_report_hour_tashkent")
    if hour is None:
        return
    tash = time.gmtime(time.time() + TASHKENT_OFFSET)
    today = time.strftime("%Y-%m-%d", tash)
    if tash.tm_hour < hour or state.data.get("summary_sent_for") == today:
        return

    check = self_check(cfg, notifier, state)
    state.data["summary_sent_for"] = today
    state.data["last_health_check"] = {
        "when": time.strftime("%d %b %H:%M", tash),
        "ok": bool(check["site"] and check["calendar"] and check["ntfy"] is not False),
        "detail": check["detail"],
    }
    state.save()

    if cfg.get("send_daily_report"):
        notifier.report(daily_report_text(cfg, state, today, check))
        return

    if state.data["last_health_check"]["ok"]:
        log("daily health check passed silently: %s" % check)
        return
    notifier.attention(
        "The daily health check FAILED.",
        "site reachable   %s\nright calendar   %s\nphone alarm      %s\n\n%s\n\n"
        "Send /status for the full picture."
        % (tick(check["site"]), tick(check["calendar"]), tick(check["ntfy"]),
           check["detail"] or "no further detail"),
        throttle_key="healthcheck")


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


NOTE_ICON = {
    "ALARM START": "\U0001F514", "ALARM REPEAT": "\U0001F501",
    "ALARM STOP": "\U0001F515", "CHANGED": "\U0001F440",
    "PROBLEM": "⚠", "REPORT": "\U0001F4CA",
}


def notification_log_text(state, limit=25):
    """Every message the bot sent, newest last, timed to the second.

    Exists because an alarm replaces its own buzz each time it rings, so after
    the fact there is no way to tell whether the alarm or the "it is gone"
    message came first. Now there is.
    """
    notes = (state.data.get("notifications") or [])[-limit:]
    if not notes:
        return "Nothing sent yet since the monitor last started."

    lines, day = [], None
    for n in notes:
        tash = time.gmtime(n["t"] + TASHKENT_OFFSET)
        d = time.strftime("%Y-%m-%d", tash)
        if d != day:
            day = d
            lines.append("")
            lines.append(pretty_date(d))
        lines.append("%s  %s %-13s %s"
                     % (notify.stamp(n["t"]),
                        NOTE_ICON.get(n["kind"], "·"), n["kind"], n["what"]))
        if n.get("detail"):
            lines.append("%s   %s" % (" " * 12, n["detail"]))
        # Every buzz, with the moment it went out. The buzzes delete each other
        # in the chat, so this is the only place the repetitions survive.
        for i, rt in enumerate(n.get("rings") or [], 1):
            lines.append("%s   ring %-2d %s" % (" " * 12, i, notify.stamp(rt)))
    lines.append("")
    lines.append("Times are Tashkent. Newest at the bottom.")
    return "\n".join(lines).strip()


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
            # None means the alarm was still ringing when this was written, which
            # is not a failure -- only an explicit False is.
            bad = (f.get("telegram") is False) or (f.get("ntfy") is False)
            L.append("      alerted %s%s" % (f["alerted_at"],
                     "   DELIVERY PROBLEM" if bad else ""))
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

    waiting = sorted({d for st in (d.get("seen") or {}).values()
                      for d, v in st.items() if v == "waitlist"})
    if waiting:
        L.append("\U0001F7E6 WAITING LIST (no seats to book)")
        for date in waiting[:8]:
            L.append("   %s" % pretty_date(date))
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

    L.append("\U0001F9EA HEALTH")
    last = d.get("last_read_at")
    if last:
        ago = int(time.time() - last)
        fresh = "%ds ago" % ago if ago < 120 else "%d min ago" % (ago // 60)
        L.append("   last good read  %s" % fresh)
    if check is None:
        # Answered from the loop's own reading rather than a fresh probe, which
        # is why this comes back at once instead of after ten seconds.
        L.append("   site            %s" % ("OK" if last and time.time() - last < 180
                                            else "NOT READ RECENTLY"))
        L.append("   telegram        OK")
    else:
        L.append("   site            %s" % tick(check["site"]))
        L.append("   right calendar  %s" % tick(check["calendar"]))
        L.append("   phone alarm     %s" % tick(check["ntfy"]))
        L.append("   telegram        OK")
    hc = d.get("last_health_check")
    if hc:
        L.append("   daily check     %s  (%s)"
                 % ("OK" if hc.get("ok") else "FAILED", hc.get("when", "?")))
    drill = d.get("drill")
    if drill:
        L.append("   alarm drill     %s  (%s)"
                 % ("OK" if drill.get("passed") else "FAILED", drill.get("when", "?")))
    if check and check["detail"]:
        L.append("")
        L.append("   %s" % check["detail"])

    if check is None:
        healthy = bool(last) and (time.time() - last) < 180
    else:
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
        ("log", "Every notification sent, timed to the second"),
        ("stop", "Stop a ringing alarm"),
    ])
    minutes = args.minutes if args.minutes is not None else cfg["run_minutes"]
    deadline = time.time() + minutes * 60
    lo, hi = cfg["hot_hours_utc"]
    log("monitor starting: watching %s | %ss during %02d-%02d UTC over %d months, "
        "%ss otherwise over %d months"
        % (", ".join(t["headline"] for t in active_targets(cfg, state)),
           cfg["poll_seconds_hot"], lo, hi, cfg.get("hot_months", 2),
           cfg["poll_seconds_rest"], cfg["months_ahead"] + 1))
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
