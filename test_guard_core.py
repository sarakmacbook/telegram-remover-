"""Tests for the join guard (guard_core) — no real Telegram account needed.

Run with:  python -m unittest discover -v
"""

import asyncio
import json
import os
import tempfile
import unittest
from datetime import timedelta

from telethon import errors, utils
from telethon.tl import types
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.types import channels as tl_channels

import guard_core as guard


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def user(uid, first="Some", last="One", username=None):
    return types.User(id=uid, first_name=first, last_name=last,
                      username=username)


def service(mid, action, sender=None, channel_id=100):
    return types.MessageService(
        id=mid, peer_id=types.PeerChannel(channel_id), date=None,
        action=action, from_id=types.PeerUser(sender) if sender else None)


def plain(mid, sender=None, channel_id=100):
    return types.Message(
        id=mid, peer_id=types.PeerChannel(channel_id), date=None,
        from_id=types.PeerUser(sender) if sender else None)


def supergroup(cid=100, title="My Group"):
    return types.Channel(id=cid, title=title, photo=None, date=None,
                         megagroup=True)


def small_group(cid=50, title="Small Group"):
    return types.Chat(id=cid, title=title, photo=None, participants_count=3,
                      date=None, version=0)


class FakeClient:
    """Minimal TelegramClient stand-in: history, deletes, bans, admin list."""

    def __init__(self, messages=(), users=(), admins=(), me_id=1,
                 fail_with=None, chats=()):
        self.messages = list(messages)
        self.chats = list(chats)
        self.users = {u.id: u for u in users}
        self.by_username = {u.username: u for u in users if u.username}
        self.admins = set(admins)
        self.me_id = me_id
        self.fail_with = fail_with
        self.deleted_batches = []   # (entity, ids, revoke)
        self.edits = []             # (entity, user id, until_date, view_messages)
        self.kicks = []             # (entity, user id)
        self.requests = []

    # -- history -----------------------------------------------------------
    def iter_messages(self, entity, from_user=None, limit=None, offset_id=0):
        msgs = sorted(self.messages, key=lambda m: m.id, reverse=True)
        if offset_id:
            msgs = [m for m in msgs if m.id < offset_id]
        if from_user is not None:
            uid = guard._user_id(from_user) or getattr(from_user, "id", None)
            msgs = [m for m in msgs
                    if guard._user_id(getattr(m, "from_id", None)) == uid]
        if limit:
            msgs = msgs[:limit]

        async def gen():
            for m in msgs:
                yield m

        return gen()

    async def disconnect(self):
        return None

    # -- entities ----------------------------------------------------------
    async def get_me(self):
        return user(self.me_id, "Me", "", "me")

    async def get_input_entity(self, uid):
        if int(uid) in self.users:
            return types.InputPeerUser(int(uid), 0)
        raise ValueError(f"no access hash cached for {uid}")

    async def get_entity(self, uid):
        if isinstance(uid, str) and uid.lstrip("@") in self.by_username:
            return self.by_username[uid.lstrip("@")]
        for chat in self.chats:
            if str(uid) in (str(guard.chat_key(chat)),
                            str(getattr(chat, "id", None))):
                return chat
        if int(uid) in self.users:
            return self.users[int(uid)]
        raise ValueError(f"no access hash cached for {uid}")

    def iter_dialogs(self):
        async def gen():
            for u in list(self.users.values()):
                yield types.Dialog(peer=types.PeerUser(u.id), top_message=0,
                                   unread_count=0, unread_mentions_count=0,
                                   unread_reactions_count=0, notify_settings=None)
        return gen()

    # -- writes ------------------------------------------------------------
    async def delete_messages(self, entity, ids, revoke=True):
        if self.fail_with is not None:
            raise self.fail_with
        self.deleted_batches.append((entity, list(ids), revoke))
        gone = set(ids)
        self.messages = [m for m in self.messages if m.id not in gone]
        return None

    async def edit_permissions(self, entity, user, until_date=None, **kwargs):
        if self.fail_with is not None:
            raise self.fail_with
        uid = guard._user_id(user) or getattr(user, "id", user)
        self.edits.append((entity, uid, until_date,
                           kwargs.get("view_messages", True)))
        return None

    async def kick_participant(self, entity, user):
        if self.fail_with is not None:
            raise self.fail_with
        self.kicks.append((entity, guard._user_id(user) or getattr(user, "id", user)))
        return None

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, GetParticipantsRequest):
            parts = [types.ChannelParticipant(user_id=u, date=None)
                     for u in sorted(self.admins)]
            users = [self.users.get(u) or user(u, f"admin{u}") 
                     for u in sorted(self.admins)]
            return tl_channels.ChannelParticipants(
                count=len(parts), participants=parts, chats=[], users=users)
        return None


