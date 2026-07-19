#!/usr/bin/env python3
"""Monitor the Japanese-embassy (Uzbekistan) visa reservation calendar for open days.

Talks directly to the site's own AJAX endpoint (no browser), keeps one session
alive, checks every ~35-75s with jitter, and alerts via Telegram message +
CallMeBot ringing Telegram call the moment a new open day appears.

Detection note (verified against the live site on 2026-07-19):
in the MONTH grid the img alt texts are swapped — closed days (X icon,
icon_disabled.svg, not clickable) carry the "Available / Qabul qilinmoqda"
alt, while bookable days (O icon, icon_circle.svg, clickable, and confirmed
to show real time slots in the day view) carry the "Not available" alt.
So we detect openness by the icon + clickable cell, never by alt text.

Notify-only: this tool never books anything or submits personal data.
"""
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from http.cookiejar import CookieJar

BASE = "https://uzembassyryouji.rsvsys.jp"
CAL_URL = BASE + "/reservations/calendar"
AJAX_URL = BASE + "/ajax/reservations/calendar"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
ARTIFACT_DIR = os.path.join(HERE, "artifacts")


def load_dotenv():
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_dotenv()
CATEGORY = os.environ.get("CATEGORY_VALUE", "12")
MONTHS_TO_SCAN = int(os.environ.get("MONTHS_TO_SCAN", "2"))
INTERVAL_MIN = float(os.environ.get("INTERVAL_MIN", "35"))
INTERVAL_MAX = float(os.environ.get("INTERVAL_MAX", "75"))
SELF_TEST = os.environ.get("SELF_TEST", "0") == "1"
# DRILL: real loop + real alerts but with a fake open date injected — for rehearsals
DRILL = os.environ.get("DRILL", "0") == "1"
# exit cleanly after N seconds (0 = run forever); used by GitHub Actions job chaining
MAX_RUNTIME_SECONDS = int(os.environ.get("MAX_RUNTIME_SECONDS", "0"))
# while a slot is open and not acknowledged, re-ring/re-message this often
ALARM_REPEAT_SECONDS = int(os.environ.get("ALARM_REPEAT_SECONDS", "65"))
ENABLE_TELEGRAM = os.environ.get("ENABLE_TELEGRAM", "1") == "1"
ENABLE_CALL = os.environ.get("ENABLE_CALL", "1") == "1"
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
CALL_USER = os.environ.get("CALLMEBOT_TG_USER", "")


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


class BlockedError(Exception):
    pass


