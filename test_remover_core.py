"""Tests for remover_core (chunked sweeps, leave, account deletion) and the
Vercel API handlers in api_common — no real Telegram account needed.

Run with:  python -m unittest discover -v
"""

import asyncio
import importlib.util
import os
import unittest
from types import SimpleNamespace

from flask import Flask
from telethon import errors
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.functions.messages import DeleteChatUserRequest
from telethon.tl.types import Channel, Chat, User

import api_common
import remover_core as core

ROOT = os.path.dirname(os.path.abspath(__file__))


def rpc_error():
    return errors.RPCError(request=None, message="boom")


class FakeMessage:
    def __init__(self, id):
        self.id = id


class FakeEntity:
    def __init__(self, id, title="chat"):
        self.id = id
        self.title = title


class FakeDialog:
    def __init__(self, entity, title=None):
        self.entity = entity
        self.title = title if title is not None else getattr(entity, "title", "")
        self.id = entity.id


class FakeClient:
    """Stand-in for TelegramClient: newest-first history, real deletions,
    dialog iteration, and request recording for leave/delete-account."""

    def __init__(self, message_ids=(), dialogs=(), fail_with=None):
        self.message_ids = list(message_ids)
        self.dialogs = list(dialogs)
        self.fail_with = fail_with
        self.deleted_batches = []
        self.requests = []

    # -- history --
    def iter_messages(self, entity, from_user=None, limit=None, offset_id=0):
        ids = sorted(self.message_ids, reverse=True)
        if offset_id:
            ids = [i for i in ids if i < offset_id]
        if from_user is not None:
            ids = [i for i in ids if i % 2 == 0]
        if limit:
            ids = ids[:limit]

        async def gen():
            for i in ids:
                yield FakeMessage(i)

        return gen()

    async def delete_messages(self, entity, ids, revoke=True):
        if self.fail_with is not None:
            raise self.fail_with
        self.deleted_batches.append((list(ids), revoke))
        self.message_ids = [i for i in self.message_ids if i not in ids]

    # -- dialogs / entities --
    def iter_dialogs(self):
        async def gen():
            for d in self.dialogs:
                yield d

        return gen()

    async def get_entity(self, query):
        try:
            return FakeEntity(int(query))
        except (TypeError, ValueError):
            return FakeEntity(999, title=str(query))

    async def get_me(self):
        return User(id=7, first_name="Test", username="tester")

    # -- raw requests (leave / delete account) --
    async def __call__(self, request):
        self.requests.append(request)
        # Leaving a chat removes it from the dialog list, like real Telegram.
        if isinstance(request, LeaveChannelRequest):
            cid = getattr(request.channel, "id", request.channel)
            self.dialogs = [d for d in self.dialogs if d.entity.id != cid]
        elif isinstance(request, DeleteChatUserRequest):
            self.dialogs = [d for d in self.dialogs
                            if d.entity.id != request.chat_id]
        return None


def channel(id, megagroup=True):
    return Channel(id=id, title=f"ch{id}", photo=None, date=None,
                   megagroup=megagroup)


def basic_group(id):
    return Chat(id=id, title=f"gr{id}", photo=None, participants_count=5,
                date=None, version=1)


# --------------------------------------------------------------------------
# sweep_chunk
# --------------------------------------------------------------------------