# --------------------------------------------------------------------------
# join detection
# --------------------------------------------------------------------------

class TestJoinInfo(unittest.TestCase):
    def test_join_by_request_uses_the_service_message_sender(self):
        info = guard.join_info(service(10, types.MessageActionChatJoinedByRequest(),
                                       sender=42))
        self.assertEqual(info["kind"], guard.JOINED_BY_REQUEST)
        self.assertEqual(info["user_ids"], [42])
        self.assertEqual(info["message_id"], 10)
        self.assertEqual(info["chat_key"], utils.get_peer_id(types.PeerChannel(100)))

    def test_join_by_link(self):
        info = guard.join_info(service(11, types.MessageActionChatJoinedByLink(inviter_id=7),
                                       sender=43))
        self.assertEqual(info["kind"], guard.JOINED_BY_LINK)
        self.assertEqual(info["user_ids"], [43])

    def test_self_join_via_username(self):
        # X added themselves: sender == the only user in the action
        info = guard.join_info(service(12, types.MessageActionChatAddUser(users=[44]),
                                       sender=44))
        self.assertEqual(info["kind"], guard.JOINED)
        self.assertEqual(info["user_ids"], [44])
        self.assertEqual(info["added_ids"], [])

    def test_added_by_another_member(self):
        info = guard.join_info(service(13, types.MessageActionChatAddUser(users=[45, 46]),
                                       sender=44))
        self.assertEqual(info["kind"], guard.ADDED)
        self.assertEqual(sorted(info["user_ids"]), [45, 46])
        self.assertEqual(info["actor_id"], 44)

    def test_other_service_messages_and_plain_messages_are_not_joins(self):
        self.assertIsNone(guard.join_info(
            service(14, types.MessageActionChatEditTitle(title="x"), sender=44)))
        self.assertIsNone(guard.join_info(plain(15, sender=44)))
        self.assertIsNone(guard.join_info(object()))  # no action at all

    def test_join_without_a_known_sender_is_ignored(self):
        self.assertIsNone(guard.join_info(
            service(16, types.MessageActionChatJoinedByRequest())))


class TestJoinInfoFromUpdate(unittest.TestCase):
    def test_service_message_update(self):
        msg = service(20, types.MessageActionChatJoinedByLink(inviter_id=1),
                      sender=42)
        info = guard.join_info_from_update(types.UpdateNewChannelMessage(
            message=msg, pts=1, pts_count=1))
        self.assertEqual(info["kind"], guard.JOINED_BY_LINK)
        self.assertEqual(info["user_ids"], [42])

    def test_plain_message_update(self):
        info = guard.join_info_from_update(types.UpdateNewChannelMessage(
            message=plain(21, sender=42), pts=1, pts_count=1))
        self.assertIsNone(info)

    def test_small_group_member_add_update(self):
        info = guard.join_info_from_update(types.UpdateChatParticipantAdd(
            chat_id=50, user_id=42, inviter_id=7, date=None, version=1))
        self.assertEqual(info["kind"], guard.ADDED)
        self.assertEqual(info["user_ids"], [42])
        self.assertIsNone(info["message_id"])  # no service message to delete
        self.assertEqual(info["chat_key"], utils.get_peer_id(types.PeerChat(50)))

        joined = guard.join_info_from_update(types.UpdateChatParticipantAdd(
            chat_id=50, user_id=42, inviter_id=42, date=None, version=1))
        self.assertEqual(joined["kind"], guard.JOINED)   # they joined by link

    def test_hidden_member_join_and_leave(self):
        join = guard.join_info_from_update(types.UpdateChannelParticipant(
            channel_id=100, date=None, actor_id=42, user_id=42, qts=1,
            prev_participant=None,
            new_participant=types.ChannelParticipant(user_id=42, date=None)))
        self.assertEqual(join["kind"], guard.JOINED)

        added = guard.join_info_from_update(types.UpdateChannelParticipant(
            channel_id=100, date=None, actor_id=7, user_id=42, qts=1,
            prev_participant=None,
            new_participant=types.ChannelParticipant(user_id=42, date=None)))
        self.assertEqual(added["kind"], guard.ADDED)

        left = guard.join_info_from_update(types.UpdateChannelParticipant(
            channel_id=100, date=None, actor_id=42, user_id=42, qts=1,
            prev_participant=types.ChannelParticipant(user_id=42, date=None),
            new_participant=None))
        self.assertIsNone(left)

        banned = guard.join_info_from_update(types.UpdateChannelParticipant(
            channel_id=100, date=None, actor_id=7, user_id=42, qts=1,
            prev_participant=None,
            new_participant=types.ChannelParticipantBanned(
                peer=types.PeerUser(42), kicked_by=7, date=None,
                banned_rights=types.ChatBannedRights(until_date=None,
                                                     view_messages=True))))
        self.assertIsNone(banned)


