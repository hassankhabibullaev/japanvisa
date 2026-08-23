"""Getting hold of a human.

Two channels, used for different things:

  Telegram  -- everything: alarms, warnings, crashes, daily summaries.
  ntfy.sh   -- alarms only, at max priority, so routine status never wakes anyone.

Nothing here trusts an HTTP 200. Telegram is confirmed by reading back the
message id and chat id the API returns; ntfy is confirmed by re-reading the
topic and finding the message we just published. A send that cannot be
confirmed is reported as a failure, not logged as a success.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TG_API = "https://api.telegram.org/bot%s/%s"
NTFY = "https://ntfy.sh"
UA = "japanvisa-monitor"


def _req(url, data=None, headers=None, timeout=20):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


class Delivery:
    def __init__(self, ok, detail, ident=None):
        self.ok = ok
        self.detail = detail
        self.ident = ident

    def __repr__(self):
        return "Delivery(ok=%s, %s)" % (self.ok, self.detail)


class Telegram:
    def __init__(self, token, chat_id, offset=None):
        self.token = token
        self.chat_id = str(chat_id)
        self._offset = offset

    def _call(self, method, params, timeout=20):
        url = TG_API % (self.token, method)
        body = urllib.parse.urlencode(params).encode()
        status, raw = _req(url, data=body, timeout=timeout)
        return status, json.loads(raw)

    def send(self, text, buttons=None, tries=3):
        """Send, and confirm the message really landed in the right chat."""
        params = {"chat_id": self.chat_id, "text": text,
                  "disable_web_page_preview": "true"}
        if buttons:
            params["reply_markup"] = json.dumps({"inline_keyboard": buttons})

        last = "no attempt"
        for attempt in range(tries):
            try:
                status, body = self._call("sendMessage", params)
            except urllib.error.HTTPError as e:
                try:
                    body = json.loads(e.read().decode("utf-8", "replace"))
                    last = "HTTP %s: %s" % (e.code, body.get("description"))
                except Exception:
                    last = "HTTP %s" % e.code
                time.sleep(1 + attempt)
                continue
            except Exception as e:
                last = type(e).__name__
                time.sleep(1 + attempt)
                continue

            if not body.get("ok"):
                last = "API said not ok: %s" % body.get("description")
                time.sleep(1 + attempt)
                continue

            result = body.get("result") or {}
            mid = result.get("message_id")
            landed = str((result.get("chat") or {}).get("id"))
            if not mid:
                last = "no message_id came back"
                continue
            if landed != self.chat_id:
                return Delivery(False, "message landed in chat %s, not %s"
                                % (landed, self.chat_id))
            return Delivery(True, "message_id %s" % mid, mid)

        return Delivery(False, last)

    def register_commands(self, commands):
        """Put the commands in Telegram's own menu. Best effort; cosmetic only."""
        try:
            self._call("setMyCommands", {"commands": json.dumps(
                [{"command": c, "description": d} for c, d in commands])})
            return True
        except Exception:
            return False

    def delete(self, message_id):
        """Best effort. A message that will not delete is left alone, never fatal."""
        if not message_id:
            return False
        try:
            _, body = self._call("deleteMessage",
                                 {"chat_id": self.chat_id, "message_id": message_id},
                                 timeout=10)
            return bool(body.get("ok"))
        except Exception:
            return False

    @property
    def offset(self):
        """How far through the update queue we have read.

        Saved between runs. Without it a restart either replays button presses
        from hours ago or, if they are simply discarded, loses a press made while
        the runner was restarting -- which is exactly how a switch to COE went
        missing.
        """
        return self._offset

    def poll_replies(self, timeout=2):
        """Recent button presses and messages, for the alarm snooze."""
        out = []
        params = {"timeout": 0, "allowed_updates": json.dumps(["message", "callback_query"])}
        if self._offset is not None:
            params["offset"] = self._offset
        try:
            _, body = self._call("getUpdates", params, timeout=timeout + 5)
        except Exception:
            return out
        if not body.get("ok"):
            return out
        for upd in body.get("result", []):
            self._offset = upd["update_id"] + 1
            cq = upd.get("callback_query")
            if cq:
                out.append(("callback", (cq.get("data") or "").strip().lower()))
                try:
                    self._call("answerCallbackQuery", {"callback_query_id": cq["id"]})
                except Exception:
                    pass
                continue
            msg = upd.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) == self.chat_id:
                out.append(("text", (msg.get("text") or "").strip().lower()))
        return out


