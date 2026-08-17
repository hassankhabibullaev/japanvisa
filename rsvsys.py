"""Talking to uzembassyryouji.rsvsys.jp, and reading what it says.

Two rules live in this file and nowhere else:

  1. Switching the calendar type works ONLY if you post category+event with NO plan.
     Posting a plan that belongs to another event makes the server silently ignore
     your event and serve the Applicant calendar instead. Every response is checked
     against what we asked for; a mismatch raises WrongCalendar.

  2. Never read availability from image alt text. On the month grid the alt text is
     inverted -- closed days are labelled "Available". Availability is read from the
     icon file name and from whether the cell is clickable.
"""

import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://uzembassyryouji.rsvsys.jp"
CALENDAR_PAGE = BASE + "/reservations/calendar"
CALENDAR_AJAX = BASE + "/ajax/reservations/calendar"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


class WrongCalendar(Exception):
    """The site served a calendar we did not ask for. Never treat as 'no slots'."""


class FetchFailed(Exception):
    """Network trouble or an HTTP 500. Normal weather here; retry."""


# --------------------------------------------------------------------------
# parsing helpers
# --------------------------------------------------------------------------

def _hidden(html, name):
    m = re.search(r'<input[^>]*type="hidden"[^>]*name="%s"[^>]*value="([^"]*)"' % name, html)
    if m:
        return m.group(1)
    m = re.search(r'<input[^>]*name="%s"[^>]*type="hidden"[^>]*value="([^"]*)"' % name, html)
    return m.group(1) if m else None


def _cells(html):
    return re.findall(r"<td[^>]*>.*?</td>", html, re.S)


class Day:
    """One square on the month grid."""

    OPEN = "open"          # bookable: clickable, and not the disabled icon
    FULL = "full"          # reception happens, no seats left
    NONE = "none"          # no reception that day
    UNKNOWN = "unknown"    # a shape we do not recognise -- always escalated

    def __init__(self, date, state, note=""):
        self.date = date
        self.state = state
        self.note = note

    def __repr__(self):
        return "Day(%s, %s)" % (self.date, self.state)


class Slot:
    """One time slot on the day view."""

    def __init__(self, time, available, seats, note=""):
        self.time = time
        self.available = available
        self.seats = seats          # int, or None when the site did not say
        self.note = note

    def describe(self):
        if self.seats is None:
            return "%s (seats not stated)" % self.time
        return "%s (%d seat%s)" % (self.time, self.seats, "" if self.seats == 1 else "s")

    def __repr__(self):
        return "Slot(%s, available=%s, seats=%s)" % (self.time, self.available, self.seats)


DAY_NUMBER = re.compile(r'class="sc_cal_date[^"]*">(.*?)</div>', re.S)


def parse_month(html, year=None, mon=None):
    """Return one Day per square of the month grid.

    Read the day number by stripping tags, never by expecting a bare digit right
    after the class. On an open day this site wraps the number in a link --
    `<div class="sc_cal_date"><a ...>20</a></div>` against `...">19</div>` when
    closed -- so a digit-only pattern matches every closed day and silently skips
    every open one, which is the exact shape of "cheerfully reports no slots".

    Availability comes from the icon file name. Closed is icon_disabled; anything
    else with an icon is treated as bookable, including icons never seen before.
    """
    days = []
    for cell in _cells(html):
        m = DAY_NUMBER.search(cell)
        if not m:
            continue
        digits = re.search(r"(\d+)", re.sub(r"<[^>]+>", " ", m.group(1)))
        if not digits:
            continue
        daynum = int(digits.group(1))

        link = re.search(r'data-date="(\d{4}/\d{2}/\d{2})"', cell)
        date = link.group(1).replace("/", "-") if link else None
        if date is None and year and mon:
            date = "%04d-%02d-%02d" % (year, mon, daynum)

        icons = set(re.findall(r"(icon_[a-z_]+)\.svg", cell))
        if not icons:
            days.append(Day(date, Day.NONE))
        elif "icon_disabled" in icons:
            days.append(Day(date, Day.FULL))
        else:
            # icon_circle, or a shape we have not met. Both mean "look at it".
            note = "" if icons == {"icon_circle"} else "unfamiliar icon %s" % sorted(icons)
            days.append(Day(date, Day.OPEN, note))
    return days


