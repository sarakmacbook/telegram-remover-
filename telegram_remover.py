#!/usr/bin/env python3
"""
telegram-remover — remove the shxt from YOUR OWN Telegram account.

A CLI that runs on your account (MTProto user session via Telethon) and can:

  * clean-messages  — delete every message YOU sent, in every chat
  * wipe            — wipe the full history of a single chat
  * leave-all       — leave every group and channel you joined
  * delete-account  — permanently delete your Telegram account

The official Bot API cannot do any of this (it can't touch your private
chats, act as you, or leave groups on your behalf), which is why this tool
uses a user session, exactly like Telegram Desktop does.

SAFETY: every destructive command is a DRY RUN unless you pass --yes.
Nothing is deleted until you say so.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from telethon import TelegramClient, errors, types
from telethon.tl.functions.account import DeleteAccountRequest
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.functions.messages import DeleteChatUserRequest

BATCH = 100  # Telegram allows deleting up to 100 messages per call


# --------------------------------------------------------------------------
# config / connection helpers
# --------------------------------------------------------------------------

def load_config():
    """Read API_ID / API_HASH / PHONE from the environment or a .env file."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    api_id = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    if not api_id or not api_hash:
        sys.exit(
            "ERROR: API_ID and API_HASH are not set.\n"
            "Get them at https://my.telegram.org (API development tools) and put\n"
            "them in a .env file — see .env.example."
        )
    return int(api_id), api_hash, os.getenv("PHONE")


async def rpc(call, what="request"):
    """Run an RPC, transparently sleeping through Telegram's flood limits."""
    while True:
        try:
            return await call()
        except errors.FloodWaitError as e:
            print(f"    flood-limited, sleeping {e.seconds}s ...", flush=True)
            await asyncio.sleep(e.seconds + 1)


async def connect(args, need_auth=True):
    """Connect the client; abort with a friendly message if not logged in."""
    api_id, api_hash, phone = load_config()
    client = TelegramClient(args.session, api_id, api_hash)
    await client.connect()
    if need_auth and not await client.is_user_authorized():
        await client.disconnect()
        sys.exit("Not logged in. Run first:  python telegram_remover.py login")
    return client, phone


async def resolve_chat(client, query):
    """Accept @username, numeric id, phone number, or an exact chat title."""
    try:
        return await client.get_entity(query)
    except (ValueError, TypeError):
        pass
    q = query.lower()
    async for d in client.iter_dialogs():
        if (d.title or "").lower() == q:
            return d.entity
    sys.exit(f"ERROR: could not find a chat matching {query!r}")


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


# --------------------------------------------------------------------------
# deletion helpers
# --------------------------------------------------------------------------

async def delete_ids(client, entity, ids, revoke=True):
    """Delete a batch of message ids. Returns how many FAILED."""
    if not ids:
        return 0
    try:
        await rpc(lambda: client.delete_messages(entity, ids, revoke=revoke))
        return 0
    except errors.ChatAdminRequiredError:
        # Not allowed to delete for everyone here (not admin / channel):
        # fall back to deleting for ourselves only.
        if revoke:
            return await delete_ids(client, entity, ids, revoke=False)
        return len(ids)
    except errors.RPCError as e:
        print(f"    could not delete {len(ids)} message(s): {e}", flush=True)
        return len(ids)


async def _sweep(client, entity, label, yes, from_user=None, limit=None):
    """Iterate a chat and delete messages in batches of BATCH.

    If from_user is given, only that user's messages are removed.
    Returns (processed, failed).
    """
    processed = failed = 0
    ids = []

    async def flush():
        nonlocal ids, failed
        if ids:
            if yes:
                failed += await delete_ids(client, entity, ids)
            ids = []

    async for msg in client.iter_messages(entity, from_user=from_user,
                                         limit=limit):
        ids.append(msg.id)
        if len(ids) >= BATCH:
            processed += len(ids)
            await flush()
            print(f"\r  {label}: {processed} so far ...", end="", flush=True)
    if ids:
        processed += len(ids)
        await flush()
    if processed:
        print(f"\r  {label}: {processed} so far ...")
    return processed, failed


async def wipe_entity(client, entity, label, yes):
    """Delete ALL messages in a chat (both sides where Telegram permits)."""
    return await _sweep(client, entity, label, yes, from_user=None)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