class Ntfy:
    def __init__(self, topic):
        self.topic = topic

    def send(self, title, text, tries=3, verify=True, click=None,
             priority="urgent", tags="rotating_light"):
        """Publish at max priority.

        With verify=True the topic is read back and the message found before this
        reports success -- an HTTP 200 alone is not proof of anything. Verifying
        costs a second or two, so a ringing alarm checks the first round and every
        tenth after that rather than every single buzz.
        """
        last = "no attempt"
        for attempt in range(tries):
            try:
                headers = {"Title": title, "Priority": priority, "Tags": tags}
                if click:
                    headers["Click"] = click     # tapping the buzz opens the calendar
                status, raw = _req("%s/%s" % (NTFY, self.topic),
                                   data=text.encode("utf-8"), headers=headers)
                ident = json.loads(raw).get("id")
            except Exception as e:
                last = type(e).__name__
                time.sleep(1 + attempt)
                continue

            if not ident:
                last = "publish returned no message id"
                time.sleep(1 + attempt)
                continue
            if not verify:
                return Delivery(True, "id %s, not re-checked this round" % ident, ident)
            if self._readback(ident):
                return Delivery(True, "id %s, confirmed on server" % ident, ident)
            last = "id %s published but not found when reading the topic back" % ident
            time.sleep(1 + attempt)
        return Delivery(False, last)

    def _readback(self, ident, window="1m"):
        """Ask ntfy for the topic's recent messages and look for ours."""
        url = "%s/%s/json?poll=1&since=%s" % (NTFY, self.topic, window)
        for _ in range(3):
            try:
                _, raw = _req(url, timeout=15)
            except Exception:
                time.sleep(1)
                continue
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    if json.loads(line).get("id") == ident:
                        return True
                except ValueError:
                    pass
            time.sleep(1)
        return False


def _summarise(body, width=70):
    """The first couple of meaningful lines, for a one-line log entry."""
    lines = [l.strip() for l in body.strip().splitlines() if l.strip()]
    lines = [l for l in lines if not l.startswith("http")]
    return "  ".join(lines[:2])[:width]


