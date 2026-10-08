"""Shared Telegram-cleanup logic for the telegram-remover CLI and web app.

Everything here is async and works with any Telethon ``TelegramClient``.
Flood waits either sleep through (``sleep=True``, used by the CLI) or raise
:class:`FloodWait` (``sleep=False``, used by the web app so a serverless
function can bail out with a 429 and let the browser retry).
"""

import asyncio

from telethon import errors, types
from telethon.tl.functions.account import DeleteAccountRequest
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.functions.messages import DeleteChatUserRequest

BATCH = 100  # Telegram's per-call delete limit


class FloodWait(Exception):
    """Raised instead of sleeping when sleep=False (web/serverless mode)."""

    def __init__(self, seconds):
        super().__init__(f"flood wait: {seconds}s")
        self.seconds = seconds


async def rpc(call, sleep=True):
    """Run an RPC. With sleep=True, sleep through flood waits (CLI).
    With sleep=False, raise FloodWait so the caller can bail out (web)."""
    while True:
        try:
            return await call()
        except errors.FloodWaitError as e:
            if not sleep:
                raise FloodWait(e.seconds) from e
            await asyncio.sleep(e.seconds + 1)


def classify(entity):
    """Human-readable kind of a chat entity."""
    if isinstance(entity, types.User):
        return "private"
    if isinstance(entity, types.Channel) and entity.megagroup:
        return "supergroup"
    if isinstance(entity, types.Channel):
        return "channel"
    if isinstance(entity, types.Chat):
        return "group"
    return "unknown"


async def resolve_entity(client, query):
    """Resolve @username, numeric id, or exact chat title to an entity."""
    try:
        return await client.get_entity(query)
    except (ValueError, TypeError):
        pass
    try:
        q = int(query)
    except (TypeError, ValueError):
        q = None
    title = str(query).lower()
    async for d in client.iter_dialogs():
        if (q is not None and d.id == q) or (d.title or "").lower() == title:
            return d.entity
    raise ValueError(f"could not find a chat matching {query!r}")


async def delete_ids(client, entity, ids, revoke=True, sleep=True):
    """Delete a batch of message ids. Returns how many FAILED."""
    if not ids:
        return 0
    try:
        await rpc(lambda: client.delete_messages(entity, ids, revoke=revoke),
                  sleep=sleep)
        return 0
    except errors.ChatAdminRequiredError:
        # Not allowed to delete for everyone here (not admin / channel):
        # fall back to deleting for ourselves only.
        if revoke:
            return await delete_ids(client, entity, ids, revoke=False,
                                    sleep=sleep)
        return len(ids)
    except errors.RPCError as e:
        print(f"    could not delete {len(ids)} message(s): {e}", flush=True)
        return len(ids)


async def sweep_chunk(client, entity, limit, cursor=0, from_user=None,
                      yes=True, revoke=True, sleep=True):
    """Delete up to ``limit`` messages with id < cursor (cursor=0: from newest).

    Deletes in batches of BATCH. Returns a dict:

      processed   — messages examined
      deleted     — messages actually deleted (0 in dry-run, yes=False)
      failed      — messages that could not be deleted
      next_cursor — pass as ``cursor`` to continue where this stopped
      done        — True when no more matching messages remain
    """
    processed = deleted = failed = 0
    ids = []
    smallest = cursor or None

    async def flush():
        nonlocal ids, deleted, failed
        if ids:
            if yes:
                f = await delete_ids(client, entity, ids, revoke=revoke,
                                     sleep=sleep)
                deleted += len(ids) - f
                failed += f
            ids = []

    async for msg in client.iter_messages(entity, limit=limit,
                                          offset_id=cursor or 0,
                                          from_user=from_user):
        processed += 1
        ids.append(msg.id)
        if smallest is None or msg.id < smallest:
            smallest = msg.id
        if len(ids) >= BATCH:
            await flush()
    await flush()
    return {
        "processed": processed,
        "deleted": deleted,
        "failed": failed,
        "next_cursor": smallest or cursor or 0,
        "done": processed < limit,
    }


async def leave_chats(client, keep_ids, limit=10, yes=True, sleep=True,
                      me=None):
    """Leave up to ``limit`` groups/channels not in ``keep_ids``.

    Returns {"left", "failed", "done"}. Already-left chats disappear from
    the dialog list, so calling again naturally continues where it stopped.
    """
    if me is None:
        me = await client.get_me()
    left = failed = 0
    more = False
    async for d in client.iter_dialogs():
        ent = d.entity
        if ent is None:
            continue
        if classify(ent) not in ("group", "supergroup", "channel"):
            continue
        if ent.id in keep_ids:
            continue
        if left >= limit:
            more = True
            continue
        if isinstance(ent, types.Chat):  # small/basic group
            req = DeleteChatUserRequest(ent.id, me)
        else:  # channel or megagroup
            req = LeaveChannelRequest(ent)
        try:
            if yes:
                await rpc(lambda: client(req), sleep=sleep)
            left += 1
        except errors.RPCError as e:
            failed += 1
            print(f"    could not leave {d.title}: {e}", flush=True)
    return {"left": left, "failed": failed, "done": not more}


async def delete_account(client, reason="User requested account deletion",
                         sleep=True):
    """Permanently delete the Telegram account. The nuclear option."""
    await rpc(lambda: client(DeleteAccountRequest(reason=reason)), sleep=sleep)
    return True