# --------------------------------------------------------------------------
# policy / confirmation / limiter
# --------------------------------------------------------------------------

class TestPolicy(unittest.TestCase):
    def test_confirmation_phrases(self):
        self.assertEqual(guard.required_confirmation({"delete"}), "")
        self.assertEqual(guard.required_confirmation({"kick"}), "KICK")
        self.assertEqual(guard.required_confirmation({"ban"}), "BAN")
        self.assertEqual(guard.required_confirmation({"kick", "ban"}), "KICK BAN")

    def test_policy_confirmation_and_ban_text(self):
        p = guard.GuardPolicy(ban_joiner=True, ban_seconds=3600)
        self.assertEqual(p.confirmation, "BAN")
        self.assertEqual(p.ban_text, "ban for 1h")
        self.assertIn("ban", p.intent)
        self.assertEqual(guard.GuardPolicy(ban_joiner=True).ban_text,
                         "PERMANENT ban")

    def test_plan_ban_supersedes_kick_and_purge_needs_one_of_them(self):
        info = {"message_id": 5}
        policy = guard.GuardPolicy(remove_joiner=True, ban_joiner=True,
                                   purge_joiner_messages=True)
        plan = guard.plan_actions(policy, {"delete", "kick", "ban", "purge"}, info)
        self.assertNotIn("kick", plan)          # a ban removes them anyway
        self.assertTrue(plan["ban"])
        self.assertTrue(plan["purge"])

        no_member_action = guard.GuardPolicy(purge_joiner_messages=True)
        plan = guard.plan_actions(no_member_action, {"purge"}, info)
        self.assertNotIn("purge", plan)         # purge alone is meaningless

    def test_plan_respects_arming(self):
        policy = guard.GuardPolicy(remove_joiner=True)
        plan = guard.plan_actions(policy, {"delete"}, {"message_id": 5})
        self.assertTrue(plan["delete"])
        self.assertFalse(plan["kick"])
        # no service message (hidden members) -> nothing to delete
        plan = guard.plan_actions(policy, {"delete"}, {"message_id": None})
        self.assertNotIn("delete", plan)

    def test_triggers_respect_include_added(self):
        self.assertNotIn(guard.ADDED, guard.GuardPolicy().triggers)
        self.assertIn(guard.ADDED, guard.GuardPolicy(include_added=True).triggers)


class TestRateLimiter(unittest.TestCase):
    def test_sliding_window(self):
        clock = {"t": 0.0}
        limiter = guard.RateLimiter(2, now=lambda: clock["t"])
        self.assertTrue(limiter.allowed())
        limiter.record()
        limiter.record()
        self.assertEqual(limiter.used(), 2)
        self.assertFalse(limiter.allowed())
        self.assertFalse(limiter.allowed(count=2))
        self.assertGreater(limiter.retry_in(), 0)
        clock["t"] = 3601          # window passed
        self.assertTrue(limiter.allowed())
        self.assertEqual(limiter.used(), 0)

    def test_zero_means_unlimited(self):
        limiter = guard.RateLimiter(0, now=lambda: 0)
        limiter.record(1000)
        self.assertTrue(limiter.allowed())
        self.assertTrue(limiter.allowed(500))


# --------------------------------------------------------------------------
# the guard in action
# --------------------------------------------------------------------------

class GuardCase(unittest.IsolatedAsyncioTestCase):
    def build(self, policy, **kwargs):
        return guard.JoinGuard(policy, **kwargs)

    def join(self, mid=100, uid=42, action=None, kind=None, message_id=True):
        return {
            "kind": kind or guard.JOINED_BY_REQUEST,
            "user_ids": [uid],
            "added_ids": [],
            "message_id": mid if message_id else None,
            "actor_id": uid,
            "chat_key": utils.get_peer_id(types.PeerChannel(100)),
        }


