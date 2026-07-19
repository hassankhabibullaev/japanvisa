#!/usr/bin/env python3
"""Detector unit tests using day-cell HTML captured from the live site on 2026-07-19.

Key point these fixtures prove: the site's month-grid alt texts are SWAPPED.
A truly bookable day is an O icon (icon_circle) in a clickable cell — and it
carries the "Not available" alt. A closed day is an X icon (icon_disabled) —
and it carries the "Available / Qabul qilinmoqda" alt. Detection therefore
uses icons/clickability and must ignore alt text entirely.
"""
from monitor import is_open_cell, parse_month

# Real bookable day (COE category, 2026/07/22 — day view confirmed a 14:30 slot).
# Note the misleading "Not available" alt on a genuinely OPEN day.
OPEN_CELL = '''<td><div class="sc_cal_month_itemlist">
<div class="sc_cal_date"><a href="#" class="js_change_date js_change" data-date="2026/07/22" data-value="day" data-target="sel_disp_type">22</a></div>
<a href="#" class="c_cal_time_cell js_change_date js_change" data-date="2026/07/22" data-value="day" data-target="sel_disp_type"><img src="/assets/images/user/icon_circle.svg?1604042662" alt=" Not available / Qabul tugadi / Приём окончен" width="24" height="24"/></a>
</div></td>'''

# Real closed day (short-stay category, 2026/07/22 — X icon, not clickable).
# Note the misleading "Available / Qabul qilinmoqda" alt on a CLOSED day.
CLOSED_CELL = '''<td><div class="sc_cal_month_itemlist">
<div class="sc_cal_date">22</div>
<p class="c_cal_time_cell"><img src="/assets/images/user/icon_disabled.svg?1604042663" alt=" Available / Qabul qilinmoqda / Приём ведётся" width="24" height="24"/></p>
</div></td>'''

# Day with no reservation window at all (weekend/past): no icon.
EMPTY_CELL = '<td><div class="sc_cal_month_itemlist"><div class="sc_cal_date">19</div></div></td>'


def run():
    assert is_open_cell(OPEN_CELL) is True, "circle+clickable cell must be OPEN"
    assert is_open_cell(CLOSED_CELL) is False, \
        "X-icon cell must be closed even though its alt says 'Qabul qilinmoqda'"
    assert is_open_cell(EMPTY_CELL) is False, "icon-less cell must not be OPEN"

    days = parse_month(OPEN_CELL + CLOSED_CELL + EMPTY_CELL, 2026, 7)
    assert days == [("2026/07/22", "OPEN"), ("2026/07/22", "closed"), ("2026/07/19", "-")], days
    print("PASS: open cell detected as OPEN (despite 'Not available' alt)")
    print("PASS: closed X cell NOT detected as open (despite 'Available / Qabul qilinmoqda' alt)")
    print("PASS: empty cell ignored; dates parsed correctly")


if __name__ == "__main__":
    run()