class Notifier:
    """The only thing the monitor talks to.

    Every message carries a header saying what kind of message it is, so an alarm,
    a warning and a routine status update never look alike at a glance.
    """

    STOP_WORDS = {"stop", "snooze", "/stop", "ok", "seen", "alarm_stop"}

    # Exactly three kinds of message get sent on the monitor's own initiative.
    ALARM = "\U0001F514"      # bell     - a slot is open, this one rings
    REPORT = "\U0001F4CA"     # chart    - the once-a-day report
    ATTENTION = "⚠"      # warning  - something needs a human, sent at once

    RULE = "─" * 12           # short enough not to wrap on a phone

    def __init__(self, telegram, ntfy=None, log=print):
        self.tg = telegram
        self.ntfy = ntfy
        self.log = log
        self._last_sent = {}          # kind -> when, so a stuck fault cannot spam
        self._alarms = {}             # key -> {thread, stop}
        self._finished = []
        self._alarm_lock = threading.Lock()
        # Every outbound message is written down here with the second it was
        # sent. Alarm buzzes delete themselves as they go, so without this there
        # is no way afterwards to tell which message came first.
        self._journal = []

    def note(self, kind, what, detail=""):
        """Write one line into the journal, with the moment it happened."""
        with self._alarm_lock:
            self._journal.append({"t": time.time(), "kind": kind,
                                  "what": what, "detail": detail})

    def take_journal(self):
        with self._alarm_lock:
            out, self._journal = self._journal, []
        return out

    def _typed(self, icon, kind, body, buttons=None, journal_as=None):
        text = "%s %s\n%s\n%s" % (icon, kind, self.RULE, body.strip())
        d = self.tg.send(text, buttons=buttons)
        self.log("telegram %s: %s" % (kind.lower(), d))
        if journal_as:
            self.note(journal_as, _summarise(body), "" if d.ok else "SEND FAILED")
        return d

    def report(self, body, title="DAILY REPORT"):
        """The one scheduled message: what happened today and whether it is well."""
        return self._typed(self.REPORT, title, body, journal_as="REPORT")

    def attention(self, headline, body, throttle_key=None, throttle_seconds=1800):
        """Something a human should know about, now.

        A fault that persists would otherwise send one of these every polling
        cycle, so the same kind repeats at most every half hour. It is always
        recorded in the day's report regardless.
        """
        if throttle_key:
            last = self._last_sent.get(throttle_key, 0)
            if time.time() - last < throttle_seconds:
                self.log("attention '%s' held back (sent %ds ago)"
                         % (throttle_key, int(time.time() - last)))
                return Delivery(True, "throttled")
            self._last_sent[throttle_key] = time.time()
        return self._typed(self.ATTENTION, "NEEDS ATTENTION",
                           "%s\n\n%s" % (headline, body), journal_as="PROBLEM")

    def changed(self, body, throttle_key="changed", throttle_seconds=20):
        """A release happened but the seats are already gone.

        Telegram only and no ringing: it is news worth having in seconds, but
        being woken for something unbookable would teach you to ignore the bell.
        """
        last = self._last_sent.get(throttle_key, 0)
        if time.time() - last < throttle_seconds:
            return Delivery(True, "throttled")
        self._last_sent[throttle_key] = time.time()
        return self._typed("\U0001F440", "CALENDAR CHANGED", body,
                           journal_as="CHANGED")

    def menu(self, body, buttons):
        """Only ever shown because you asked for it."""
        return self._typed("⚙", "WATCHING", body, buttons=buttons)

    # -- the alarm --------------------------------------------------------

    # -- ringing without going deaf ---------------------------------------
    #
    # An alarm used to run inside the monitoring loop, so for as long as it rang
    # -- up to fifteen minutes -- nothing was being checked. The one moment you
    # most want to keep watching is the moment a slot just opened, because the
    # other calendar may be about to open too. Alarms now ring on their own
    # threads and the loop carries on.

    def ring(self, key, title, text, repeat_seconds, max_seconds, click=None,
             subject=""):
        """Start an alarm for `key` in the background. No-op if already ringing."""
        with self._alarm_lock:
            live = self._alarms.get(key)
            if live and live["thread"].is_alive():
                self.log("alarm '%s' already ringing" % key)
                self._journal.append({"t": time.time(), "kind": "ALARM REPEAT",
                                      "what": subject or key,
                                      "detail": "already ringing since %s"
                                                % time.strftime("%H:%M:%S", time.gmtime(
                                                    live["started"] + 5 * 3600))})
                return False
            stop = threading.Event()
            t = threading.Thread(
                target=self._ring_worker,
                args=(key, stop, title, text, repeat_seconds, max_seconds, click),
                daemon=True)
            self._alarms[key] = {"thread": t, "stop": stop, "started": time.time(),
                                 "subject": subject or key}
            self._journal.append({"t": time.time(), "kind": "ALARM START",
                                  "what": subject or key, "detail": ""})
        t.start()
        return True

    def _ring_worker(self, key, stop, title, text, repeat_seconds, max_seconds, click):
        try:
            result = self.alarm(title, text, repeat_seconds, max_seconds, click,
                                stop_event=stop, stop_data="stop:" + key)
        except Exception as e:                       # noqa: BLE001 - reported below
            result = {"rounds": 0, "seconds": 0, "telegram_confirmed": False,
                      "ntfy_confirmed": False, "problems": [str(e)],
                      "stopped_by": "error"}
        result["key"] = key
        with self._alarm_lock:
            # "you" is the default for a stop event; the real reason is recorded
            # by whoever set it, so a slot vanishing is not logged as a person.
            if result["stopped_by"] == "you":
                result["stopped_by"] = self._alarms.get(key, {}).get("reason", "you")
            self._finished.append(result)
            self._journal.append({
                "t": time.time(), "kind": "ALARM STOP",
                "what": self._alarms.get(key, {}).get("subject", key),
                "detail": "%d rings, ended by %s" % (result["rounds"],
                                                     result["stopped_by"])})

    def stop_ringing(self, key=None, reason="you"):
        """Silence one alarm, or all of them when key is None."""
        stopped = []
        with self._alarm_lock:
            for k, a in self._alarms.items():
                if (key is None or k == key) and a["thread"].is_alive():
                    a["stop"].set()
                    a["reason"] = reason
                    stopped.append(k)
        return stopped

    def ringing(self):
        with self._alarm_lock:
            return [k for k, a in self._alarms.items() if a["thread"].is_alive()]

    def take_finished(self):
        """Hand back results of alarms that have ended, so the loop can log them."""
        with self._alarm_lock:
            out, self._finished = self._finished, []
        return out

    def alarm(self, title, text, repeat_seconds=60, max_seconds=900, click=None,
              stop_event=None, stop_data="alarm_stop"):
        """Ring both channels until acknowledged, and give up after max_seconds.

        Returns a dict describing what actually happened, including whether each
        channel could be confirmed. Never claims success it cannot verify.
        """
        started = time.time()
        rounds = 0
        tg_ok = ntfy_ok = False
        problems = []
        stopped_by = "timeout"

        buttons = [[{"text": "STOP ALARM", "callback_data": stop_data}]]

        previous = None      # the buzz we are about to replace

        while time.time() - started < max_seconds:
            round_start = time.time()
            rounds += 1
            body = "%s %s\n%s\n%s" % (self.ALARM, "SLOT AVAILABLE", self.RULE, text)
            head = body if rounds == 1 else "%s\n\n(ringing - %d)" % (body, rounds)

            d = self.tg.send(head, buttons=buttons, tries=1 if rounds > 1 else 3)
            tg_ok = tg_ok or d.ok
            if not d.ok:
                problems.append("telegram round %d: %s" % (rounds, d.detail))
            self.log("alarm round %d telegram: %s" % (rounds, d))

            # Send first, then remove the one before it. This way a failed send
            # never leaves the chat with no alert in it, and ninety buzzes leave
            # exactly one message behind.
            if d.ok:
                if previous:
                    self.tg.delete(previous)
                previous = d.ident

            if self.ntfy:
                n = self.ntfy.send(title, text, tries=1 if rounds > 1 else 3,
                                   verify=(rounds == 1 or rounds % 10 == 0),
                                   click=click)
                ntfy_ok = ntfy_ok or n.ok
                if not n.ok:
                    problems.append("ntfy round %d: %s" % (rounds, n.detail))
                self.log("alarm round %d ntfy: %s" % (rounds, n))

            # Wait until the next round is due, watching for STOP as we go. This
            # counts wall-clock from the start of the round, so sending time comes
            # out of the interval instead of stretching it.
            next_round = round_start + repeat_seconds
            while True:
                now = time.time()
                if now >= next_round or now - started >= max_seconds:
                    break
                if stop_event is not None and stop_event.wait(0.3):
                    return self._alarm_result(started, rounds, tg_ok, ntfy_ok,
                                              problems, "you")
                if stop_event is None:
                    for kind, value in self.tg.poll_replies():
                        if value in self.STOP_WORDS:
                            return self._alarm_result(started, rounds, tg_ok, ntfy_ok,
                                                      problems, "you (%s)" % kind)
                    time.sleep(0.3)

        return self._alarm_result(started, rounds, tg_ok, ntfy_ok, problems, stopped_by)

    def _alarm_result(self, started, rounds, tg_ok, ntfy_ok, problems, stopped_by):
        result = {
            "rounds": rounds,
            "seconds": int(time.time() - started),
            "telegram_confirmed": tg_ok,
            "ntfy_confirmed": ntfy_ok,
            "problems": problems,
            "stopped_by": stopped_by,
        }
        self.log("alarm finished: %s" % result)

        # If a channel never confirmed, say so on the channel that did.
        if not ntfy_ok and self.ntfy and tg_ok:
            self.attention("Your phone alarm (ntfy) is not working.",
                           "Telegram worked, which is why you can read this.\n\n%s"
                           % (problems[-1] if problems else "reason unknown"),
                           throttle_key="ntfy-dead")
        return result


def from_env(log=print):
    """Build the notifier from environment variables. ntfy is optional."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not token or not chat:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
    return Notifier(Telegram(token, chat), Ntfy(topic) if topic else None, log=log)
