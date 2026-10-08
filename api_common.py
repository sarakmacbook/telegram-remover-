"""Shared helpers for the Vercel serverless endpoints (api/*.py).

Stateless design: the Telegram session is a Telethon ``StringSession`` sent
by the browser (kept in localStorage) as the ``X-Tg-Session`` header. Optional
browser bot credentials are also sent per request for alerts; neither is
stored server-side between requests.

Set the ``ACCESS_TOKEN`` environment variable to password-protect the whole
deployment — every request must then send it as ``X-Access-Token``.

Handlers return plain dicts / (dict, status) tuples; Flask jsonifies them.
"""

import asyncio
import os
import re

from flask import request
from werkzeug.exceptions import HTTPException
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

import db
import guard_core
import notify
import remover_core

DEFAULT_CHUNK = 200   # messages per /api/clean and /api/wipe call
MAX_CHUNK = 1000
DEFAULT_LEAVE = 10    # chats per /api/leave call
MAX_LEAVE = 100

# Anything shaped like a Bot API token, so error text can never leak one.
SECRET_RE = re.compile(r"[0-9]{5,}:[A-Za-z0-9_-]{10,}")


def redact(text):
    """Replace anything shaped like a bot token in ``text``."""
    return SECRET_RE.sub("bot<redacted>", str(text))


def json_errors(app):
    """Make a Flask app answer every error as JSON instead of an HTML page.

    A crashed serverless function otherwise returns the platform's HTML error
    page, which the web UI can only report as an unexplained failure ("Could
    not connect the bot."). JSON keeps the real reason visible.
    """
    @app.errorhandler(HTTPException)
    def _http_error(error):
        return {"error": f"{error.code} {error.name}: {error.description}",
                "http_status": error.code}, error.code

    @app.errorhandler(Exception)
    def _unhandled(error):      # pragma: no cover - defensive
        return {"error": f"server error: {type(error).__name__}: "
                         f"{redact(error)}", "http_status": 500}, 500
    return app


# --------------------------------------------------------------------------
# SQL storage + Telegram notifications (optional, best effort)
# --------------------------------------------------------------------------

def _store():
    """The SQL store (set DATABASE_URL to enable), or None. Never raises."""
    try:
        return db.get_store()
    except Exception:  # noqa: BLE001 — storage must never break an API call
        return None


def _notifier(headers=None):
    """Use browser-provided bot settings for this request, else server env."""
    try:
        if headers is not None:
            token = (headers.get("X-Tg-Bot-Token") or "").strip()
            chat_id = (headers.get("X-Tg-Bot-Chat-Id") or "").strip()
            if token or chat_id:
                # A partial/invalid browser config must not fall through to a
                # different bot configured for the deployment.
                if not notify.valid_bot_token(token) or not chat_id:
                    return notify.Notifier("", "")
                enabled = (os.environ.get("TELEGRAM_NOTIFY") or "1").strip().lower() \
                    not in notify.FALSEY
                return notify.Notifier(token, chat_id, enabled=enabled)
        return notify.Notifier.from_env()
    except Exception:  # noqa: BLE001
        return notify.Notifier("", "")


def _record_run(action, summary, status="done", chat=None, details=None,
                headers=None):
    """Store one cleanup run and push a Telegram summary (best effort)."""
    store, notifier = _store(), _notifier(headers)
    if store is not None:
        try:
            store.record_run(action, status=status, chat=chat, details=details,
                             source="web")
        except Exception:  # noqa: BLE001
            pass
    notifier.send_run(action, summary)


def _record_guard(records, headers=None):
    """Store guard audit records and push alerts for the loud ones."""
    store, notifier = _store(), _notifier(headers)
    for record in records or []:
        if record.get("preview"):
            continue    # previews decide nothing real
        if store is not None:
            try:
                store.record_guard(record, source="web")
            except Exception:  # noqa: BLE001
                pass
        if record.get("status") != "ignored":
            notifier.send_guard(record)