def seats_in(fragment):
    """Seats remaining in a slot cell, or None if the page does not say.

    Two ways this software states capacity, both seen in the wild:
      data-stock="3"     - what the site's own "how many people" filter reads
      残<i>3件...        - the count printed for humans
    """
    m = re.search(r'data-stock="(\d+)"', fragment)
    if m:
        return int(m.group(1))
    m = re.search(r"残\s*(?:<[^>]+>\s*)?(\d+)\s*件", fragment)
    if m:
        return int(m.group(1))
    m = re.search(r"(?:残り|[Rr]emaining|[Qq]uedan)\s*[:\s]\s*(\d+)", fragment)
    if m:
        return int(m.group(1))
    return None


def parse_day(html):
    """Return the time slots on a day view, with seats remaining where stated.

    Biased towards calling a slot available. A false alarm costs a glance at the
    phone; a missed slot costs the appointment.
    """
    slots = []
    body = re.search(r'<tbody[^>]*c_cal_time_tbody.*?</tbody>', html, re.S)
    scope = body.group(0) if body else html

    for row in re.findall(r"<tr[^>]*>.*?</tr>", scope, re.S):
        tm = re.search(r"<th[^>]*>\s*(\d{1,2}:\d{2})\s*</th>", row)
        if not tm:
            continue
        when = tm.group(1)
        seats = seats_in(row)

        # Explicitly closed: the disabled class, the disabled icon, or a stated zero.
        if ("c_cal_time_cell--disabled" in row or "icon_disabled" in row or seats == 0):
            slots.append(Slot(when, False, 0))
            continue

        # Anything else that is a slot cell at all counts as available. That
        # includes shapes we have not seen: an unknown icon, or a link with no
        # stock attribute. Seats stay None and the alert says so out loud.
        is_cell = "c_cal_time_cell" in row or "js_calendar_item" in row
        clickable = ("js_check_in_stock" in row or "js_reserve" in row
                     or "js_change" in row or "<a " in row)
        has_icon = "icon_" in row

        if seats is not None or clickable or has_icon or is_cell:
            note = "" if seats is not None else "site did not state a seat count"
            slots.append(Slot(when, True, seats, note))
    return slots


def max_group_size(html):
    """Largest number of applicants the day view lets you put in one booking."""
    sel = re.search(r'<select[^>]*name="stock".*?</select>', html, re.S)
    if not sel:
        return None
    vals = [int(v) for v in re.findall(r'value="(\d+)"', sel.group(0))]
    return max(vals) if vals else None


# --------------------------------------------------------------------------
# the session
# --------------------------------------------------------------------------