async def cmd_login(args):
    api_id, api_hash, phone = load_config()
    client = TelegramClient(args.session, api_id, api_hash)
    await client.start(
        phone=lambda: phone or input("Phone number (with country code): ")
    )
    me = await client.get_me()
    name = f"{me.first_name or ''} {me.last_name or ''}".strip()
    print(f"Logged in as {name} (@{me.username}, id {me.id})")
    print(f"Session saved to '{args.session}.session' — keep this file SECRET,")
    print("anyone with it can act as you. It is gitignored.")
    await client.disconnect()


async def cmd_dialogs(args):
    client, _ = await connect(args)
    print(f"{'ID':>12}  [KIND]      TITLE")
    async for d in client.iter_dialogs():
        kind = classify(d.entity)
        if args.type == "private" and kind != "private":
            continue
        if args.type == "groups" and kind not in ("group", "supergroup"):
            continue
        if args.type == "channels" and kind != "channel":
            continue
        print(f"{d.id:>12}  [{kind:<9}] {d.title}")
    await client.disconnect()


async def cmd_clean_messages(args):
    """Delete every message sent by YOU, everywhere (or in one chat)."""
    client, _ = await connect(args)
    me = await client.get_me()

    if args.chat:
        targets = [(args.chat, await resolve_chat(client, args.chat))]
    else:
        targets = [(d.title, d) async for d in client.iter_dialogs()]

    mode = "DELETING" if args.yes else "DRY RUN — nothing will be deleted"
    print(f"{mode}: removing messages sent by you "
          f"({me.first_name} @{me.username}) in {len(targets)} chat(s)")
    if args.limit:
        print(f"(limited to {args.limit} messages per chat)")
    print()

    total = total_failed = 0
    for title, entity in targets:
        kwargs = {} if not args.limit else {"limit": args.limit}
        processed, failed = await _sweep(client, entity, title, args.yes,
                                         from_user=me, **kwargs)
        if processed or failed:
            verb = "deleted" if args.yes else "would delete"
            line = f"  {title}: {verb} {processed} message(s)"
            if failed:
                line += f", {failed} failed"
            print(line, flush=True)
        total += processed
        total_failed += failed

    verb = "Deleted" if args.yes else "Would delete"
    print(f"\n{verb} {total} message(s) in total"
          + (f" ({total_failed} failed)" if total_failed else ""))
    if not args.yes and total:
        print("Re-run with --yes to actually delete them.")
    await client.disconnect()


async def cmd_wipe(args):
    """Wipe the FULL history of one chat (both sides where permitted)."""
    client, _ = await connect(args)
    entity = await resolve_chat(client, args.chat)
    label = getattr(entity, "title", None) or getattr(entity, "first_name", None) \
        or args.chat

    mode = "WIPING" if args.yes else "DRY RUN — nothing will be deleted"
    print(f"{mode}: full history of '{label}'")
    processed, failed = await wipe_entity(client, entity, label, args.yes)
    verb = "Wiped" if args.yes else "Would wipe"
    print(f"{verb} {processed} message(s)"
          + (f", {failed} failed" if failed else ""))
    if not args.yes and processed:
        print("Re-run with --yes to actually delete them.")
    if args.yes and processed:
        print("Note: if Telegram cut us off mid-wipe, just re-run the command.")
    await client.disconnect()


async def cmd_leave_all(args):
    """Leave every group and channel (with an optional keep-list)."""
    client, _ = await connect(args)
    me = await client.get_me()

    keep = set()
    for k in args.keep:
        keep.add((await resolve_chat(client, k)).id)

    mode = "LEAVING" if args.yes else "DRY RUN — nothing will be left"
    print(f"{mode}: all groups/channels"
          + (f" (keeping {len(keep)})" if keep else ""))
    if args.include_private:
        print("Private chats will be wiped (history deleted for both sides).")
    print()

    left = kept = failed = wiped = 0
    async for d in client.iter_dialogs():
        ent = d.entity
        if ent is None:
            continue
        kind = classify(ent)

        if kind == "private":
            if not args.include_private or ent.id == me.id:  # skip Saved Messages
                continue
            processed, _ = await wipe_entity(client, ent, d.title, args.yes)
            wiped += processed
            continue

        if kind not in ("group", "supergroup", "channel"):
            continue
        if ent.id in keep:
            kept += 1
            continue

        if isinstance(ent, types.Chat):  # small/basic group
            request = DeleteChatUserRequest(ent.id, me)
        else:  # channel or megagroup
            request = LeaveChannelRequest(ent)

        if args.yes:
            try:
                await rpc(lambda: client(request))
                left += 1
                print(f"  left {d.title}", flush=True)
            except errors.RPCError as e:
                failed += 1
                print(f"  FAILED to leave {d.title}: {e}", flush=True)
        else:
            left += 1
            print(f"  would leave {d.title}", flush=True)

    verb = "Left" if args.yes else "Would leave"
    print(f"\n{verb} {left} group(s)/channel(s), kept {kept}, {failed} failed")
    if wiped:
        print(f"{'Wiped' if args.yes else 'Would wipe'} {wiped} message(s) "
              f"in private chats")
    if not args.yes and left:
        print("Re-run with --yes to actually leave them.")
    await client.disconnect()