# --------------------------------------------------------------------------
# request helpers
# --------------------------------------------------------------------------

def guard():
    """Optional deployment-level access token. Returns a response or None."""
    token = os.environ.get("ACCESS_TOKEN")
    if token and request.headers.get("X-Access-Token") != token:
        return {"error": "unauthorized: missing or wrong X-Access-Token"}, 401
    return None


def tg_session(headers):
    session = (headers.get("X-Tg-Session") or "").strip()
    if not session:
        return None, ({"error": "not logged in (missing X-Tg-Session)"}, 401)
    return session, None


def api_creds(data):
    """API_ID/API_HASH from the request body or the server environment."""
    api_id = str(data.get("api_id") or os.environ.get("API_ID") or "").strip()
    api_hash = str(data.get("api_hash") or os.environ.get("API_HASH") or "").strip()
    if not api_id or not api_hash:
        return None, None, ({
            "error": "API_ID/API_HASH missing — enter them in the web UI "
                     "or set them as environment variables on Vercel"
        }, 400)
    try:
        return int(api_id), api_hash, None
    except ValueError:
        return None, None, ({"error": "API_ID must be a number"}, 400)


def _exec(session, api_id, api_hash, work):
    """Connect a fresh client from a StringSession, run work(client), return."""
    async def runner():
        client = TelegramClient(StringSession(session), api_id, api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise PermissionError(
                    "Telegram session is not authorized — log in again")
            return await work(client)
        finally:
            await client.disconnect()

    try:
        return asyncio.run(runner())
    except remover_core.FloodWait as e:
        return {"flood_wait": e.seconds}, 429
    except PermissionError as e:
        return {"error": str(e)}, 401
    except errors.RPCError as e:
        return {"error": f"{type(e).__name__}: {e}"}, 400
    except ValueError as e:
        return {"error": str(e)}, 400


def _chunk_params(data):
    try:
        cursor = int(data.get("cursor") or 0)
        limit = int(data.get("limit") or DEFAULT_CHUNK)
    except (TypeError, ValueError):
        return None, None, ({"error": "cursor/limit must be integers"}, 400)
    return cursor, max(1, min(limit, MAX_CHUNK)), None


# --------------------------------------------------------------------------
# handlers (unit-testable without a Flask request context)
# --------------------------------------------------------------------------

def _api_base_info():
    """The configured Bot API root, or None when TELEGRAM_API_BASE is broken."""
    try:
        return notify.api_base()
    except notify.BotAPIError:
        return None


def _with_hint(message, error):
    """Append the actionable hint for a Bot API rejection, if there is one."""
    hint = notify.bot_error_hint(error)
    if not hint:
        return message
    return f"{message} {hint[0].upper()}{hint[1:]}."


def handle_bot_connect(data):
    """Verify a browser-supplied bot token and deliver a one-time test alert.

    Credentials are only used for these two Bot API calls and are never stored
    by the server. The browser may keep them locally after this succeeds.

    Every failure names what went wrong (bad token shape, unreachable Bot API
    host, chat the bot cannot post to) so the UI never has to guess.
    """
    if not isinstance(data, dict):
        return {"error": "the request body must be a JSON object with "
                         "bot_token and chat_id"}, 400
    token = str(data.get("bot_token") or "").strip()
    chat_id = str(data.get("chat_id") or "").strip()
    problem = notify.bot_token_problem(token)
    if problem:
        return {"error": problem}, 400
    problem = notify.chat_id_problem(chat_id)
    if problem:
        return {"error": problem}, 400

    base = _api_base_info()
    try:
        me = notify.bot_api_call(token, "getMe", timeout=10)
    except notify.BotAPIError as e:
        status = 502 if e.network else 400
        message = (f"Could not verify the bot token with "
                   f"{notify.api_host(base)}: {redact(e)}.")
        if base is None:
            message += (" Fix TELEGRAM_API_BASE on the server: it must be an "
                        "absolute http(s) URL.")
        return {"error": _with_hint(message, e), "api_base": base}, status
    if not isinstance(me, dict):
        return {"error": "Telegram returned an invalid bot profile"}, 502

    try:
        notify.bot_api_call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "✅ telegram-remover: test alert — this bot can send messages to this chat.",
            "disable_web_page_preview": True,
        }, timeout=10)
    except notify.BotAPIError as e:
        status = 502 if e.network else 400
        message = ("The bot token is valid, but Telegram could not deliver the "
                   f"test message to {chat_id}: {redact(e)}.")
        hint = notify.bot_error_hint(e)
        message += (f" {hint[0].upper()}{hint[1:]}." if hint else
                    " For a private chat, open the bot and send /start first, "
                    "then check the chat ID.")
        return {"error": message, "api_base": base}, status

    return {
        "connected": True,
        "test_message_sent": True,
        "chat_id": chat_id,
        "api_base": base,
        "bot": {
            "id": me.get("id"),
            "username": me.get("username"),
            "first_name": me.get("first_name"),
        },
    }


