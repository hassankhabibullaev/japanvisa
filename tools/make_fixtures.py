"""Build open-slot test pages out of the real captured pages.

Why this exists: on the day this was built, every Japanese embassy calendar on
this booking platform was fully booked, so no page showing an open slot could be
captured live. These fixtures take the genuinely captured pages and swap only the
slot cell for the open forms this software is known to produce:

  - icon_circle inside a js_change_date link          (month grid, from the site's own JS)
  - a js_check_in_stock link carrying data-stock="N"  (what the site's own filter reads)
  - a printed count, 残<i>N件                          (as rendered by embjpcol.rsvsys.jp)
  - an icon shape never seen before                   (must still be treated as open)

The open cells deliberately carry the WRONG alt text, so any future attempt to
read availability from alt text fails these tests loudly.
"""

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")

ALT_LIE = " Not available / Qabul tugadi / Приём окончен"


def open_month_cell(day, date):
    return (
        '<td > <div class="sc_cal_month_itemlist"> '
        '<div class="sc_cal_date">%d</div> '
        '<p class="c_cal_time_cell" style=""><a href="#" class="js_change_date" '
        'data-date="%s"><img src="/assets/images/user/icon_circle.svg?1604042663" '
        'alt="%s" width="24" height="24"/></a></p> '
        '</div><!-- .sc_cal_month_itemlist --> </td>' % (day, date, ALT_LIE))


def build_month(src, out, day, date):
    html = open(src, encoding="utf-8").read()
    cells = re.findall(r"<td[^>]*>.*?</td>", html, re.S)
    target = None
    for c in cells:
        if "icon_disabled" in c:
            target = c
            break
    if target is None:
        raise SystemExit("no closed cell to convert in %s" % src)
    html = html.replace(target, open_month_cell(day, date), 1)
    open(out, "w", encoding="utf-8").write(html)
    return out


DAY_VARIANTS = {
    "day_open_stock.html":
        '<td> <p class="c_cal_time_cell"><a href="#" class="js_check_in_stock" '
        'data-stock="4" data-date="%%s"><img src="/assets/images/user/icon_circle.svg?1604042663" '
        'alt="%s" width="24" height="24"/></a></p> </td>' % ALT_LIE,

    "day_open_printed_count.html":
        '<td> <p class="c_cal_time_cell"><a href="#" class="js_check_in_stock">'
        '<span>残<i>3件／ Ariza</i></span></a></p> </td>',

    "day_open_two_seats.html":
        '<td> <p class="c_cal_time_cell"><a href="#" class="js_check_in_stock" '
        'data-stock="2"><img src="/assets/images/user/icon_circle.svg?1604042663" '
        'alt="%s" width="24" height="24"/></a></p> </td>' % ALT_LIE,

    "day_open_unknown_shape.html":
        '<td> <p class="c_cal_time_cell"><a href="#">'
        '<img src="/assets/images/user/icon_triangle.svg?1604042663" '
        'alt="%s" width="24" height="24"/></a></p> </td>' % ALT_LIE,
}


def build_days(src):
    html = open(src, encoding="utf-8").read()
    m = re.search(r"<td>\s*<p class=\"c_cal_time_cell c_cal_time_cell--disabled\">.*?</td>",
                  html, re.S)
    if not m:
        raise SystemExit("no disabled slot cell found in %s" % src)
    made = []
    for name, cell in DAY_VARIANTS.items():
        out = os.path.join(FIX, name)
        open(out, "w", encoding="utf-8").write(
            html.replace(m.group(0), cell.replace("%s", "2026/08/27"), 1))
        made.append(out)
    return made


if __name__ == "__main__":
    print(build_month(os.path.join(FIX, "month_all_closed.html"),
                      os.path.join(FIX, "month_one_open.html"), 27, "2026/08/27"))
    for p in build_days(os.path.join(FIX, "day_all_full.html")):
        print(p)