async def cmd_delete_account(args):
    """Permanently delete the Telegram account. The nuclear option."""
    client, _ = await connect(args)
    me = await client.get_me()
    name = f"{me.first_name or ''} {me.last_name or ''}".strip()
    print(f"This will PERMANENTLY delete the account of {name} "
          f"(@{me.username}, id {me.id}).")
    print("All your messages, chats, groups and data will be gone. "
          "This CANNOT be undone.")
    if not args.yes:
        print("\nDRY RUN — nothing happened. Re-run with --yes to continue.")
        await client.disconnect()
        return
    typed = input("\nType DELETE to confirm: ")
    if typed.strip() != "DELETE":
        print("Aborted. Nothing was deleted.")
        await client.disconnect()
        return
    await rpc(lambda: client(DeleteAccountRequest(reason=args.reason)))
    print("Account deleted. The session is now invalid.")
    try:
        await client.disconnect()
    except errors.RPCError:
        pass
    # Remove the local session file — it is useless now and sensitive.
    for suffix in (".session", ".session-journal"):
        Path(str(args.session) + suffix).unlink(missing_ok=True)
    print(f"Local session file '{args.session}.session' removed.")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="telegram-remover",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--session", default=os.environ.get("SESSION_PATH", "telegram_remover"),
                   help="session file name (default: telegram_remover)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="log in with your phone number (one-time setup)")

    pd = sub.add_parser("dialogs", help="list your chats")
    pd.add_argument("--type", choices=["all", "private", "groups", "channels"],
                    default="all", help="which chats to show (default: all)")

    pc = sub.add_parser("clean-messages",
                        help="delete every message YOU sent, in all chats "
                             "(dry run unless --yes)")
    pc.add_argument("--chat", help="only this chat (@username, id, or exact title)")
    pc.add_argument("--limit", type=int, default=0,
                    help="max messages per chat (0 = no limit)")
    pc.add_argument("--yes", action="store_true",
                    help="actually delete (default: dry run)")

    pw = sub.add_parser("wipe",
                        help="wipe the FULL history of one chat "
                             "(dry run unless --yes)")
    pw.add_argument("chat", help="@username, id, or exact title")
    pw.add_argument("--yes", action="store_true",
                    help="actually delete (default: dry run)")

    pl = sub.add_parser("leave-all",
                        help="leave all groups and channels "
                             "(dry run unless --yes)")
    pl.add_argument("--keep", action="append", default=[], metavar="CHAT",
                    help="chat to keep, repeatable (@username, id, or title)")
    pl.add_argument("--include-private", action="store_true",
                    help="also wipe private chats (deletes history for both sides)")
    pl.add_argument("--yes", action="store_true",
                    help="actually leave (default: dry run)")

    pda = sub.add_parser("delete-account",
                         help="PERMANENTLY delete your Telegram account")
    pda.add_argument("--reason", default="User requested account deletion",
                     help="reason sent to Telegram")
    pda.add_argument("--yes", action="store_true",
                     help="skip the first confirmation (you must still type DELETE)")
    return p


def main():
    args = build_parser().parse_args()
    handlers = {
        "login": cmd_login,
        "dialogs": cmd_dialogs,
        "clean-messages": cmd_clean_messages,
        "wipe": cmd_wipe,
        "leave-all": cmd_leave_all,
        "delete-account": cmd_delete_account,
    }
    try:
        asyncio.run(handlers[args.command](args))
    except KeyboardInterrupt:
        print("\nInterrupted. Nothing further was deleted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