def handle_bot_diagnostics():
    """Diagnose the bot-alert path for ``GET /api/bot_connect``.

    Answers without any credentials, so the web UI (or ``curl``) can tell a
    misconfigured deployment from a bad token: it reports the Bot API root in
    use, whether this server can reach it at all, and what else would make a
    connect attempt fail. Tokens are never returned, only their presence.
    """
    env = os.environ
    base = _api_base_info()
    reachable, detail = notify.probe_api_base(base, timeout=5)
    access_token = bool((env.get("ACCESS_TOKEN") or "").strip())
    notify_on = (env.get("TELEGRAM_NOTIFY") or "1").strip().lower() \
        not in notify.FALSEY
    server_bot = bool((env.get("TELEGRAM_BOT_TOKEN") or "").strip()
                      and (env.get("TELEGRAM_CHAT_ID") or "").strip())

    hints = []
    if base is None:
        hints.append("TELEGRAM_API_BASE is not a valid absolute http(s) URL — "
                     "fix it on the deployment or remove it.")
    if not reachable:
        hints.append(
            f"This server cannot reach the Bot API host "
            f"{notify.api_host(base)}. Hosted deployments such as Vercel "
            "normally can; sandboxes, some corporate networks and firewalls "
            "block api.telegram.org. Point TELEGRAM_API_BASE at a reachable "
            "Bot API server (or set HTTPS_PROXY) and retry.")
    if access_token:
        hints.append("This deployment requires ACCESS_TOKEN: paste that value "
                     "in the Access token field before connecting the bot.")
    if not notify_on:
        hints.append("TELEGRAM_NOTIFY=0 on the server: automatic alerts stay "
                     "muted (the test alert from this card is still sent).")

    return {
        "endpoint": "bot_connect",
        "api_base": base,
        "api_host": notify.api_host(base) if base else None,
        "api_host_reachable": reachable,
        "detail": detail,
        "chat_id_help": ("Use your numeric user ID from @userinfobot "
                         "(never your phone number), a negative supergroup ID, "
                         "or @channelusername for a public channel."),
        "server_bot_configured": server_bot,
        "access_token_required": access_token,
        "notify_enabled": notify_on,
        "hints": hints,
    }


def handle_auth_start(data):
    """Send a login code to the phone. Returns the phone_code_hash."""
    api_id, api_hash, err = api_creds(data)
    if err:
        return err
    phone = str(data.get("phone") or "").strip()
    if not phone:
        return {"error": "phone is required (with country code)"}, 400

    async def runner():
        client = TelegramClient(StringSession(""), api_id, api_hash)
        await client.connect()
        try:
            sent = await client.send_code_request(phone)
            return {"phone_code_hash": sent.phone_code_hash}
        finally:
            await client.disconnect()

    try:
        return asyncio.run(runner())
    except errors.RPCError as e:
        return {"error": f"{type(e).__name__}: {e}"}, 400