class TestGuardDryRun(GuardCase):
    async def test_dry_run_performs_nothing_but_reports_everything(self):
        client = FakeClient(users=[user(42, username="spammer")],
                            admins=[7])
        entity = supergroup()
        policy = guard.GuardPolicy(remove_joiner=True, ban_joiner=True)
        g = self.build(policy, dry_run=True, armed={"delete", "ban"})
        await g.prepare(client, entity)

        records = await g.handle(client, entity, self.join())
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["status"], "dry_run")
        self.assertEqual(client.deleted_batches, [])   # nothing deleted
        self.assertEqual(client.edits, [])             # nobody banned
        self.assertIn("ban the member", rec["results"]["ban"])
        self.assertEqual(g.limiter.used(), 0)          # no budget consumed
        self.assertIn("would", guard.render_record(rec))

    async def test_unarmed_actions_are_labelled(self):
        client = FakeClient(users=[user(42)])
        entity = supergroup()
        g = self.build(guard.GuardPolicy(remove_joiner=True), dry_run=True,
                       armed={"delete"})
        await g.prepare(client, entity)
        rec = (await g.handle(client, entity, self.join()))[0]
        self.assertIn("NOT ARMED", rec["results"]["kick"])


class TestGuardActions(GuardCase):
    async def test_delete_only(self):
        client = FakeClient(users=[user(42)], admins=[7])
        entity = supergroup()
        g = self.build(guard.GuardPolicy(), dry_run=False, armed={"delete"})
        await g.prepare(client, entity)

        rec = (await g.handle(client, entity, self.join(mid=99)))[0]
        self.assertEqual(rec["status"], "acted")
        self.assertEqual(client.deleted_batches,
                         [(entity, [99], True)])     # revoked for everyone
        self.assertEqual(client.edits, [])

    async def test_permanent_ban(self):
        client = FakeClient(users=[user(42)], admins=[7])
        entity = supergroup()
        g = self.build(guard.GuardPolicy(ban_joiner=True), dry_run=False,
                       armed={"delete", "ban"})
        await g.prepare(client, entity)

        rec = (await g.handle(client, entity, self.join()))[0]
        self.assertEqual(rec["status"], "acted")
        self.assertEqual(len(client.edits), 1)
        _, uid, until, view = client.edits[0]
        self.assertEqual(uid, 42)
        self.assertIsNone(until)                # permanent
        self.assertFalse(view)                  # view_messages=False = ban
        self.assertEqual(g.limiter.used(), 1)

    async def test_temporary_ban(self):
        client = FakeClient(users=[user(42)], admins=[7])
        entity = supergroup()
        g = self.build(guard.GuardPolicy(ban_joiner=True, ban_seconds=3600),
                       dry_run=False, armed={"delete", "ban"})
        await g.prepare(client, entity)
        await g.handle(client, entity, self.join())
        self.assertEqual(client.edits[0][2], timedelta(seconds=3600))

    async def test_kick_in_supergroup_is_ban_then_unban(self):
        client = FakeClient(users=[user(42)], admins=[7])
        entity = supergroup()
        g = self.build(guard.GuardPolicy(remove_joiner=True), dry_run=False,
                       armed={"delete", "kick"})
        await g.prepare(client, entity)
        rec = (await g.handle(client, entity, self.join()))[0]
        self.assertEqual(rec["status"], "acted")
        self.assertEqual([e[3] for e in client.edits], [False, True])

    async def test_kick_in_small_group(self):
        client = FakeClient(users=[user(42)])
        entity = small_group()
        g = self.build(guard.GuardPolicy(remove_joiner=True), dry_run=False,
                       armed={"delete", "kick"})
        await g.prepare(client, entity)
        await g.handle(client, entity, self.join())
        self.assertEqual(client.kicks, [(entity, 42)])
        self.assertEqual(client.edits, [])

    async def test_ban_in_small_group_falls_back_to_removal(self):
        client = FakeClient(users=[user(42)])
        entity = small_group()
        g = self.build(guard.GuardPolicy(ban_joiner=True), dry_run=False,
                       armed={"delete", "ban"})
        await g.prepare(client, entity)
        rec = (await g.handle(client, entity, self.join()))[0]
        self.assertEqual(client.kicks, [(entity, 42)])
        self.assertIn("small groups cannot ban", rec["results"]["ban"])

    async def test_purge_deletes_the_joiners_messages_then_bans(self):
        messages = [plain(1, sender=42), plain(2, sender=42), plain(3, sender=9)]
        client = FakeClient(messages=messages, users=[user(42)], admins=[7])
        entity = supergroup()
        g = self.build(guard.GuardPolicy(ban_joiner=True,
                                         purge_joiner_messages=True),
                       dry_run=False, armed={"delete", "ban", "purge"})
        await g.prepare(client, entity)
        rec = (await g.handle(client, entity, self.join()))[0]
        purged = [sorted(ids) for _, ids, _ in client.deleted_batches]
        self.assertIn([1, 2], purged)           # only user 42's messages
        self.assertEqual(rec["results"]["purge"], "deleted 2 message(s) of theirs")
        self.assertEqual(len(client.edits), 1)  # and the ban happened

    async def test_purge_is_not_armed_by_a_ban_alone(self):
        client = FakeClient(messages=[plain(1, sender=42)], users=[user(42)],
                            admins=[7])
        entity = supergroup()
        g = self.build(guard.GuardPolicy(ban_joiner=True,
                                        purge_joiner_messages=True),
                       dry_run=False, armed={"delete", "ban"})
        await g.prepare(client, entity)
        rec = (await g.handle(client, entity, self.join()))[0]
        self.assertTrue(rec["results"]["purge"].startswith("skipped"))
        self.assertEqual(len(client.edits), 1)

    async def test_admin_and_owner_are_protected(self):
        client = FakeClient(users=[user(7, username="admin7"), user(1, "Me")],
                            admins=[7])
        entity = supergroup()
        g = self.build(guard.GuardPolicy(ban_joiner=True), dry_run=False,
                       armed={"delete", "ban"})
        protected = await g.prepare(client, entity)
        self.assertEqual(protected[7], "admin")
        self.assertEqual(protected[1], "you (the account owner)")

        for uid in (7, 1):
            rec = (await g.handle(client, entity, self.join(uid=uid)))[0]
            self.assertEqual(rec["status"], "protected")
            self.assertIn("never touched", rec["reason"])
        self.assertEqual(client.edits, [])

    async def test_allow_list_protects_members(self):
        client = FakeClient(users=[user(42, username="friend")], admins=[7])
        entity = supergroup()
        policy = guard.GuardPolicy(ban_joiner=True, allow=["@friend"])
        g = self.build(policy, dry_run=False, armed={"delete", "ban"})
        await g.prepare(client, entity)
        rec = (await g.handle(client, entity, self.join()))[0]
        self.assertEqual(rec["status"], "protected")
        self.assertEqual(client.edits, [])

    async def test_unresolvable_allow_list_entry_fails_loudly(self):
        client = FakeClient(users=[], admins=[])
        entity = supergroup()
        g = self.build(guard.GuardPolicy(allow=["@nobody"]), dry_run=False)
        with self.assertRaises(ValueError):
            await g.prepare(client, entity)

    async def test_added_by_someone_else_is_left_alone_by_default(self):
        client = FakeClient(users=[user(42)], admins=[7])
        entity = supergroup()
        g = self.build(guard.GuardPolicy(ban_joiner=True), dry_run=False,
                       armed={"delete", "ban"})
        await g.prepare(client, entity)
        info = self.join(kind=guard.ADDED)
        rec = (await g.handle(client, entity, info))[0]
        self.assertEqual(rec["status"], "ignored")
        self.assertIn("added by someone else", rec["reason"])
        self.assertEqual(client.edits, [])

        g2 = self.build(guard.GuardPolicy(ban_joiner=True, include_added=True),
                        dry_run=False, armed={"delete", "ban"})
        await g2.prepare(client, entity)
        rec = (await g2.handle(client, entity, info))[0]
        self.assertEqual(rec["status"], "acted")
        self.assertEqual(len(client.edits), 1)

    async def test_duplicate_join_updates_are_ignored(self):
        client = FakeClient(users=[user(42)], admins=[7])
        entity = supergroup()
        g = self.build(guard.GuardPolicy(ban_joiner=True), dry_run=False,
                       armed={"delete", "ban"})
        await g.prepare(client, entity)
        self.assertEqual((await g.handle(client, entity, self.join()))[0]["status"],
                         "acted")
        second = (await g.handle(client, entity, self.join()))[0]
        self.assertEqual(second["status"], "ignored")
        self.assertIn("duplicate", second["reason"])
        self.assertEqual(len(client.edits), 1)   # not banned twice

    async def test_circuit_breaker_pauses_instead_of_mass_banning(self):
        client = FakeClient(users=[user(42), user(43), user(44)], admins=[7])
        entity = supergroup()
        g = self.build(guard.GuardPolicy(ban_joiner=True, max_actions_per_hour=2),
                       dry_run=False, armed={"delete", "ban"})
        await g.prepare(client, entity)

        statuses = []
        for uid in (42, 43, 44):
            statuses.append((await g.handle(client, entity, self.join(uid=uid)))[0]["status"])
        self.assertEqual(statuses, ["acted", "acted", "paused"])
        self.assertEqual(len(client.edits), 2)   # the third was NOT banned
        self.assertTrue(g.paused)
        self.assertIn("circuit breaker", g.pause_reason())

    async def test_unresolvable_user_still_deletes_the_notice(self):
        client = FakeClient(users=[], admins=[7])   # user 42 is unknown
        entity = supergroup()
        g = self.build(guard.GuardPolicy(ban_joiner=True), dry_run=False,
                       armed={"delete", "ban"})
        await g.prepare(client, entity)
        rec = (await g.handle(client, entity, self.join()))[0]
        self.assertEqual(client.deleted_batches, [(entity, [100], True)])
        self.assertTrue(rec["results"]["ban"].startswith("error:"))
        self.assertEqual(rec["status"], "acted")   # the delete worked

    async def test_rpc_errors_are_reported_not_raised(self):
        client = FakeClient(users=[user(42)], admins=[7],
                            fail_with=errors.ChatAdminRequiredError(request=None))
        entity = supergroup()
        g = self.build(guard.GuardPolicy(ban_joiner=True), dry_run=False,
                       armed={"delete", "ban"})
        await g.prepare(client, entity)
        rec = (await g.handle(client, entity, self.join()))[0]
        self.assertEqual(rec["status"], "error")
        self.assertIn("error", rec["results"]["ban"])

    async def test_audit_log_is_written(self):
        client = FakeClient(users=[user(42)], admins=[7])
        entity = supergroup()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "audit.jsonl")
            g = self.build(guard.GuardPolicy(), dry_run=False, armed={"delete"},
                           audit_path=path)
            await g.prepare(client, entity)
            await g.handle(client, entity, self.join())
            with open(path, encoding="utf-8") as fh:
                rec = json.loads(fh.readline())
            self.assertEqual(rec["user"]["id"], 42)
            self.assertEqual(rec["status"], "acted")


