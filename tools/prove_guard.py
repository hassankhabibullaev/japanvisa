"""Prove the wrong-calendar guard against the live site.

This reproduces the exact failure that made the previous monitor useless: ask for
the Representative calendar while sending the Applicant's plan id. The site
answers HTTP 200 with a perfectly valid calendar -- for the wrong type.

Expected result: the monitor refuses to read it, raises WrongCalendar, and (with
--alert) sends a Telegram message instead of ever saying "no slots".

  python3 tools/prove_guard.py
  python3 tools/prove_guard.py --alert
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notify
import rsvsys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--alert", action="store_true", help="really send the Telegram alert")
    args = ap.parse_args()

    print("1. Asking for category 12 / event 21 while forcing plan 19 (the Applicant's plan).")
    s = rsvsys.Site()
    s.select(12, 21, plan=19)
    print("   session now claims: category=%s event=%s plan=%s" % (s.category, s.event, s.plan))

    print("2. Fetching a month with that mismatched plan...")
    try:
        s.month(2026, 9)
    except rsvsys.WrongCalendar as e:
        print("   GUARD FIRED: %s" % e)
        if args.alert:
            n = notify.from_env()
            d = n.notice("WRONG CALENDAR - the monitor is NOT watching what you asked for.\n\n"
                         "%s\n\nIt will keep retrying, and it will not report 'no slots' "
                         "while this is happening.\n\n(This is the deliberate proof run.)" % e)
            print("   telegram: %s" % d)
            return 0 if d.ok else 1
        return 0
    except rsvsys.FetchFailed as e:
        print("   site unreachable (%s) - rerun; this proves nothing either way" % e)
        return 2

    print("   NO GUARD FIRED - the monitor would have reported 'no slots' on the wrong")
    print("   calendar. This is the failure mode this whole project exists to prevent.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