class Site:
    def __init__(self, timeout=20):
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = None
        self.category = None
        self.event = None
        self.plan = None

    # -- raw transport -----------------------------------------------------
    #
    # This site answers HTTP 500 for roughly one request in thirteen, worst
    # during the hours slots are released. That is weather, not an outage, so a
    # single request retries a few times over a handful of seconds rather than
    # letting a routine 500 become a failed check.

    RETRY_PAUSES = (1, 2, 2)      # ~5 seconds of trying, then give up and report

    def _attempt(self, make_request, describe):
        last = None
        for i in range(len(self.RETRY_PAUSES) + 1):
            try:
                with self.opener.open(make_request(), timeout=self.timeout) as r:
                    return r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                last = "HTTP %s%s" % (e.code, describe)
            except Exception as e:
                last = "%s%s" % (type(e).__name__, describe)
            if i < len(self.RETRY_PAUSES):
                time.sleep(self.RETRY_PAUSES[i])
        raise FetchFailed(last)

    def _get(self, url):
        return self._attempt(
            lambda: urllib.request.Request(url, headers={"User-Agent": UA}), " on GET")

    def _post(self, fields):
        body = urllib.parse.urlencode(fields).encode()

        def build():
            return urllib.request.Request(CALENDAR_AJAX, data=body, headers={
                "User-Agent": UA,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": CALENDAR_PAGE,
                "Accept": "application/json, text/javascript, */*; q=0.01",
            })

        raw = self._attempt(build, "")
        try:
            return json.loads(raw).get("html", "")
        except ValueError:
            raise FetchFailed("response was not JSON")

    # -- session ----------------------------------------------------------

    def open_session(self):
        html = self._get(CALENDAR_PAGE)
        m = re.search(r'name="_csrfToken"[^>]*value="([^"]+)"', html)
        if not m:
            raise FetchFailed("no CSRF token on the calendar page")
        self.csrf = m.group(1)
        self.category = self.event = self.plan = None
        return html

    def _check(self, html, want_category, want_event, want_plan=None,
               want_month=None, want_date=None, want_disp=None):
        got_c, got_e = _hidden(html, "category"), _hidden(html, "event")
        got_p, got_d = _hidden(html, "plan"), _hidden(html, "date")

        if got_c is None or got_e is None:
            raise WrongCalendar("response carried no category/event fields at all")
        if str(got_c) != str(want_category) or str(got_e) != str(want_event):
            raise WrongCalendar(
                "asked for category=%s event=%s but the site served category=%s event=%s"
                % (want_category, want_event, got_c, got_e))
        if want_plan is not None and str(got_p) != str(want_plan):
            raise WrongCalendar(
                "asked for plan=%s but the site served plan=%s (category=%s event=%s)"
                % (want_plan, got_p, got_c, got_e))
        if want_disp is not None:
            got_disp = _hidden(html, "disp_type")
            if got_disp != want_disp:
                raise WrongCalendar("asked for %s view, got %s" % (want_disp, got_disp))
        if want_date is not None and (got_d or "").replace("/", "-") != want_date:
            raise WrongCalendar("asked for date %s but the site served %s" % (want_date, got_d))
        if want_month is not None and (got_d or "")[:7].replace("/", "-") != want_month:
            raise WrongCalendar("asked for month %s but the site served %s" % (want_month, got_d))
        return html

    def select(self, category, event, plan=None):
        """Switch the calendar. Sends NO plan -- that is what makes it work."""
        if self.csrf is None:
            self.open_session()
        html = self._post([
            ("category", str(category)),
            ("event", str(event)),
            ("_csrfToken", self.csrf),
            ("search", "exec"),
        ])
        self._check(html, category, event)
        self.category, self.event = str(category), str(event)
        self.plan = str(plan) if plan is not None else _hidden(html, "plan")
        if not self.plan:
            raise WrongCalendar("site returned no plan id for category=%s event=%s"
                                % (category, event))
        return html

    def _navigate(self, date, disp_type):
        if self.plan is None:
            raise FetchFailed("select() must run before fetching a calendar")
        return self._post([
            ("category", self.category),
            ("event", self.event),
            ("plan", self.plan),
            ("date", date),
            ("disp_type", disp_type),
            ("_csrfToken", self.csrf),
            ("search", "exec"),
        ])

    def month(self, year, mon):
        """Month grid. Verified to be the calendar we asked for."""
        date = "%04d/%02d/01" % (year, mon)
        html = self._navigate(date, "month")
        self._check(html, self.category, self.event, self.plan,
                    want_month="%04d-%02d" % (year, mon), want_disp="month")
        self.last_month = (year, mon)
        return html

    def day(self, date):
        """Day view for YYYY-MM-DD, with the real time slots and seat counts."""
        html = self._navigate(date.replace("-", "/"), "day")
        self._check(html, self.category, self.event, self.plan,
                    want_date=date, want_disp="day")
        return html