# --------------------------------------------------------------------------
# batch scanning (the web path) and preview
# --------------------------------------------------------------------------

class TestGuardScan(GuardCase):
    def messages(self):
        return [
            service(8, types.MessageActionChatJoinedByLink(inviter_id=1), sender=42),
            plain(9, sender=9),
            service(10, types.MessageActionChatJoinedByRequest(), sender=43),
        ]

    async def test_baseline_never_acts_on_history(self):
        client = FakeClient(messages=self.messages(), users=[user(42), user(43)],
                            admins=[7])
        entity = supergroup()
        g = self.build(guard.GuardPolicy(ban_joiner=True), dry_run=False,
                       armed={"delete", "ban"})
        await g.prepare(client, entity)
        res = await g.scan(client, entity, cursor=0)
        self.assertTrue(res["baseline"])
        self.assertEqual(res["next_cursor"], 10)      # newest message id
        self.assertEqual(res["records"], [])
        self.assertEqual(client.edits, [])
        self.assertEqual(client.deleted_batches, [])

    async def test_only_messages_newer_than_the_cursor_are_examined(self):
        client = FakeClient(messages=self.messages(), users=[user(42), user(43)],
                            admins=[7])
        entity = supergroup()
        g = self.build(guard.GuardPolicy(), dry_run=False, armed={"delete"})
        await g.prepare(client, entity)

        res = await g.scan(client, entity, cursor=10)
        self.assertEqual(res["records"], [])          # 10 is not > 10
        self.assertEqual(res["next_cursor"], 10)

        client.messages.append(service(11, types.MessageActionChatJoinedByRequest(),
                                      sender=42))
        res = await g.scan(client, entity, cursor=10)
        self.assertEqual([r["user"]["id"] for r in res["records"]], [42])
        self.assertEqual(res["next_cursor"], 11)
        self.assertEqual(res["scanned"], 1)

    async def test_scan_is_resumable_and_does_the_work_once(self):
        client = FakeClient(messages=self.messages(), users=[user(42), user(43)],
                            admins=[7])
        for i in (11, 12, 13):
            client.messages.append(service(
                i, types.MessageActionChatJoinedByRequest(), sender=42 + i))
            client.users[42 + i] = user(42 + i)
        entity = supergroup()
        g = self.build(guard.GuardPolicy(), dry_run=False, armed={"delete"})
        await g.prepare(client, entity)

        res = await g.scan(client, entity, cursor=10)
        self.assertEqual(res["next_cursor"], 13)
        self.assertEqual(len(res["records"]), 3)
        again = await g.scan(client, entity, cursor=res["next_cursor"])
        self.assertEqual(again["records"], [])

    async def test_action_burst_is_capped_and_the_cursor_rewinds(self):
        client = FakeClient(messages=self.messages(), users=[user(42), user(43)],
                            admins=[7])
        entity = supergroup()
        g = self.build(guard.GuardPolicy(ban_joiner=True), dry_run=False,
                       armed={"delete", "ban"})
        await g.prepare(client, entity)

        res = await g.scan(client, entity, cursor=0)      # baseline at 10
        client.messages.append(service(11, types.MessageActionChatJoinedByRequest(),
                                       sender=42))
        client.messages.append(service(12, types.MessageActionChatJoinedByRequest(),
                                       sender=43))
        res = await g.scan(client, entity, cursor=res["next_cursor"], max_actions=1)
        self.assertTrue(res["truncated"])
        self.assertEqual([r["user"]["id"] for r in res["records"]], [42])
        # rewound to just before the join that was NOT handled (id 12), so the
        # next call picks it up and nothing is lost or done twice
        self.assertEqual(res["next_cursor"], 11)
        self.assertEqual(len(client.edits), 1)

        # the next call picks up the join that was left over
        res = await g.scan(client, entity, cursor=res["next_cursor"], max_actions=1)
        self.assertFalse(res["truncated"])
        self.assertEqual([r["user"]["id"] for r in res["records"]], [43])
        self.assertEqual(res["next_cursor"], 12)

    async def test_a_flood_wait_is_reported_and_resumable(self):
        client = FakeClient(messages=self.messages(), users=[user(42)],
                            admins=[7])
        entity = supergroup()
        g = self.build(guard.GuardPolicy(), dry_run=False, armed={"delete"},
                       sleep=False)
        await g.prepare(client, entity)
        client.messages.append(service(11, types.MessageActionChatJoinedByRequest(),
                                      sender=42))
        client.fail_with = errors.FloodWaitError(request=None, capture=30)
        res = await g.scan(client, entity, cursor=10)
        self.assertEqual(res["flood_wait"], 30)
        self.assertEqual(res["next_cursor"], 10)          # retry from here

    async def test_preview_never_writes_and_keeps_the_cursor(self):
        client = FakeClient(messages=self.messages(), users=[user(42), user(43)],
                            admins=[7])
        entity = supergroup()
        g = self.build(guard.GuardPolicy(ban_joiner=True), dry_run=False,
                       armed={"delete", "ban"})
        await g.prepare(client, entity)
        res = await g.scan(client, entity, cursor=10, preview=True)
        self.assertTrue(res["preview"])
        self.assertEqual(res["next_cursor"], 10)          # unchanged
        self.assertEqual(len(res["records"]), 2)          # the joins at id 8 and 10
        self.assertTrue(all(r["status"] == "dry_run" for r in res["records"]))
        self.assertEqual(client.edits, [])
        self.assertEqual(client.deleted_batches, [])

    async def test_scan_dry_run_reports_but_does_not_write(self):
        client = FakeClient(messages=self.messages(), users=[user(42)],
                            admins=[7])
        entity = supergroup()
        g = self.build(guard.GuardPolicy(ban_joiner=True), dry_run=True,
                       armed={"delete", "ban"})
        await g.prepare(client, entity)
        client.messages.append(service(11, types.MessageActionChatJoinedByRequest(),
                                      sender=42))
        res = await g.scan(client, entity, cursor=10)
        self.assertEqual(len(res["records"]), 1)
        self.assertEqual(res["records"][0]["status"], "dry_run")
        self.assertEqual(client.edits, [])
        self.assertFalse(res["truncated"])