def handle_auth_finish(data):
    """Confirm the login code (and 2FA password if needed).

    Returns {"session": <StringSession>, "me": {...}} on success,
    {"need_password": True} when 2FA is enabled and no password was given.
    """
    api_id, api_hash, err = api_creds(data)
    if err:
        return err
    phone = str(data.get("phone") or "").strip()
    code = str(data.get("code") or "").strip()
    phone_code_hash = str(data.get("phone_code_hash") or "").strip()
    password = str(data.get("password") or "").strip() or None
    if not phone or not code or not phone_code_hash:
        return {"error": "phone, code and phone_code_hash are required"}, 400

    async def runner():
        client = TelegramClient(StringSession(""), api_id, api_hash)
        await client.connect()
        try:
            try:
                await client.sign_in(phone=phone, code=code,
                                     phone_code_hash=phone_code_hash)
            except errors.SessionPasswordNeededError:
                if not password:
                    return {"need_password": True}
                await client.sign_in(password=password)
            me = await client.get_me()
            return {
                "session": client.session.save(),
                "me": {
                    "id": me.id,
                    "name": f"{me.first_name or ''} {me.last_name or ''}".strip(),
                    "username": me.username,
                },
            }
        finally:
            await client.disconnect()

    try:
        return asyncio.run(runner())
    except errors.PasswordHashInvalidError:
        return {"error": "wrong 2FA password", "need_password": True}, 400
    except errors.RPCError as e:
        restart = type(e).__name__ in ("PhoneCodeExpiredError",
                                       "PhoneCodeInvalidError",
                                       "PhoneCodeEmptyError")
        return {"error": f"{type(e).__name__}: {e}", "restart": restart}, 400


def handle_dialogs(headers, query):
    """List the user's chats."""
    session, err = tg_session(headers)
    if err:
        return err
    api_id, api_hash, err = api_creds(query)
    if err:
        return err

    async def work(client):
        out = []
        async for d in client.iter_dialogs():
            out.append({"id": d.id, "title": d.title or "",
                        "kind": remover_core.classify(d.entity)})
        return {"dialogs": out}

    return _exec(session, api_id, api_hash, work)


def handle_clean(data, headers):
    """Delete up to `limit` of the user's own messages in one chat."""
    session, err = tg_session(headers)
    if err:
        return err
    api_id, api_hash, err = api_creds(data)
    if err:
        return err
    chat = str(data.get("chat") or "").strip()
    if not chat:
        return {"error": "chat is required (@username, id, or exact title)"}, 400
    cursor, limit, err = _chunk_params(data)
    if err:
        return err

    async def work(client):
        entity = await remover_core.resolve_entity(client, chat)
        me = await client.get_me()
        return await remover_core.sweep_chunk(client, entity, limit, cursor,
                                              from_user=me, sleep=False)

    result = _exec(session, api_id, api_hash, work)
    if isinstance(result, dict):
        _record_run("clean-messages",
                    f"deleted {result.get('deleted', 0)} message(s) in {chat}"
                    + (f" ({result.get('failed', 0)} failed)"
                       if result.get("failed") else ""),
                    status="done", chat=chat,
                    details={k: result.get(k) for k in
                             ("processed", "deleted", "failed", "done")},
                    headers=headers)
    return result


def handle_wipe(data, headers):
    """Wipe up to `limit` messages of the FULL history of one chat."""
    session, err = tg_session(headers)
    if err:
        return err
    api_id, api_hash, err = api_creds(data)
    if err:
        return err
    chat = str(data.get("chat") or "").strip()
    if not chat:
        return {"error": "chat is required (@username, id, or exact title)"}, 400
    cursor, limit, err = _chunk_params(data)
    if err:
        return err

    async def work(client):
        entity = await remover_core.resolve_entity(client, chat)
        return await remover_core.sweep_chunk(client, entity, limit, cursor,
                                              from_user=None, sleep=False)

    result = _exec(session, api_id, api_hash, work)
    if isinstance(result, dict):
        _record_run("wipe",
                    f"wiped {result.get('deleted', 0)} message(s) of {chat}"
                    + (f" ({result.get('failed', 0)} failed)"
                       if result.get("failed") else ""),
                    status="done", chat=chat,
                    details={k: result.get(k) for k in
                             ("processed", "deleted", "failed", "done")},
                    headers=headers)
    return result


