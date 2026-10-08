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
CHAT_ID_RE = re.compile(r"-?[0-9]+\Z|@[A-Za-z0-9_]{3,}\Z")
PHONE_RE = re.compile(r"\+[0-9][0-9 ()-]{4,}\Z")


class BotAPIError(Exception):
    """A request failed or the Telegram Bot API rejected it."""

    def __init__(self, message, network=False, http_status=None):
        super().__init__(message)
        self.network = bool(network)
        self.http_status = http_status


def valid_bot_token(token):
    """Return whether a value has the URL-safe shape of a Bot API token."""
    token = str(token or "").strip()
    return len(token) <= 256 and bool(BOT_TOKEN_RE.fullmatch(token))


def bot_token_problem(token):
    """Explain a token the API would reject, or None when it looks usable.

    The messages name the usual copy/paste mistakes so the web UI can say what
    is actually wrong instead of a bare "invalid format".
    """
    token = str(token or "").strip()
    if not token:
        return ("bot_token is required — paste the token @BotFather gave you "
                "(it looks like 123456789:AA...)")
    if valid_bot_token(token):
        return None
    if token.startswith(("http://", "https://")):
        return ("the bot token has an invalid format: that is a URL, not a "
                "token — paste only the token from @BotFather, the part after "
                "/bot in an api.telegram.org URL")
    if re.match(r"bot[0-9]+:", token, re.IGNORECASE):
        return ("the bot token has an invalid format: remove the leading "
                "\"bot\" and paste 123456789:AA... exactly as @BotFather sent "
                "it (\"bot\" is only used in Bot API URLs)")
    if ":" not in token:
        return ("the bot token has an invalid format: it must contain a colon "
                "(123456789:AA...) — this looks like a partial copy of the "
                "token @BotFather sent")
    return ("the bot token has an invalid format — paste the whole token from "
            "@BotFather (123456789:AA...): no spaces, quotes or URLs")


def chat_id_problem(chat_id):
    """Explain an unusable ``chat_id``, or None when it is acceptable.

    Accepted: a numeric user/group/channel ID (supergroup IDs are negative,
    e.g. -1001234567890) or a public channel username such as @mychannel.
    """
    chat_id = str(chat_id or "").strip()
    if not chat_id:
        return ("chat_id is required: your numeric Telegram user ID "
                "(for example 123456789) or @channelusername")
    if len(chat_id) > 128 or any(ord(char) < 32 for char in chat_id):
        return "chat_id is invalid"
    if CHAT_ID_RE.fullmatch(chat_id):
        return None
    if PHONE_RE.fullmatch(chat_id):
        return ("chat_id looks like a phone number — the Bot API needs your "
                "numeric user ID instead (send /start to @userinfobot to see it)")
    if any(char.isspace() for char in chat_id):
        return ("chat_id must be a single ID — no spaces, commas or lists "
                "(123456789, -1001234567890 or @channel)")
    if "," in chat_id:
        return ("chat_id must be a single ID — no commas or lists "
                "(123456789, -1001234567890 or @channel)")
    if chat_id.startswith("@") and len(chat_id) < 4:
        return "chat_id must be a username of at least 3 characters (@channel)"
    return ("chat_id must be a numeric user/chat ID (optionally negative) or a "
            "public channel username such as @mychannel")


def bot_error_hint(detail):
    """An actionable hint for the most common Telegram Bot API rejections."""
    text = str(detail or "").lower()
    if "chat not found" in text:
        return ("Telegram does not know that chat: for private alerts, open the "
                "bot and send /start first and use your numeric user ID from "
                "@userinfobot (not your phone number); for a group, add the bot "
                "to that group first")
    if "bot was blocked by the user" in text or "user is deactivated" in text:
        return "the recipient blocked the bot — unblock it and send /start"
    if "can't initiate conversation" in text or "bot can't initiate" in text:
        return "open your bot in Telegram and send /start first"
    if "not enough rights" in text or "chat_admin_required" in text:
        return "make the bot an administrator of that chat"
    if "chat_write_forbidden" in text or "not enough rights to send" in text:
        return "posting is not allowed in that chat for the bot"
    if "retry after" in text:
        return "Telegram rate-limited this bot — wait a moment and retry"
    if "unauthorized" in text or text.strip().rstrip(".") == "not found":
        return ("check that the token is the one @BotFather shows for this bot "
                "(with a self-hosted Bot API server, also that "
                "TELEGRAM_API_BASE is its root URL)")
    return ""


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


def api_host(base=None):
    """The host of the configured Bot API root (never contains the token)."""
    try:
        parsed = urlsplit(base if base is not None else api_base())
        hostname = parsed.hostname or "?"
        return f"{hostname}:{parsed.port}" if parsed.port else hostname
    except (BotAPIError, ValueError):
        return "the configured TELEGRAM_API_BASE"


def bot_api_call(token, method, params=None, timeout=10):
    """Call one Telegram Bot API method, returning ``result`` or raising.

    Keep errors free of request URLs: the URL contains the secret bot token.
    """
    if not token or not method:
        raise BotAPIError("bot token and method are required")
    host = api_host()
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
        if not detail:
            detail = f"{host} answered HTTP {e.code}"
        parameters = body.get("parameters") if isinstance(body, dict) else None
        retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
        if retry_after:
            detail = f"{detail} (retry after {retry_after}s)"
        raise BotAPIError(str(detail), http_status=e.code) from e
    except (urllib.error.URLError, OSError) as e:
        raise BotAPIError(
            f"could not reach the Telegram Bot API at {host}: {e}",
            network=True) from e
    except (UnicodeDecodeError, ValueError) as e:
        raise BotAPIError(f"{host} returned an invalid response",
                          network=True) from e

    try:
        body = json.loads(raw)
    except (TypeError, ValueError) as e:
        raise BotAPIError(f"{host} returned an invalid response",
                          network=True) from e
    if not isinstance(body, dict) or not body.get("ok"):
        detail = body.get("description") if isinstance(body, dict) else None
        raise BotAPIError(str(detail or f"{host} rejected the request"))
    return body.get("result")


def probe_api_base(base=None, timeout=5):
    """Check that the Bot API root answers, without any bot credentials.

    Returns ``(reachable, detail)``. Any HTTP answer — even a 404 — proves the
    host is reachable from this server; connection problems are reported with
    their reason. Used by the web UI's ``GET /api/bot_connect`` diagnostics.
    """
    try:
        url = f"{base if base is not None else api_base()}/"
    except BotAPIError as e:
        return False, str(e)
    request = urllib.request.Request(url, headers={"Accept": "application/json"},
                                     method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            code = getattr(response, "status", None)
            if code is None:
                getcode = getattr(response, "getcode", None)
                code = getcode() if callable(getcode) else "?"
        return True, f"{url} answered HTTP {code}"
    except urllib.error.HTTPError as e:
        return True, f"{url} answered HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return False, f"{url} is not reachable from this server: {e}"
    except (UnicodeDecodeError, ValueError) as e:
        return True, f"{url} answered HTTP with an invalid body: {e}"


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