class TestSweepChunk(unittest.IsolatedAsyncioTestCase):
    async def test_chunk_limit_and_done_flag(self):
        client = FakeClient(list(range(1, 13)))  # 12 messages
        res = await core.sweep_chunk(client, "e", limit=5)
        self.assertEqual(res["processed"], 5)
        self.assertEqual(res["deleted"], 5)
        self.assertFalse(res["done"])
        self.assertEqual(res["next_cursor"], 8)  # ids 12..8 deleted
        self.assertEqual(sorted(client.message_ids), list(range(1, 8)))

        res2 = await core.sweep_chunk(client, "e", limit=5, cursor=res["next_cursor"])
        self.assertEqual(res2["deleted"], 5)
        self.assertEqual(sorted(client.message_ids), [1, 2])

        res3 = await core.sweep_chunk(client, "e", limit=5, cursor=res2["next_cursor"])
        self.assertEqual(res3["deleted"], 2)
        self.assertTrue(res3["done"])
        self.assertEqual(client.message_ids, [])

    async def test_from_user_filters(self):
        client = FakeClient(list(range(1, 11)))
        res = await core.sweep_chunk(client, "e", limit=100, from_user="me")
        self.assertEqual(res["processed"], 5)  # only even ids
        self.assertEqual(sorted(client.message_ids),
                         [1, 3, 5, 7, 9])  # odd ids untouched

    async def test_dry_run_counts_but_deletes_nothing(self):
        client = FakeClient(list(range(1, 8)))
        res = await core.sweep_chunk(client, "e", limit=100, yes=False)
        self.assertEqual(res["processed"], 7)
        self.assertEqual(res["deleted"], 0)
        self.assertEqual(client.deleted_batches, [])
        self.assertEqual(len(client.message_ids), 7)

    async def test_failures_are_counted(self):
        client = FakeClient(list(range(1, 6)), fail_with=rpc_error())
        res = await core.sweep_chunk(client, "e", limit=100)
        self.assertEqual(res["deleted"], 0)
        self.assertEqual(res["failed"], 5)

    async def test_admin_required_falls_back_to_revoke_false(self):
        class Flaky(FakeClient):
            async def delete_messages(self, entity, ids, revoke=True):
                if revoke:
                    raise errors.ChatAdminRequiredError(request=None)
                await super().delete_messages(entity, ids, revoke=revoke)

        client = Flaky(list(range(1, 4)))
        res = await core.sweep_chunk(client, "e", limit=100)
        self.assertEqual(res["deleted"], 3)
        self.assertEqual(res["failed"], 0)
        self.assertTrue(all(revoke is False
                            for _, revoke in client.deleted_batches))

    async def test_flood_wait_raises_when_sleep_false(self):
        client = FakeClient(list(range(1, 4)),
                            fail_with=errors.FloodWaitError(request=None,
                                                            capture=30))
        with self.assertRaises(core.FloodWait) as ctx:
            await core.sweep_chunk(client, "e", limit=100, sleep=False)
        self.assertEqual(ctx.exception.seconds, 30)


# --------------------------------------------------------------------------
# leave_chats
# --------------------------------------------------------------------------

class TestLeaveChats(unittest.IsolatedAsyncioTestCase):
    def make_client(self):
        return FakeClient(dialogs=[
            FakeDialog(channel(100, megagroup=True)),    # supergroup
            FakeDialog(channel(200, megagroup=False)),   # broadcast channel
            FakeDialog(basic_group(300)),                # basic group
            FakeDialog(User(id=400, first_name="Dm")),   # private — skipped
        ])

    async def test_leaves_groups_and_channels_skips_private(self):
        client = self.make_client()
        res = await core.leave_chats(client, keep_ids=set(), limit=10)
        self.assertEqual(res["left"], 3)
        self.assertEqual(res["failed"], 0)
        self.assertTrue(res["done"])
        kinds = [type(r) for r in client.requests]
        self.assertEqual(kinds, [LeaveChannelRequest, LeaveChannelRequest,
                                 DeleteChatUserRequest])

    async def test_keep_list_is_respected(self):
        client = self.make_client()
        res = await core.leave_chats(client, keep_ids={100, 300}, limit=10)
        self.assertEqual(res["left"], 1)
        self.assertEqual(len(client.requests), 1)
        self.assertIsInstance(client.requests[0], LeaveChannelRequest)

    async def test_limit_and_done_flag(self):
        client = self.make_client()
        res = await core.leave_chats(client, keep_ids=set(), limit=2)
        self.assertEqual(res["left"], 2)
        self.assertFalse(res["done"])  # one eligible chat remains
        res2 = await core.leave_chats(client, keep_ids=set(), limit=2)
        self.assertEqual(res2["left"], 1)
        self.assertTrue(res2["done"])

    async def test_dry_run_leaves_nothing(self):
        client = self.make_client()
        res = await core.leave_chats(client, keep_ids=set(), limit=10, yes=False)
        self.assertEqual(res["left"], 3)
        self.assertEqual(client.requests, [])


