"""Shared helpers for the Vercel serverless endpoints (api/*.py).

Stateless design: the Telegram session is a Telethon ``StringSession`` sent
by the browser (kept in localStorage) as the ``X-Tg-Session`` header. Nothing
is stored server-side between requests.

Set the ``ACCESS_TOKEN`` environment variable to password-protect the whole
deployment — every request must then send it as ``X-Access-Token``.

Handlers return plain dicts / (dict, status) tuples; Flask jsonifies them.
"""

import asyncio
import os

from flask import request
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

import remover_core

DEFAULT_CHUNK = 200   # messages per /api/clean and /api/wipe call
MAX_CHUNK = 1000
DEFAULT_LEAVE = 10    # chats per /api/leave call
MAX_LEAVE = 100


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

    return _exec(session, api_id, api_hash, work)


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

    return _exec(session, api_id, api_hash, work)


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

    return _exec(session, api_id, api_hash, work)


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

    return _exec(session, api_id, api_hash, work)