def handle_leave(data, headers):
    """Leave up to `limit` groups/channels (keep-list respected)."""
    session, err = tg_session(headers)
    if err:
        return err
    api_id, api_hash, err = api_creds(data)
    if err:
        return err
    try:
        limit = max(1, min(int(data.get("limit") or DEFAULT_LEAVE), MAX_LEAVE))
    except (TypeError, ValueError):
        return {"error": "limit must be an integer"}, 400
    keep_raw = data.get("keep") or []
    if not isinstance(keep_raw, list):
        return {"error": "keep must be a list"}, 400

    async def work(client):
        me = await client.get_me()
        keep_ids = set()
        for k in keep_raw:
            keep_ids.add((await remover_core.resolve_entity(client, k)).id)
        return await remover_core.leave_chats(client, keep_ids, limit=limit,
                                              me=me, sleep=False)

    result = _exec(session, api_id, api_hash, work)
    if isinstance(result, dict):
        _record_run("leave-all",
                    f"left {result.get('left', 0)} chat(s)"
                    + (f" ({result.get('failed', 0)} failed)"
                       if result.get("failed") else ""),
                    status="done",
                    details={k: result.get(k) for k in ("left", "failed", "done")},
                    headers=headers)
    return result


def handle_guard(data, headers):
    """The join guard: act on the joins that happened since ``cursor``.

    Safety gates, all server-side (the browser cannot skip them):

    * ``cursor`` is required unless the caller asks for a ``baseline`` (a
      first call that only records "watch from here" — the guard never acts
      on old history by accident).
    * Deleting the join notices needs ``armed`` to contain ``"delete"``.
    * Kicking/banning/purging needs ``armed`` **and** the exact confirmation
      phrase in ``confirm`` whenever ``dry_run`` is false (HTTP 403
      otherwise) — see ``guard_core.required_confirmation``.
    * At most ``MAX_ACTIONS_PER_CALL`` member actions per request, and
      ``JoinGuard`` pauses itself after ``max_actions_per_hour`` (default 30).
    * Admins, the account owner and the allow-list are resolved on the server
      and are never touched.
    """
    session, err = tg_session(headers)
    if err:
        return err
    api_id, api_hash, err = api_creds(data)
    if err:
        return err
    chat = str(data.get("chat") or "").strip()
    if not chat:
        return {"error": "chat is required (@username, id, or exact title)"}, 400
    try:
        cursor = max(0, int(data.get("cursor") or 0))
        scan = int(data.get("scan") or guard_core.DEFAULT_SCAN)
        max_actions = int(data.get("max_actions")
                          or guard_core.DEFAULT_ACTIONS_PER_CALL)
        ban_seconds = int(round(float(data.get("ban_hours") or 0) * 3600))
        max_per_hour = int(data.get("max_actions_per_hour")
                           or guard_core.DEFAULT_MAX_ACTIONS_PER_HOUR)
    except (TypeError, ValueError):
        return {"error": "cursor/scan/max_actions/ban_hours must be numbers"}, 400

    armed_raw = data.get("armed") or []
    if not isinstance(armed_raw, list):
        return {"error": "armed must be a list of action names "
                         f"({', '.join(guard_core.ACTIONS)})"}, 400
    unknown = [str(a) for a in armed_raw if str(a) not in guard_core.ACTIONS]
    if unknown:
        return {"error": f"unknown armed action(s): {', '.join(unknown)}"}, 400
    allow = data.get("allow") or []
    if not isinstance(allow, list):
        return {"error": "allow must be a list of @usernames or user ids"}, 400

    dry_run = bool(data.get("dry_run", True))
    preview = bool(data.get("preview", False))

    # Remote kill switch: the companion bot's /pause is stored in the SQL
    # database and holds every live action until /resume.
    store = _store()
    if store is not None and not dry_run and not preview:
        try:
            if store.kv_get("guard:paused"):
                return {
                    "error": ("the guard is paused (Telegram bot /pause) — "
                              "send /resume in the bot first"),
                    "paused": True,
                }, 409
        except Exception:  # noqa: BLE001
            pass

    policy = guard_core.GuardPolicy(
        delete_join_message=bool(data.get("delete_join_message", True)),
        remove_joiner=bool(data.get("remove_joiner")),
        ban_joiner=bool(data.get("ban_joiner")),
        ban_seconds=ban_seconds,
        purge_joiner_messages=bool(data.get("purge_joiner_messages")),
        include_added=bool(data.get("include_added")),
        allow=allow,
        protect_admins=bool(data.get("protect_admins", True)),
        max_actions_per_hour=max_per_hour,
    )
    armed = {str(a) for a in armed_raw}
    confirmation = policy.confirmation

    if not dry_run and not preview and confirmation:
        given = str(data.get("confirm") or "").strip()
        if given != confirmation:
            return {
                "error": (f"refusing to kick/ban: type {confirmation!r} into the "
                          f"confirmation box first (got {given!r})"),
                "confirm_required": confirmation,
            }, 403

    async def work(client):
        entity = await remover_core.resolve_entity(client, chat)
        join_guard = guard_core.JoinGuard(
            policy, dry_run=dry_run or preview, armed=armed, sleep=False,
            pause_check=lambda: bool(store and store.kv_get("guard:paused")))
        await join_guard.prepare(client, entity)
        result = await join_guard.scan(client, entity, cursor=cursor, scan=scan,
                                       max_actions=max_actions, preview=preview)
        for record in result.get("records") or []:
            record["text"] = guard_core.render_record(record)
        return result

    result = _exec(session, api_id, api_hash, work)
    if isinstance(result, tuple):      # an error response from _exec
        return result
    result["policy"] = policy.to_dict()
    result["confirmation"] = confirmation

    # Mirror everything into the SQL database (events, cursor, status) so the
    # companion bot's /status, /recent and /pause work across machines.
    _record_guard(result.get("records"), headers=headers)
    if store is not None and not preview:
        try:
            store.kv_set(f"guard:cursor:{chat}", result.get("next_cursor"))
            store.kv_set("guard:status", {
                "mode": "dry run" if dry_run else "LIVE",
                "chats": [chat],
                "source": "web (polling)",
                "last_scan": result.get("scanned"),
                "summary": result.get("summary"),
            })
        except Exception:  # noqa: BLE001
            pass
    return result