# --------------------------------------------------------------------------
# API handlers
# --------------------------------------------------------------------------

class TestHandlers(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.ctx = self.app.test_request_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def patch_exec(self, client):
        """Replace api_common._exec so handlers run against a FakeClient."""
        def fake_exec(session, api_id, api_hash, work):
            return asyncio.run(work(client))
        self.addCleanup(setattr, api_common, "_exec", api_common._exec)
        api_common._exec = fake_exec

    # -- validation --

    def test_clean_requires_session(self):
        body, status = api_common.handle_clean(
            {"chat": "@x", "api_id": "1", "api_hash": "h"}, {})
        self.assertEqual(status, 401)

    def test_clean_requires_chat(self):
        body, status = api_common.handle_clean(
            {"api_id": "1", "api_hash": "h"}, {"X-Tg-Session": "s"})
        self.assertEqual(status, 400)
        self.assertIn("chat", body["error"])

    def test_clean_requires_api_creds(self):
        os.environ.pop("API_ID", None)
        os.environ.pop("API_HASH", None)
        body, status = api_common.handle_clean(
            {"chat": "@x"}, {"X-Tg-Session": "s"})
        self.assertEqual(status, 400)
        self.assertIn("API_ID", body["error"])

    def test_leave_rejects_bad_keep(self):
        body, status = api_common.handle_leave(
            {"keep": "@a", "api_id": "1", "api_hash": "h"},
            {"X-Tg-Session": "s"})
        self.assertEqual(status, 400)

    # -- happy paths (with a FakeClient behind _exec) --

    def test_clean_handler(self):
        client = FakeClient(list(range(1, 8)))
        self.patch_exec(client)
        result = api_common.handle_clean(
            {"chat": "123", "api_id": "1", "api_hash": "h", "limit": 3},
            {"X-Tg-Session": "s"})
        self.assertEqual(result["processed"], 3)
        self.assertEqual(result["deleted"], 3)
        self.assertFalse(result["done"])
        # the fake treats even ids as "mine": 6, 4, 2 -> cursor is the min
        self.assertEqual(result["next_cursor"], 2)

    def test_wipe_handler_deletes_everything(self):
        client = FakeClient(list(range(1, 6)))
        self.patch_exec(client)
        result = api_common.handle_wipe(
            {"chat": "123", "api_id": "1", "api_hash": "h"},
            {"X-Tg-Session": "s"})
        self.assertEqual(result["deleted"], 5)
        self.assertTrue(result["done"])

    def test_leave_handler_resolves_keep_list(self):
        client = FakeClient(dialogs=[
            FakeDialog(channel(100)),
            FakeDialog(channel(200)),
        ])
        self.patch_exec(client)
        result = api_common.handle_leave(
            {"keep": ["555"], "api_id": "1", "api_hash": "h"},
            {"X-Tg-Session": "s"})
        # FakeClient.get_entity("555") -> FakeEntity(555), not in dialogs
        self.assertEqual(result["left"], 2)
        self.assertTrue(result["done"])

    def test_delete_account_handler(self):
        client = FakeClient()
        self.patch_exec(client)
        result = api_common.handle_delete_account(
            {"api_id": "1", "api_hash": "h"}, {"X-Tg-Session": "s"})
        self.assertEqual(result, {"deleted": True})
        self.assertEqual(len(client.requests), 1)

    def test_dialogs_handler(self):
        client = FakeClient(dialogs=[
            FakeDialog(channel(100), "News"),
            FakeDialog(User(id=400, first_name="Dm"), "Dm"),
        ])
        self.patch_exec(client)
        result = api_common.handle_dialogs(
            {"X-Tg-Session": "s"}, {"api_id": "1", "api_hash": "h"})
        kinds = {d["title"]: d["kind"] for d in result["dialogs"]}
        self.assertEqual(kinds, {"News": "supergroup", "Dm": "private"})

    # -- auth handlers (with a fake TelegramClient) --

    def patch_client(self, cls):
        self.addCleanup(setattr, api_common, "TelegramClient",
                        api_common.TelegramClient)
        api_common.TelegramClient = cls

    def test_auth_start_returns_phone_code_hash(self):
        class FakeTG:
            def __init__(self, session, api_id, api_hash):
                self.session = SimpleNamespace(save=lambda: "SESSION_STRING")

            async def connect(self): pass
            async def disconnect(self): pass

            async def send_code_request(self, phone):
                return SimpleNamespace(phone_code_hash="HASH123")

        self.patch_client(FakeTG)
        result = api_common.handle_auth_start(
            {"phone": "+855123", "api_id": "1", "api_hash": "h"})
        self.assertEqual(result, {"phone_code_hash": "HASH123"})

    def test_auth_finish_needs_password(self):
        class FakeTG:
            def __init__(self, session, api_id, api_hash):
                self.session = SimpleNamespace(save=lambda: "SESSION_STRING")

            async def connect(self): pass
            async def disconnect(self): pass

            async def sign_in(self, phone=None, code=None,
                              phone_code_hash=None, password=None):
                if password is None:
                    raise errors.SessionPasswordNeededError(request=None)

        self.patch_client(FakeTG)
        result = api_common.handle_auth_finish(
            {"phone": "+855123", "code": "123", "phone_code_hash": "h",
             "api_id": "1", "api_hash": "h"})
        self.assertEqual(result, {"need_password": True})

    def test_auth_finish_returns_session(self):
        class FakeTG:
            def __init__(self, session, api_id, api_hash):
                self.session = SimpleNamespace(save=lambda: "SESSION_STRING")

            async def connect(self): pass
            async def disconnect(self): pass

            async def sign_in(self, phone=None, code=None,
                              phone_code_hash=None, password=None):
                if password is None:
                    raise errors.SessionPasswordNeededError(request=None)

            async def get_me(self):
                return User(id=7, first_name="Test", username="tester")

        self.patch_client(FakeTG)
        result = api_common.handle_auth_finish(
            {"phone": "+855123", "code": "123", "phone_code_hash": "h",
             "password": "secret", "api_id": "1", "api_hash": "h"})
        self.assertEqual(result["session"], "SESSION_STRING")
        self.assertEqual(result["me"]["username"], "tester")

    # -- access-token guard --

    def test_guard(self):
        os.environ.pop("ACCESS_TOKEN", None)
        self.assertIsNone(api_common.guard())
        os.environ["ACCESS_TOKEN"] = "secret"
        try:
            with self.app.test_request_context(
                    headers={"X-Access-Token": "wrong"}):
                body, status = api_common.guard()
                self.assertEqual(status, 401)
            with self.app.test_request_context(
                    headers={"X-Access-Token": "secret"}):
                self.assertIsNone(api_common.guard())
        finally:
            os.environ.pop("ACCESS_TOKEN", None)


# --------------------------------------------------------------------------
# api/*.py wrapper smoke tests (real files, fake backend)
# --------------------------------------------------------------------------

class TestApiWrapper(unittest.TestCase):
    def test_clean_wrapper_end_to_end(self):
        spec = importlib.util.spec_from_file_location(
            "api_clean", os.path.join(ROOT, "api", "clean.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        client = FakeClient(list(range(1, 6)))

        def fake_exec(session, api_id, api_hash, work):
            return asyncio.run(work(client))

        original = api_common._exec
        api_common._exec = fake_exec
        try:
            flask_client = module.app.test_client()
            res = flask_client.post(
                "/api/clean",
                json={"chat": "1", "api_id": "1", "api_hash": "h"},
                headers={"X-Tg-Session": "s"})
        finally:
            api_common._exec = original

        self.assertEqual(res.status_code, 200)
        # clean only touches "my" messages; the fake marks even ids as mine
        self.assertEqual(res.get_json()["deleted"], 2)

    def test_index_wrapper_serves_html(self):
        spec = importlib.util.spec_from_file_location(
            "api_index", os.path.join(ROOT, "api", "index.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        res = module.app.test_client().get("/api")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"telegram", res.data.lower())


if __name__ == "__main__":
    unittest.main()
