#!/usr/bin/env python3
"""Companion Telegram bot for telegram-remover: alerts + status commands.

    python bot.py                 # long-polls until Ctrl+C

Setup (once):

1. Create a bot with @BotFather, put the token in ``TELEGRAM_BOT_TOKEN``.
2. Send that bot any message in Telegram and watch this console — it prints
   your chat id. Put it in ``TELEGRAM_CHAT_ID`` (comma-separated for several
   admins) and restart.

The bot shares its state with the CLI guard and the web API through the SQL
database (``DATABASE_URL``, or the default ``telegram_remover.db`` next to the
CLI), so from your phone you can ask it what the guard is doing and pause it:

    /help     — this list
    /status   — guard state, circuit breaker, database stats
    /recent   — the last guard decisions  (/recent 10 for more)
    /runs     — the last cleanup runs
    /pause    — hold ALL guard actions until /resume  (kill switch)
    /resume   — let the guard act again
    /purge    — delete records older than the retention window now
    /db       — database statistics

Alerts (guard actions, cleanup summaries) arrive in the same chat straight
from the CLI / web app via ``notify.py``. Commands only work in the chats
listed in ``TELEGRAM_CHAT_ID`` — everybody else is ignored.
"""

import os
import sys
import time
import urllib.error
import urllib.request
import json

import db
import notify

POLL_TIMEOUT = 50          # seconds for long polling
MAX_RECENT = 10
FALSEY = ("0", "false", "no", "off", "none")

HELP = (
    "telegram-remover bot — status commands\n"
    "/status — guard state, circuit breaker, database stats\n"
    "/recent [n] — the last n guard decisions (default 5, max %d)\n"
    "/runs [n] — the last n cleanup runs\n"
    "/pause — hold ALL guard actions until /resume (kill switch)\n"
    "/resume — let the guard act again\n"
    "/purge — delete records older than the retention window now\n"
    "/db — database statistics\n"
    "/help — this list"
) % MAX_RECENT


class BotError(Exception):
    """Telegram Bot API said no."""


