"""Unit tests for telegram_remover — no real Telegram account needed.

Run with:  python -m unittest test_telegram_remover -v
"""

import unittest
from unittest.mock import AsyncMock

from telethon import errors

import telegram_remover as tr


def rpc_error():
    """A generic RPC error (Telethon's RPCError requires a message arg)."""
    return errors.RPCError(request=None, message="boom")


class FakeMessage:
    """Just enough of a Message for the sweep helpers (they only use .id)."""

    def __init__(self, id):
        self.id = id


class FakeClient:
    """Minimal stand-in for TelegramClient used by the sweep/delete helpers."""

    def __init__(self, message_ids, fail_with=None):
        self.message_ids = message_ids
        self.fail_with = fail_with
        self.deleted_batches = []  # list of (ids, revoke)

    def iter_messages(self, entity, from_user=None, limit=None):
        ids = self.message_ids
        if from_user is not None:
            ids = [i for i in ids if i % 2 == 0]  # pretend only even ids are "mine"
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
        return None


class TestRpc(unittest.IsolatedAsyncioTestCase):
    async def test_retries_after_flood_wait(self):
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise errors.FloodWaitError(request=None, capture=5)
            return "ok"

        # don't actually sleep 5s in the test
        real_sleep = tr.asyncio.sleep
        tr.asyncio.sleep = AsyncMock()
        try:
            result = await tr.rpc(flaky)
        finally:
            tr.asyncio.sleep = real_sleep
        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 2)


class TestDeleteIds(unittest.IsolatedAsyncioTestCase):
    async def test_success(self):
        client = FakeClient([])
        failed = await tr.delete_ids(client, "chat", [1, 2, 3])
        self.assertEqual(failed, 0)
        self.assertEqual(client.deleted_batches, [([1, 2, 3], True)])

    async def test_admin_required_falls_back_to_revoke_false(self):
        client = FakeClient(
            [], fail_with=errors.ChatAdminRequiredError(request=None))
        failed = await tr.delete_ids(client, "chat", [1, 2], revoke=True)
        self.assertEqual(failed, 2)  # fallback also fails -> all counted failed
        self.assertEqual(client.deleted_batches, [])  # every attempt raised

    async def test_admin_required_fallback_succeeds_when_revoke_false(self):
        attempts = {"n": 0}

        class FlakyClient(FakeClient):
            async def delete_messages(self, entity, ids, revoke=True):
                attempts["n"] += 1
                if revoke:
                    raise errors.ChatAdminRequiredError(request=None)
                self.deleted_batches.append((list(ids), revoke))

        client = FlakyClient([])
        failed = await tr.delete_ids(client, "chat", [7, 8], revoke=True)
        self.assertEqual(failed, 0)
        self.assertEqual(client.deleted_batches, [([7, 8], False)])
        self.assertEqual(attempts["n"], 2)

    async def test_generic_rpc_error_counts_all_failed(self):
        client = FakeClient([], fail_with=rpc_error())
        failed = await tr.delete_ids(client, "chat", [1, 2, 3, 4], revoke=False)
        self.assertEqual(failed, 4)


class TestSweep(unittest.IsolatedAsyncioTestCase):
    async def test_batches_of_100_and_deletes_only_own_messages(self):
        ids = list(range(1, 251))  # 250 messages
        client = FakeClient(ids)
        processed, failed = await tr._sweep(
            client, "chat", "label", yes=True, from_user="me")
        # only "my" messages (even ids) are swept: 125 of them
        self.assertEqual(processed, 125)
        self.assertEqual(failed, 0)
        batch_sizes = [len(b[0]) for b in client.deleted_batches]
        self.assertEqual(batch_sizes, [100, 25])
        all_ids = [i for batch, _ in client.deleted_batches for i in batch]
        self.assertTrue(all(i % 2 == 0 for i in all_ids))
        self.assertTrue(all(revoke for _, revoke in client.deleted_batches))

    async def test_dry_run_deletes_nothing(self):
        client = FakeClient(list(range(1, 11)))
        processed, failed = await tr._sweep(
            client, "chat", "label", yes=False, from_user="me")
        self.assertEqual(processed, 5)  # counted, not deleted
        self.assertEqual(client.deleted_batches, [])

    async def test_limit_is_respected(self):
        client = FakeClient(list(range(1, 1001)))
        processed, _ = await tr._sweep(
            client, "chat", "label", yes=True, from_user="me", limit=10)
        # the fake yields at most `limit` of "my" messages
        self.assertEqual(processed, 10)
        total_deleted = sum(len(b[0]) for b in client.deleted_batches)
        self.assertEqual(total_deleted, 10)

    async def test_failures_are_counted(self):
        client = FakeClient(list(range(2, 202)), fail_with=rpc_error())
        processed, failed = await tr._sweep(
            client, "chat", "label", yes=True, from_user="me")
        self.assertEqual(processed, 100)
        self.assertEqual(failed, 100)


class TestClassify(unittest.TestCase):
    def test_classify(self):
        from telethon.tl.types import Channel, Chat, User
        self.assertEqual(tr.classify(User(id=1)), "private")
        self.assertEqual(
            tr.classify(Channel(id=1, title="t", photo=None, date=None,
                                megagroup=True)), "supergroup")
        self.assertEqual(
            tr.classify(Channel(id=1, title="t", photo=None, date=None,
                                megagroup=False)), "channel")
        self.assertEqual(
            tr.classify(Chat(id=1, title="t", photo=None, participants_count=0,
                             date=None, version=0)), "group")


class TestParser(unittest.TestCase):
    def test_defaults_are_safe(self):
        args = tr.build_parser().parse_args(["clean-messages"])
        self.assertFalse(args.yes)  # dry run by default
        args = tr.build_parser().parse_args(["leave-all"])
        self.assertFalse(args.yes)
        self.assertFalse(args.include_private)
        args = tr.build_parser().parse_args(["wipe", "@somechat"])
        self.assertFalse(args.yes)

    def test_keep_is_repeatable(self):
        args = tr.build_parser().parse_args(
            ["leave-all", "--keep", "@a", "--keep", "@b"])
        self.assertEqual(args.keep, ["@a", "@b"])


if __name__ == "__main__":
    unittest.main()
