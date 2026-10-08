"""Telegram notifications via the Bot API — no extra dependencies.

Configure with two environment variables (e.g. in ``.env``):

* ``TELEGRAM_BOT_TOKEN`` — the token @BotFather gave your bot
* ``TELEGRAM_CHAT_ID``   — the chat that should receive the alerts
                           (your own user id, or a group the bot is in)
* ``TELEGRAM_API_BASE``  — optional custom Bot API root; defaults to
                           ``https://api.telegram.org``

``TELEGRAM_NOTIFY=0`` keeps the bot silent while everything else still works.
The web UI can instead provide a bot token and chat ID from its Telegram bot
alerts card; those values are sent per request and never persisted by the
server.

Notifications are *best effort*: :meth:`Notifier.send` returns ``False`` on
any problem and never raises, so a Telegram hiccup can never break a cleanup.
Used by the CLI, the web API and anything else that wants to shout about
guard decisions (``send_guard``) or cleanup runs (``send_run``).
"""

import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

API_BASE = "https://api.telegram.org"
MAX_LEN = 4096          # Telegram's message length limit
FALSEY = ("0", "false", "no", "off", "none")
BOT_TOKEN_RE = re.compile(r"[0-9]+:[A-Za-z0-9_-]+\Z")


class BotAPIError(Exception):
    """A request failed or the Telegram Bot API rejected it."""

    def __init__(self, message, network=False):
        super().__init__(message)
        self.network = bool(network)


def valid_bot_token(token):
    """Return whether a value has the URL-safe shape of a Bot API token."""
    token = str(token or "").strip()
    return len(token) <= 256 and bool(BOT_TOKEN_RE.fullmatch(token))


def api_base(environ=None):
    """Return the configured Bot API root, defaulting to Telegram's hosted API.

    ``TELEGRAM_API_BASE`` is useful when the app's server must use a custom or
    self-hosted Bot API endpoint. It is read at call time so values loaded from
    a local ``.env`` file after module import are honored too.
    """
    env = os.environ if environ is None else environ
    base = str(env.get("TELEGRAM_API_BASE") or API_BASE).strip().rstrip("/")
    if not base:
        base = API_BASE
    try:
        parsed = urlsplit(base)
        hostname = parsed.hostname
        _ = parsed.port  # force validation of an explicitly supplied port
    except ValueError as e:
        raise BotAPIError(
            "TELEGRAM_API_BASE must be an absolute HTTP(S) URL without credentials, "
            "a query, or a fragment") from e
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise BotAPIError(
            "TELEGRAM_API_BASE must be an absolute HTTP(S) URL without credentials, "
            "a query, or a fragment")
    return base


def bot_api_call(token, method, params=None, timeout=10):
    """Call one Telegram Bot API method, returning ``result`` or raising.

    Keep errors free of request URLs: the URL contains the secret bot token.
    """
    if not token or not method:
        raise BotAPIError("bot token and method are required")
    url = f"{api_base()}/bot{token}/{method}"
    request = urllib.request.Request(
        url, data=json.dumps(params or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8") or "{}"
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except (AttributeError, UnicodeDecodeError, ValueError):
            body = {}
        detail = (body.get("description") if isinstance(body, dict) else None)
        raise BotAPIError(str(detail or f"Telegram returned HTTP {e.code}")) from e
    except (urllib.error.URLError, OSError) as e:
        raise BotAPIError(f"could not reach Telegram Bot API: {e}",
                          network=True) from e
    except (UnicodeDecodeError, ValueError) as e:
        raise BotAPIError("Telegram returned an invalid response",
                          network=True) from e

    try:
        body = json.loads(raw)
    except (TypeError, ValueError) as e:
        raise BotAPIError("Telegram returned an invalid response",
                          network=True) from e
    if not isinstance(body, dict) or not body.get("ok"):
        detail = body.get("description") if isinstance(body, dict) else None
        raise BotAPIError(str(detail or "Telegram Bot API request failed"))
    return body.get("result")


class Notifier:
    """Sends short Telegram messages through a bot."""

    def __init__(self, token, chat_id, timeout=10, enabled=True):
        self.token = (token or "").strip()
        self.chat_id = (str(chat_id).strip() if chat_id else "")
        self.timeout = timeout
        self.enabled = bool(enabled and self.token and self.chat_id)

    @classmethod
    def from_env(cls, environ=None):
        """Notifier from the environment, or a disabled one when unset."""
        env = os.environ if environ is None else environ
        token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
        chat_id = (env.get("TELEGRAM_CHAT_ID") or "").strip().split(",")[0].strip()
        on = (env.get("TELEGRAM_NOTIFY") or "1").strip().lower() not in FALSEY
        return cls(token, chat_id, enabled=on)

    # -- sending ----------------------------------------------------------

    def send(self, text):
        """Send one message. Returns True on success, never raises."""
        if not self.enabled or not text:
            return False
        return send_message(self.token, self.chat_id, text, timeout=self.timeout)

    def send_guard(self, record):
        """One guard_core audit record as a short alert."""
        return self.send(format_guard(record))

    def send_run(self, action, summary=""):
        """A cleanup-run summary (clean/wipe/leave/delete-account)."""
        return self.send(format_run(action, summary))


def send_message(token, chat_id, text, timeout=10):
    """POST sendMessage to the Bot API. True on success, False on any error."""
    if not token or not chat_id or not text:
        return False
    payload = {
        "chat_id": str(chat_id),
        "text": str(text)[:MAX_LEN],
        "disable_web_page_preview": True,
    }
    try:
        bot_api_call(token, "sendMessage", payload, timeout=timeout)
        return True
    except BotAPIError:
        return False


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------

def format_guard(record):
    """One guard audit record as a human-readable alert (plain text)."""
    status = record.get("status") or "?"
    user = record.get("user") or {}
    who = (f"@{user['username']}" if user.get("username")
           else " ".join(str(user.get(k) or "") for k in ("first", "last")).strip()
           or (f"id {user['id']}" if user.get("id") else "someone"))
    chat = record.get("chat_label") or record.get("chat_key") or "?"
    head = {"acted": "🛡 guard ACTED", "error": "⚠️ guard ERROR",
            "paused": "⏸ guard PAUSED", "protected": "🛡 guard skipped",
            "dry_run": "🔍 guard dry run",
            "ignored": "guard"}.get(status, f"guard {status}")
    lines = [f"{head}: {who} in {chat}"]
    results = [str(v) for v in (record.get("results") or {}).values() if v]
    if results:
        lines.append("  " + "; ".join(results))
    if record.get("reason"):
        lines.append(f"  ({record['reason']})")
    return "\n".join(lines)


def format_run(action, summary=""):
    """A cleanup-run summary as a human-readable alert (plain text)."""
    icon = {"clean-messages": "🧹", "wipe": "🧽", "leave-all": "🚪",
            "delete-account": "💀", "guard": "🛡"}.get(action, "▶️")
    return f"{icon} {action}: {summary}" if summary else f"{icon} {action}"