def api_call(token, method, params=None, timeout=POLL_TIMEOUT):
    """One Bot API call. Returns the ``result`` field or raises BotError."""
    try:
        url = f"{notify.api_base()}/bot{token}/{method}"
    except notify.BotAPIError as e:
        raise BotError(f"{method}: {e}") from e
    request = urllib.request.Request(
        url, data=json.dumps(params or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise BotError(f"{method}: {e}") from e
    if not body.get("ok"):
        raise BotError(f"{method}: {body.get('description') or body}")
    return body.get("result")


def command_of(message):
    """'/recent@mybot 10' -> ('recent', '10'); non-commands -> (None, '')."""
    text = str((message or {}).get("text") or "").strip()
    if not text.startswith("/"):
        return None, ""
    head, _, rest = text[1:].partition(" ")
    name = head.split("@", 1)[0].lower()
    return name, rest.strip()


class CompanionBot:
    """Long-polling bot: answers status commands in the allowed chats."""

    def __init__(self, token, chat_ids, store=None, poll_timeout=POLL_TIMEOUT,
                 log=print):
        self.token = token
        self.allowed = {str(c).strip() for c in chat_ids if str(c).strip()}
        self.store = store
        self.poll_timeout = poll_timeout
        self.log = log
        self.notifier = notify.Notifier.from_env()

    # -- plumbing ---------------------------------------------------------

    def send(self, chat_id, text):
        ok = notify.send_message(self.token, chat_id, text)
        if not ok:
            self.log(f"  could not send to chat {chat_id}")
        return ok

    def handle_message(self, message):
        """Answer one message. Returns the reply text (or None to stay quiet)."""
        chat_id = str(((message or {}).get("chat") or {}).get("id") or "")
        name, rest = command_of(message)
        if not chat_id or name is None:
            return None
        if self.allowed and chat_id not in self.allowed:
            self.log(f"  ignoring message from unauthorized chat {chat_id}")
            return None
        if not self.allowed:
            # Learn mode: tell people their chat id so they can lock the bot down.
            self.log(f"  (set TELEGRAM_CHAT_ID={chat_id} to authorize this chat)")
            return (f"This bot is not locked down yet.\n"
                    f"Add this to your .env and restart:\n"
                    f"TELEGRAM_CHAT_ID={chat_id}")
        handler = getattr(self, f"cmd_{name}", None)
        if handler is None:
            return HELP
        try:
            return handler(rest) or "Done."
        except Exception as e:  # noqa: BLE001 — a bad command never kills the poll
            self.log(f"  command /{name} failed: {type(e).__name__}: {e}")
            return f"error: {type(e).__name__}: {e}"

    # -- commands ---------------------------------------------------------

    def cmd_start(self, rest):
        return HELP

    def cmd_help(self, rest):
        return HELP

    def cmd_status(self, rest):
        store = self.store
        lines = []
        if store is None:
            lines.append("SQL store: DISABLED (set DATABASE_URL)")
        else:
            stats = store.stats()
            lines.append(f"SQL store: {stats['url']} — "
                         f"{stats['events']} event(s), {stats['runs']} run(s), "
                         f"kept {stats['retention_days']} days")
        if store is not None:
            paused = bool(store.kv_get("guard:paused"))
            lines.append("Guard pause: " + ("YES — /resume to continue" if paused else "no"))
            status = store.kv_get("guard:status") or {}
            if status:
                mode = status.get("mode") or "?"
                chats = ", ".join(status.get("chats") or []) or "?"
                lines.append(f"Guard: {mode} on {chats} (since {status.get('started') or '?'})")
                if status.get("last_record"):
                    lines.append(f"Last decision: {status['last_record']}")
                if status.get("summary"):
                    s = status["summary"]
                    lines.append("This session: " + ", ".join(
                        f"{k} {v}" for k, v in s.items() if v))
            else:
                lines.append("Guard: no session recorded "
                             "(start one with: python telegram_remover.py guard --chat ...)")
            cursors = {k: v for k, v in store.kv_items("guard:cursor").items()}
            for key, value in cursors.items():
                lines.append(f"Cursor {key}: {value}")
        return "\n".join(lines)

    def cmd_recent(self, rest):
        if self.store is None:
            return "SQL store is disabled — set DATABASE_URL."
        limit = _parse_n(rest, 5)
        events = self.store.list_events(limit=limit)
        if not events:
            return "No guard events stored yet."
        return "\n".join(format_event_line(e) for e in events)

    def cmd_runs(self, rest):
        if self.store is None:
            return "SQL store is disabled — set DATABASE_URL."
        limit = _parse_n(rest, 5)
        runs = self.store.list_runs(limit=limit)
        if not runs:
            return "No cleanup runs stored yet."
        return "\n".join(format_run_line(r) for r in runs)

    def cmd_pause(self, rest):
        if self.store is None:
            return "SQL store is disabled — set DATABASE_URL."
        self.store.kv_set("guard:paused", True)
        self.store.kv_set("guard:paused_at", time.time())
        return ("⏸ Guard PAUSED. Every running guard holds all actions "
                "(nothing deleted, nobody removed) until /resume.")

    def cmd_resume(self, rest):
        if self.store is None:
            return "SQL store is disabled — set DATABASE_URL."
        self.store.kv_set("guard:paused", False)
        self.store.kv_set("guard:resumed_at", time.time())
        return "▶️ Guard resumed. Live guards may act again."

    def cmd_purge(self, rest):
        if self.store is None:
            return "SQL store is disabled — set DATABASE_URL."
        days = None
        if rest:
            try:
                days = max(1, int(rest.split()[0]))
            except ValueError:
                return "usage: /purge [days]"
        res = self.store.purge_old(days=days)
        return (f"🧹 Purged {res['events']} event(s) and {res['runs']} run(s) "
                f"older than {res['days']} day(s) (cutoff {res['cutoff']}).")

    def cmd_db(self, rest):
        if self.store is None:
            return "SQL store is disabled — set DATABASE_URL."
        stats = self.store.stats()
        return "\n".join([
            f"Database: {stats['url']}",
            f"events: {stats['events']} (oldest {stats['oldest_event'] or '—'})",
            f"runs:   {stats['runs']}",
            f"kv:     {stats['kv']} entries",
            f"retention: {stats['retention_days']} days (auto-purge every month)",
        ])

    # -- the loop ---------------------------------------------------------

    def run_forever(self):
        self.log("bot: polling for updates — press Ctrl+C to stop")
        offset = 0
        failures = 0
        while True:
            try:
                updates = api_call(self.token, "getUpdates", {
                    "timeout": self.poll_timeout,
                    "offset": offset,
                    "allowed_updates": ["message"],
                })
                failures = 0
            except BotError as e:
                failures += 1
                wait = min(60, 2 ** min(failures, 6))
                self.log(f"  poll failed: {e} — retrying in {wait}s")
                time.sleep(wait)
                continue
            for update in updates or []:
                offset = int(update.get("update_id") or offset) + 1
                message = update.get("message") or {}
                reply = self.handle_message(message)
                if reply:
                    chat_id = (message.get("chat") or {}).get("id")
                    self.send(chat_id, reply)


def _parse_n(rest, default):
    try:
        return max(1, min(int((rest or "").split()[0]), MAX_RECENT))
    except (IndexError, ValueError):
        return min(default, MAX_RECENT)


def format_event_line(e):
    who = e.get("user_label") or (f"id {e['user_id']}" if e.get("user_id") else "—")
    chat = e.get("chat_label") or e.get("chat_key") or "?"
    status = e.get("status") or "?"
    detail = e.get("reason") or ""
    payload = e.get("payload") or {}
    if not detail:
        results = [str(v) for v in (payload.get("results") or {}).values() if v]
        detail = "; ".join(results)
    line = f"[{e.get('time') or '?'}] {chat}: {who} — {status}"
    if detail:
        line += f" ({detail})"
    return line


def format_run_line(r):
    line = f"[{r.get('time') or '?'}] {r.get('action')} — {r.get('status') or '?'}"
    if r.get("chat"):
        line += f" in {r['chat']}"
    details = r.get("details") or {}
    if details:
        line += " (" + ", ".join(f"{k} {v}" for k, v in details.items()
                                 if not isinstance(v, (dict, list))) + ")"
    return line


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        sys.exit("ERROR: set TELEGRAM_BOT_TOKEN in .env (ask @BotFather).")
    chat_ids = [c.strip() for c in
                (os.environ.get("TELEGRAM_CHAT_ID") or "").split(",") if c.strip()]
    if not chat_ids:
        print("WARNING: TELEGRAM_CHAT_ID is not set — the bot will tell every "
              "chat its id so you can add it to .env.")

    store = db.get_store(default=True)
    if store is None:
        print("NOTE: SQL storage is off (SQLAlchemy not installed) — "
              "/status and /recent will be limited.")
    else:
        print(f"SQL store: {db.redact_url(store.url)} "
              f"(records kept {store.retention_days} days)")

    bot = CompanionBot(token, chat_ids, store=store)
    if bot.notifier.enabled:
        print(f"Alerts -> chat {bot.notifier.chat_id}")
    try:
        bot.run_forever()
    except KeyboardInterrupt:
        print("\nbot: stopped.")


if __name__ == "__main__":
    main()