# --------------------------------------------------------------------------
# the web endpoint (api_common.handle_guard) — safety gates
# --------------------------------------------------------------------------

class TestGuardApi(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import api_common
        self.api = api_common
        self.client = FakeClient(
            messages=[service(10, types.MessageActionChatJoinedByRequest(),
                              sender=42)],
            users=[user(42, username="spammer")], admins=[7],
            chats=[supergroup()])
        self.chat = str(guard.chat_key(supergroup()))
        self.original = api_common._exec

        def fake_exec(session, api_id, api_hash, work):
            return asyncio.run(work(self.client))

        api_common._exec = fake_exec
        self.addCleanup(setattr, api_common, "_exec", self.original)

    def call(self, **body):
        body.setdefault("chat", self.chat)
        body.setdefault("api_id", "1")
        body.setdefault("api_hash", "h")
        return self.api.handle_guard(body, {"X-Tg-Session": "s"})

    def test_missing_session_is_rejected(self):
        result = self.api.handle_guard({"chat": self.chat},
                                       {})
        self.assertEqual(result[1], 401)

    def test_unknown_armed_action_is_rejected(self):
        result = self.call(armed=["kick", "nuke"])
        self.assertEqual(result[1], 400)
        self.assertIn("nuke", result[0]["error"])

    def test_live_kick_needs_the_confirmation_phrase(self):
        result = self.call(remove_joiner=True, armed=["delete", "kick"],
                           dry_run=False, cursor=0)
        self.assertEqual(result[1], 403)
        self.assertEqual(result[0]["confirm_required"], "KICK")

    def test_live_kick_with_wrong_phrase_is_rejected(self):
        result = self.call(remove_joiner=True, armed=["delete", "kick"],
                           dry_run=False, confirm="ban", cursor=0)
        self.assertEqual(result[1], 403)

    def test_live_kick_with_the_phrase_acts(self):
        result = self.call(remove_joiner=True, armed=["delete", "kick"],
                           dry_run=False, confirm="KICK", cursor=10)
        self.assertEqual(result["next_cursor"], 10)  # no messages newer than 10
        self.assertEqual(self.client.edits, [])      # nothing to do yet

        self.client.messages.append(service(
            11, types.MessageActionChatJoinedByRequest(), sender=42))
        result = self.call(remove_joiner=True, armed=["delete", "kick"],
                           dry_run=False, confirm="KICK", cursor=10)
        self.assertEqual(result["summary"]["acted"], 1)
        self.assertEqual([e[3] for e in self.client.edits], [False, True])
        self.assertEqual(result["next_cursor"], 11)
        self.assertTrue(result["records"][0]["text"])

    def test_dry_run_needs_no_confirmation_and_writes_nothing(self):
        result = self.call(ban_joiner=True, armed=["delete", "ban"],
                           dry_run=True, cursor=0)
        self.assertTrue(result["baseline"])
        self.assertEqual(result["confirm_required"] if "confirm_required" in result
                         else result["confirmation"], "BAN")
        self.assertEqual(self.client.edits, [])

        result = self.call(ban_joiner=True, armed=["delete", "ban"],
                           dry_run=True, cursor=5)
        self.assertEqual(result["summary"]["dry_run"], 1)
        self.assertEqual(self.client.edits, [])
        self.assertEqual(self.client.deleted_batches, [])

    def test_preview_never_writes_and_needs_no_confirmation(self):
        result = self.call(ban_joiner=True, armed=["delete", "ban"],
                           preview=True, cursor=0)
        self.assertTrue(result["preview"])
        self.assertEqual(result["next_cursor"], 0)
        self.assertEqual(result["summary"]["dry_run"], 1)
        self.assertEqual(self.client.edits, [])

    def test_baseline_reports_the_newest_message(self):
        result = self.call(armed=["delete"], dry_run=False, cursor=0)
        self.assertTrue(result["baseline"])
        self.assertEqual(result["next_cursor"], 10)
        self.assertIn("policy", result)
        self.assertEqual(result["policy"]["intent"], ["delete"])

    def test_the_api_guard_wrapper_runs_end_to_end(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "api_guard", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "api", "guard.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        res = module.app.test_client().post(
            "/api/guard",
            json={"chat": self.chat, "api_id": "1", "api_hash": "h",
                  "cursor": 0, "armed": ["delete"], "dry_run": False},
            headers={"X-Tg-Session": "s"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["baseline"])

    def test_a_flood_wait_is_returned_for_the_browser_to_retry(self):
        self.client.messages.append(service(
            11, types.MessageActionChatJoinedByRequest(), sender=42))
        self.client.fail_with = errors.FloodWaitError(request=None, capture=15)
        result = self.call(armed=["delete"], dry_run=False, cursor=10)
        self.assertEqual(result["flood_wait"], 15)
        self.assertEqual(result["next_cursor"], 10)   # retry from here


if __name__ == "__main__":
    unittest.main()
