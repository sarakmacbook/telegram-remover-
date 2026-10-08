#!/usr/bin/env python3
"""
telegram-remover — remove the shxt from YOUR OWN Telegram account.

A CLI that runs on your account (MTProto user session via Telethon) and can:

  * clean-messages  — delete every message YOU sent, in every chat
  * wipe            — wipe the full history of a single chat
  * leave-all       — leave every group and channel you joined
  * guard           — auto-mod: when somebody joins a group, delete the
                      "X joined the group" message and optionally kick/ban
                      them (needs a typed confirmation for anything that
                      touches a person)
  * delete-account  — permanently delete your Telegram account

The official Bot API cannot do any of this (it can't touch your private
chats, act as you, or leave groups on your behalf), which is why this tool
uses a user session, exactly like Telegram Desktop does.

SAFETY: every destructive command is a DRY RUN unless you pass --yes.
Nothing is deleted until you say so.

The heavy lifting lives in remover_core.py, which is shared with the
Vercel web app (api/*.py + index.html).
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from telethon import TelegramClient, events, types

import guard_core
import remover_core as core
# re-exported for the test-suite (and anyone importing this module)
from remover_core import (BATCH, FloodWait, classify, delete_account,  # noqa: F401
                          delete_ids, resolve_entity, rpc, sweep_chunk)

CHUNK = 200  # messages per sweep chunk (the CLI loops chunks until done)


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


async def connect(args, need_auth=True):
    """Connect the client; abort with a friendly message if not logged in."""
    api_id, api_hash, phone = load_config()
    client = TelegramClient(args.session, api_id, api_hash)
    await client.connect()
    if need_auth and not await client.is_user_authorized():
        await client.disconnect()
        sys.exit("Not logged in. Run first:  python telegram_remover.py login")
    return client, phone


# --------------------------------------------------------------------------
# sweeping (chunked, shared with the web app)
# --------------------------------------------------------------------------

async def _sweep(client, entity, label, yes, from_user=None, limit=None):
    """Delete (or, in dry-run, count) messages in chunks until the chat is done.

    Returns (processed, failed).
    """
    processed = failed = 0
    cursor = 0
    while True:
        remaining = None if limit is None else limit - processed
        if remaining is not None and remaining <= 0:
            break
        chunk = CHUNK if remaining is None else min(CHUNK, remaining)
        res = await sweep_chunk(client, entity, chunk, cursor,
                                from_user=from_user, yes=yes)
        processed += res["processed"]
        failed += res["failed"]
        if processed:
            print(f"\r  {label}: {processed} so far ...", end="", flush=True)
        if res["done"] or res["processed"] == 0:
            break
        cursor = res["next_cursor"]
    if processed:
        print(f"\r  {label}: {processed} so far ...")
    return processed, failed


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
        targets = [(args.chat, await resolve_entity(client, args.chat))]
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
        processed, failed = await _sweep(client, entity, title, args.yes,
                                         from_user=me, limit=args.limit or None)
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
    entity = await resolve_entity(client, args.chat)
    label = getattr(entity, "title", None) or getattr(entity, "first_name", None) \
        or args.chat

    mode = "WIPING" if args.yes else "DRY RUN — nothing will be deleted"
    print(f"{mode}: full history of '{label}'")
    processed, failed = await _sweep(client, entity, label, args.yes)
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
        keep.add((await resolve_entity(client, k)).id)

    mode = "LEAVING" if args.yes else "DRY RUN — nothing will be left"
    print(f"{mode}: all groups/channels"
          + (f" (keeping {len(keep)})" if keep else ""))
    if args.include_private:
        print("Private chats will be wiped (history deleted for both sides).\n")
    else:
        print()

    wiped = 0
    if args.include_private:
        async for d in client.iter_dialogs():
            ent = d.entity
            if ent is None or ent.id == me.id:  # skip Saved Messages
                continue
            if classify(ent) == "private":
                processed, _ = await _sweep(client, ent, d.title, args.yes)
                wiped += processed

    left = failed = 0
    while True:
        res = await core.leave_chats(client, keep, limit=50, yes=args.yes,
                                     me=me)
        left += res["left"]
        failed += res["failed"]
        verb = "left" if args.yes else "would leave"
        print(f"  {verb} {res['left']} in this pass ({left} total) ...",
              flush=True)
        if res["done"]:
            break
        if res["left"] == 0:
            print("  no progress (every remaining chat failed) — stopping.")
            break

    verb = "Left" if args.yes else "Would leave"
    print(f"\n{verb} {left} group(s)/channel(s), {failed} failed")
    if wiped:
        print(f"{'Wiped' if args.yes else 'Would wipe'} {wiped} message(s) "
              f"in private chats")
    if not args.yes and left:
        print("Re-run with --yes to actually leave them.")
    await client.disconnect()


# --------------------------------------------------------------------------
# the join guard (auto-moderation of new members)
# --------------------------------------------------------------------------

def _guard_print(record):
    """Console printer for guard audit records (loud for the scary ones)."""
    print(guard_core.render_record(record), flush=True)
    if record.get("status") == "paused":
        print("  !! The join guard PAUSED itself (circuit breaker). Nobody will "
              "be removed until you re-arm it.", flush=True)


def _guard_policy(args):
    """GuardPolicy from the command-line flags (with sanity checks)."""
    if args.ban_hours and not args.ban_joiner:
        sys.exit("ERROR: --ban-hours only makes sense together with --ban-joiner.")
    if args.purge_joiner_messages and not (args.remove_joiner or args.ban_joiner):
        sys.exit("ERROR: --purge-joiner-messages needs --remove-joiner or "
                 "--ban-joiner (it deletes the messages of members you remove).")
    return guard_core.GuardPolicy(
        delete_join_message=args.delete_join_messages,
        remove_joiner=args.remove_joiner,
        ban_joiner=args.ban_joiner,
        ban_seconds=int((args.ban_hours or 0) * 3600),
        purge_joiner_messages=args.purge_joiner_messages,
        include_added=args.include_added,
        allow=list(args.allow or []),
        protect_admins=not args.no_protect_admins,
        max_actions_per_hour=args.max_actions,
    )


def _guard_arm(policy, args):
    """Return the armed actions (and how they were armed).

    The rule is deliberately blunt: ``kick``/``ban``/``purge`` never run
    because a flag was passed — a human has to type the exact phrase
    ("KICK", "BAN" or "KICK BAN") either at the prompt or with
    ``--confirm-with``. Deleting the join notices just needs ``--yes``.
    """
    armed = {"delete"} if args.yes else set()
    member_intent = set(policy.intent) & set(guard_core.MEMBER_ACTIONS)
    if not member_intent:
        return armed, "nothing member-level is armed"
    confirmation = guard_core.required_confirmation(member_intent)

    how = None
    if args.confirm_with:
        if args.confirm_with.strip() != confirmation:
            sys.exit(f"ERROR: --confirm-with must be exactly {confirmation!r} "
                     f"for the actions you selected.")
        how = "--confirm-with on the command line"
    elif sys.stdin.isatty():
        print("\n" + "=" * 66)
        print("This will REMOVE PEOPLE from your group — not just delete messages.")
        for line in policy.describe():
            print("  " + line)
        print("=" * 66)
        typed = input(f"Type {confirmation} (exactly) to arm it, "
                      f"anything else aborts: ")
        if typed.strip() != confirmation:
            sys.exit("Aborted. Nothing was armed, nothing will be removed.")
        how = "typed at the prompt"
    else:
        sys.exit(
            f"ERROR: --remove-joiner/--ban-joiner/--purge-joiner-messages need a "
            f"human to confirm.\n"
            f"       Run this in a terminal (you will type {confirmation!r}), or "
            f"pass --confirm-with {confirmation!r} if you really are running "
            f"headless."
        )
    armed |= member_intent
    return armed, how


async def cmd_guard(args):
    """Watch a group and act the moment somebody joins (auto-moderation)."""
    client, _ = await connect(args)

    targets = []
    for query in args.chat:
        entity = await resolve_entity(client, query)
        kind = classify(entity)
        if kind not in ("group", "supergroup", "channel"):
            sys.exit(f"ERROR: --chat {query!r} is a {kind} chat, but the join "
                     f"guard only works in groups and channels.")
        targets.append((query, entity))

    policy = _guard_policy(args)
    if args.preview:
        # Previewing is read-only: nothing needs confirming, nothing is armed.
        armed, how = set(), "preview only (nothing is armed)"
    else:
        armed, how = _guard_arm(policy, args)

    guard = guard_core.JoinGuard(policy, dry_run=not args.yes, armed=armed,
                                 audit_path=args.log, on_record=_guard_print)

    mode = "LIVE" if args.yes else "DRY RUN — nothing will be deleted, nobody removed"
    print(f"\n=== JOIN GUARD ({mode}) ===")
    print(f"  confirmation: {how}")
    for line in policy.describe():
        print("  " + line)
    print(f"  armed actions: {', '.join(sorted(armed)) or 'none'}")

    chats = {}
    for query, entity in targets:
        label = getattr(entity, "title", None) or query
        try:
            protected = await guard.prepare(client, entity, label=label)
        except ValueError as e:
            sys.exit(f"ERROR: {e}")
        chats[guard_core.entity_id(entity)] = entity
        chats[getattr(entity, "id", None)] = entity
        print(f"  guarding '{label}' with {len(protected)} protected "
              f"member(s) (you, admins, allow-list)")
    if args.log:
        print(f"  audit log: {args.log}")
    print()

    if args.preview:
        for query, entity in targets:
            label = getattr(entity, "title", None) or query
            print(f"--- what the guard WOULD do to the newest {args.preview} "
                  f"message(s) of '{label}' ---")
            records = await guard.preview(client, entity, limit=args.preview,
                                          label=label)
            if not records:
                print("  (no joins in that window — nothing to do)")
            print()
        print("Preview only: nothing was deleted and nobody was removed.")
        print("Re-run without --preview to watch live.")
        await client.disconnect()
        return

    @client.on(events.Raw(types=(types.UpdateNewMessage,
                                 types.UpdateNewChannelMessage,
                                 types.UpdateChatParticipantAdd,
                                 types.UpdateChannelParticipant)))
    async def _on_update(update):
        try:
            info = guard_core.join_info_from_update(update)
            if not info:
                return
            entity = chats.get(info.get("chat_key"))
            if entity is None:
                return
            await guard.handle(client, entity, info, sleep=True)
        except Exception as e:  # noqa: BLE001 — never kill the watcher loop
            print(f"  guard error: {type(e).__name__}: {e}", flush=True)

    print(f"Watching {len(targets)} chat(s) for new members. "
          f"Press Ctrl+C to stop.")
    if not args.yes:
        print("This is a DRY RUN: you will see what would happen, but nothing "
              "is changed. Re-run with --yes to arm the deletions.")
    await client.run_until_disconnected()


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
    await core.delete_account(client, args.reason)
    print("Account deleted. The session is now invalid.")
    try:
        await client.disconnect()
    except Exception:
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

    pg = sub.add_parser("guard",
                        help="auto-mod: watch for new members and delete the "
                             "'joined' message / remove / ban them "
                             "(dry run unless --yes; removing people always "
                             "needs a typed confirmation)")
    pg.add_argument("--chat", action="append", required=True, metavar="CHAT",
                    help="group to guard, repeatable (@username, id, or exact title)")
    pg.add_argument("--yes", action="store_true",
                    help="actually delete the 'joined' messages (default: dry run)")
    pg.add_argument("--no-delete-join-messages", dest="delete_join_messages",
                    action="store_false",
                    help="do not delete the 'X joined' notices")
    pg.add_argument("--remove-joiner", action="store_true",
                    help="kick every new member (they may rejoin)")
    pg.add_argument("--ban-joiner", action="store_true",
                    help="ban every new member")
    pg.add_argument("--ban-hours", type=float, default=0,
                    help="ban duration in hours (0 = permanent, the default)")
    pg.add_argument("--purge-joiner-messages", action="store_true",
                    help="also delete the messages that joiner already sent here")
    pg.add_argument("--include-added", action="store_true",
                    help="also act when another member adds someone to the group")
    pg.add_argument("--allow", action="append", default=[], metavar="USER",
                    help="never touch this user, repeatable (@username or id)")
    pg.add_argument("--no-protect-admins", action="store_true",
                    help="also act on admins (NOT recommended)")
    pg.add_argument("--max-actions", type=int,
                    default=guard_core.DEFAULT_MAX_ACTIONS_PER_HOUR,
                    help="circuit breaker: pause after this many member actions "
                         "per hour (0 = unlimited; default: "
                         f"{guard_core.DEFAULT_MAX_ACTIONS_PER_HOUR})")
    pg.add_argument("--log", metavar="FILE",
                    help="append every decision to this file as JSONL")
    pg.add_argument("--confirm-with", metavar="TEXT",
                    help="non-interactive confirmation, e.g. --confirm-with BAN")
    pg.add_argument("--preview", type=int, metavar="N", default=0,
                    help="show what the guard would do to the newest N "
                         "messages, then exit (no confirmation needed)")

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
        "guard": cmd_guard,
        "delete-account": cmd_delete_account,
    }
    try:
        asyncio.run(handlers[args.command](args))
    except KeyboardInterrupt:
        print("\nInterrupted. Nothing further was deleted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
