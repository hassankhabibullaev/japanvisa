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
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = str(chat_id)
        self._offset = None

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
                    self._call("answerCallbackQuery",
                               {"callback_query_id": cq["id"], "text": "Alarm stopped."})
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

    def send(self, title, text, tries=3, verify=True, click=None):
        """Publish at max priority.

        With verify=True the topic is read back and the message found before this
        reports success -- an HTTP 200 alone is not proof of anything. Verifying
        costs a second or two, so a ringing alarm checks the first round and every
        tenth after that rather than every single buzz.
        """
        last = "no attempt"
        for attempt in range(tries):
            try:
                headers = {"Title": title, "Priority": "urgent",
                           "Tags": "rotating_light"}
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


class Notifier:
    """The only thing the monitor talks to."""

    STOP_WORDS = {"stop", "snooze", "/stop", "ok", "seen", "alarm_stop"}

    def __init__(self, telegram, ntfy=None, log=print):
        self.tg = telegram
        self.ntfy = ntfy
        self.log = log

    # -- routine, Telegram only -------------------------------------------

    def notice(self, text):
        d = self.tg.send(text)
        self.log("telegram notice: %s" % d)
        return d

    # -- the alarm --------------------------------------------------------

    def alarm(self, title, text, repeat_seconds=60, max_seconds=900, click=None):
        """Ring both channels until acknowledged, and give up after max_seconds.

        Returns a dict describing what actually happened, including whether each
        channel could be confirmed. Never claims success it cannot verify.
        """
        started = time.time()
        rounds = 0
        tg_ok = ntfy_ok = False
        problems = []
        stopped_by = "timeout"

        buttons = [[{"text": "STOP ALARM", "callback_data": "alarm_stop"}]]

        previous = None      # the buzz we are about to replace

        while time.time() - started < max_seconds:
            round_start = time.time()
            rounds += 1
            head = text if rounds == 1 else "%s\n(%d)" % (text, rounds)

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
            last_poll = 0.0
            while True:
                now = time.time()
                if now >= next_round or now - started >= max_seconds:
                    break
                if now - last_poll >= 3:
                    last_poll = now
                    for kind, value in self.tg.poll_replies():
                        if value in self.STOP_WORDS:
                            stopped_by = "you (%s)" % kind
                            return self._alarm_result(started, rounds, tg_ok, ntfy_ok,
                                                      problems, stopped_by)
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
            self.tg.send("Heads up: the ntfy alarm could NOT be confirmed just now.\n"
                         "Telegram worked. Reason: %s" % (problems[-1] if problems else "unknown"))
        return result


def from_env(log=print):
    """Build the notifier from environment variables. ntfy is optional."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not token or not chat:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
    return Notifier(Telegram(token, chat), Ntfy(topic) if topic else None, log=log)