class Session:
    """One persistent cookie session against the reservation site."""

    def __init__(self):
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def bootstrap(self):
        req = urllib.request.Request(CAL_URL, headers={"User-Agent": UA})
        with self.opener.open(req, timeout=30) as r:
            r.read()

    def csrf_token(self):
        for c in self.jar:
            if c.name == "csrfToken":
                return c.value
        return ""

    def fetch_month(self, month_date=None):
        """POST the site's own calendar AJAX. month_date 'YYYY/MM/DD' or None for current."""
        fields = {"category": CATEGORY, "_csrfToken": self.csrf_token(), "search": "exec"}
        if month_date:
            fields["date"] = month_date
            fields["disp_type"] = "month"
        req = urllib.request.Request(
            AJAX_URL,
            data=urllib.parse.urlencode(fields).encode(),
            headers={
                "User-Agent": UA,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": CAL_URL,
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
        )
        try:
            with self.opener.open(req, timeout=30) as r:
                body = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise BlockedError("HTTP %d" % e.code)
        try:
            return json.loads(body)["html"]
        except (ValueError, KeyError):
            raise BlockedError("non-JSON response (block page or expired session)")


def is_open_cell(td_html):
    """True iff a month-grid day cell is bookable: O icon in a clickable cell.

    Alt text is deliberately ignored — the live site attaches the affirmative
    'Available / Qabul qilinmoqda / Приём ведётся' alt to the disabled X icon.
    """
    return "icon_circle" in td_html and "js_change_date" in td_html


def parse_month(html, year, month):
    """Return list of (date_str, state) for every day cell; state OPEN/closed/-."""
    days = []
    for td in re.findall(r"<td\b.*?</td>", html, re.S):
        m = re.search(r'sc_cal_date">(?:<a[^>]*>)?(\d+)<', td)
        if not m:
            continue
        day = int(m.group(1))
        # trailing/leading cells of adjacent months have no icon and are harmless
        dm = re.search(r'data-date="(\d{4}/\d{2}/\d{2})"', td)
        dstr = dm.group(1) if dm else "%04d/%02d/%02d" % (year, month, day)
        if is_open_cell(td):
            state = "OPEN"
        elif "icon_disabled" in td:
            state = "closed"
        else:
            state = "-"
        days.append((dstr, state))
    return days


def month_targets():
    """[(post_date_or_None, year, month)] for this month + following ones."""
    t = date.today()
    out = [(None, t.year, t.month)]
    y, m = t.year, t.month
    for _ in range(MONTHS_TO_SCAN - 1):
        m += 1
        if m > 12:
            m, y = 1, y + 1
        out.append(("%04d/%02d/01" % (y, m), y, m))
    return out


def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")[:200]


def tg_api(method, params):
    """Call a Telegram bot API method; returns parsed JSON or None."""
    try:
        url = "https://api.telegram.org/bot%s/%s?%s" % (
            TG_TOKEN, method, urllib.parse.urlencode(params))
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        log("telegram %s FAILED: %s" % (method, e))
        return None


STOP_KEYBOARD = json.dumps({"inline_keyboard": [[{"text": "🛑 STOP alerts", "callback_data": "stop"}]]})


def notify(open_dates, test=False):
    """Send all enabled alerts. Returns True if at least one succeeded."""
    dates = ", ".join(sorted(open_dates))
    text = "VISA SLOT OPEN: %s — book NOW: %s" % (dates, CAL_URL)
    if test:
        text = "[TEST] " + text
    ok_any = False
    if ENABLE_TELEGRAM and TG_TOKEN and TG_CHAT:
        t0 = time.time()
        resp = tg_api("sendMessage", {"chat_id": TG_CHAT, "text": "🚨 " + text,
                                      "reply_markup": STOP_KEYBOARD})
        ok = bool(resp and resp.get("ok"))
        log("notify telegram: %s (%.1fs)" % ("ok" if ok else "FAILED", time.time() - t0))
        ok_any = ok_any or ok
    if ENABLE_CALL and CALL_USER:
        try:
            t0 = time.time()
            status, _ = http_get(
                "https://api.callmebot.com/start.php?"
                + urllib.parse.urlencode({"user": CALL_USER, "text": text, "lang": "en", "rpt": "2"}),
                timeout=30,
            )
            log("notify call: HTTP %d (%.1fs)" % (status, time.time() - t0))
            ok_any = ok_any or status == 200
        except Exception as e:
            log("notify call FAILED: %s" % e)
    if not ok_any:
        log("WARNING: no notification succeeded — check .env credentials")
    return ok_any


def poll_stop(offset):
    """Check the bot's incoming updates for a STOP button press or 'stop' text.

    Returns (new_offset, stop_requested). Only called while an alarm is active,
    so a concurrent DRILL run and the real monitor never fight over updates.
    """
    params = {"timeout": 0, "allowed_updates": '["message","callback_query"]'}
    if offset is not None:
        params["offset"] = offset
    resp = tg_api("getUpdates", params)
    if not resp or not resp.get("ok"):
        return offset, False
    stop = False
    for u in resp["result"]:
        offset = u["update_id"] + 1
        cb = u.get("callback_query")
        if cb and cb.get("data") == "stop":
            stop = True
            tg_api("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Alerts stopped"})
        msg = u.get("message", {})
        if "stop" in msg.get("text", "").lower():
            stop = True
    return offset, stop


def drain_updates():
    """Fast-forward past any pending updates so stale messages can't ack a new alarm."""
    offset = None
    while True:
        params = {"timeout": 0}
        if offset is not None:
            params["offset"] = offset
        resp = tg_api("getUpdates", params)
        if not resp or not resp.get("ok") or not resp["result"]:
            return offset
        offset = resp["result"][-1]["update_id"] + 1


def load_state():
    try:
        return set(json.load(open(STATE_FILE))["open_dates"])
    except Exception:
        return set()


def save_state(open_dates):
    json.dump({"open_dates": sorted(open_dates), "updated": datetime.now().isoformat()},
              open(STATE_FILE, "w"))


