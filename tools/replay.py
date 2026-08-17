"""Replay saved calendar pages through the real detection and alerting path.

This does not re-implement anything. It calls the same parse_month / parse_day /
describe / alarm code the live monitor calls, so a pass here means the live path
works on that page.

  python3 tools/replay.py tools/fixtures/month_open.html tools/fixtures/day_open.html
  python3 tools/replay.py --alert ...   # actually rings both channels
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import monitor
import notify
import rsvsys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("month_html")
    ap.add_argument("day_html", nargs="?")
    ap.add_argument("--alert", action="store_true", help="really ring both channels")
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()

    cfg = monitor.load_config(args.config)

    month = open(args.month_html, encoding="utf-8").read()
    days = rsvsys.parse_month(month)
    open_days = [d for d in days if d.state == rsvsys.Day.OPEN]
    unknown = [d for d in days if d.state == rsvsys.Day.UNKNOWN]

    print("--- month page: %s" % args.month_html)
    print("    squares read : %d" % len(days))
    print("    open         : %s" % ([d.date for d in open_days] or "none"))
    print("    full         : %d" % len([d for d in days if d.state == rsvsys.Day.FULL]))
    print("    no reception : %d" % len([d for d in days if d.state == rsvsys.Day.NONE]))
    print("    unrecognised : %s" % ([d.note for d in unknown] or "none"))

    if not args.day_html:
        return 0 if not unknown else 1

    day = open(args.day_html, encoding="utf-8").read()
    slots = rsvsys.parse_day(day)
    free = [s for s in slots if s.available]
    print("\n--- day page: %s" % args.day_html)
    print("    max group size the site allows: %s" % rsvsys.max_group_size(day))
    for s in slots:
        print("    %-6s %s" % (s.time, s.describe() if s.available else "full"))

    if not free:
        print("\nno bookable slot on this day page -> nothing to alert, correctly")
        return 0

    date = open_days[0].date if open_days else "(from day page)"
    opening = {"date": date, "slots": free, "max_group": rsvsys.max_group_size(day)}
    tgt = cfg["_targets"][0]
    text = monitor.describe([opening], cfg, tgt)
    print("\n--- alert this would send ---")
    print(text)
    print("--- worth waking you for groups of %s? %s ---"
          % (cfg["groups"], monitor.useful(opening, cfg["groups"])))

    if args.alert:
        n = notify.from_env()
        # Identical path and identical text, with one banner line so a drill is
        # never mistaken for a real slot.
        result = n.alarm("Visa slot open (DRILL)",
                         "*** DRILL - replayed page, not a real slot ***\n\n" + text,
                         cfg["alarm_repeat_seconds"], min(180, cfg["alarm_max_seconds"]))
        print("\nalarm result: %s" % result)
        return 0 if result["telegram_confirmed"] else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