def handle_delete_account(data, headers):
    """Permanently delete the account. The nuclear option."""
    session, err = tg_session(headers)
    if err:
        return err
    api_id, api_hash, err = api_creds(data)
    if err:
        return err
    reason = str(data.get("reason") or "User requested account deletion")

    async def work(client):
        await remover_core.delete_account(client, reason, sleep=False)
        return {"deleted": True}

    result = _exec(session, api_id, api_hash, work)
    if isinstance(result, dict) and result.get("deleted"):
        _record_run("delete-account",
                    "account permanently deleted", status="done",
                    headers=headers)
    return result


def handle_events(data):
    """GET history from the SQL database (guard events + cleanup runs).

    ``{"enabled": false}`` when the server has no ``DATABASE_URL`` — nothing
    is stored server-side in that case (by design, see README).
    """
    store = _store()
    if store is None:
        return {
            "enabled": False,
            "reason": ("set DATABASE_URL (any SQLAlchemy URL — SQLite, "
                       "PostgreSQL or MySQL) to store history server-side"),
        }
    try:
        limit = int(data.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 100))
    return {
        "enabled": True,
        "retention_days": store.retention_days,
        "stats": store.stats(),
        "events": store.list_events(limit=limit),
        "runs": store.list_runs(limit=limit),
        "paused": bool(store.kv_get("guard:paused")),
    }