def check_once(session, verbose=False, save_artifacts=False):
    """One full scan. Returns set of open dates."""
    open_dates = set()
    for post_date, y, m in month_targets():
        html = session.fetch_month(post_date)
        if save_artifacts:
            os.makedirs(ARTIFACT_DIR, exist_ok=True)
            with open(os.path.join(ARTIFACT_DIR, "calendar_%04d-%02d.html" % (y, m)), "w") as f:
                f.write("<base href='%s/'>\n" % BASE + html)
        days = parse_month(html, y, m)
        for dstr, state in days:
            if state == "OPEN":
                open_dates.add(dstr)
            if verbose:
                print("  %s  %s" % (dstr, state))
        if verbose:
            sel = re.findall(r'name="(event|plan)"[^>]*value="(\d+)"', html)[:2]
            print("month %04d-%02d: %d day cells, selected %s" % (y, m, len(days), dict(sel)))
        time.sleep(random.uniform(1.0, 3.0))  # small human-like gap between the 2 month loads
    return open_dates


def main():
    once = "--once" in sys.argv
    session = Session()
    session.bootstrap()
    log("session started (category=%s, months=%d)" % (CATEGORY, MONTHS_TO_SCAN))

    if once or SELF_TEST:
        t0 = time.time()
        open_dates = check_once(session, verbose=True, save_artifacts=True)
        fetched = time.time()
        log("check done in %.1fs — open dates: %s" % (fetched - t0, sorted(open_dates) or "none"))
        if SELF_TEST:
            fake = date.today().strftime("%Y/%m/28")
            log("SELF_TEST: injecting fake open date %s and sending real alerts" % fake)
            notify(open_dates | {fake}, test=True)
            log("SELF_TEST total latency (fetch+detect+notify): %.1fs" % (time.time() - t0))
        return

    state = load_state()
    log("known open dates from last run: %s" % (sorted(state) or "none"))
    if DRILL:
        log("DRILL MODE: a fake open date will be injected — alerts are [TEST]")
    backoff = 0
    started = time.time()
    acked = False
    last_alert = 0.0
    upd_offset = None
    while True:
        if MAX_RUNTIME_SECONDS and time.time() - started > MAX_RUNTIME_SECONDS:
            log("max runtime reached — exiting for a fresh run to take over")
            return
        t0 = time.time()
        try:
            open_dates = check_once(session)
            backoff = 0
        except (BlockedError, urllib.error.URLError, OSError) as e:
            # one silent re-bootstrap for expired sessions, then real backoff
            try:
                session = Session()
                session.bootstrap()
                open_dates = check_once(session)
                backoff = 0
                log("session refreshed after: %s" % e)
            except Exception as e2:
                backoff = min(max(backoff * 2, 120), 1800)
                log("ERROR %s — backing off %ds" % (e2, backoff))
                time.sleep(backoff + random.uniform(0, 30))
                continue
        if DRILL:
            open_dates = open_dates | {date.today().strftime("%Y/%m/28")}
        new = open_dates - state
        if new:
            log("NEW OPENING(S): %s (cycle took %.1fs)" % (sorted(new), time.time() - t0))
            acked = False  # new dates always restart the alarm
            upd_offset = drain_updates()  # ignore stale button presses/messages
        if not open_dates:
            acked = False
        # alarm: repeat ring+message every ALARM_REPEAT_SECONDS until STOP is pressed
        if open_dates and not acked:
            if time.time() - last_alert >= ALARM_REPEAT_SECONDS:
                notify(open_dates, test=DRILL)
                last_alert = time.time()
            upd_offset, stop = poll_stop(upd_offset)
            if stop:
                acked = True
                log("alerts acknowledged via STOP")
                tg_api("sendMessage", {"chat_id": TG_CHAT, "text":
                       "✅ Alerts stopped for: %s. You'll be alerted again only when a NEW date opens."
                       % ", ".join(sorted(open_dates))})
        if open_dates != state:
            state = open_dates
            if not DRILL:  # a drill's fake date must never mask a real opening
                save_state(state)
        log("checked in %.1fs — open: %s%s" % (time.time() - t0, sorted(open_dates) or "none",
                                               " (acked)" if open_dates and acked else ""))
        time.sleep(random.uniform(INTERVAL_MIN, INTERVAL_MAX))


if __name__ == "__main__":
    main()
