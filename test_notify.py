"""Tests for Telegram Bot API notifications (notify.py) — no network.

Run with:  python -m unittest discover -v
"""

import io
import json
import unittest
import urllib.error

import notify


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class NotifyCase(unittest.TestCase):
    def setUp(self):
        self.sent = []

        def fake_urlopen(request, timeout=None):
            payload = json.loads(request.data.decode("utf-8"))
            self.sent.append((request.full_url, payload, timeout))
            return FakeResponse(b'{"ok": true, "result": {}}')

        self._real = notify.urllib.request.urlopen
        notify.urllib.request.urlopen = fake_urlopen

    def tearDown(self):
        notify.urllib.request.urlopen = self._real


class TestSendMessage(NotifyCase):
    def test_send_posts_to_the_bot_api(self):
        ok = notify.send_message("TOKEN", 42, "hello")
        self.assertTrue(ok)
        self.assertEqual(len(self.sent), 1)
        url, payload, _ = self.sent[0]
        self.assertIn("/botTOKEN/sendMessage", url)
        self.assertEqual(payload["chat_id"], "42")
        self.assertEqual(payload["text"], "hello")

    def test_long_text_is_truncated_to_the_telegram_limit(self):
        notify.send_message("TOKEN", 42, "x" * 5000)
        self.assertEqual(len(self.sent[0][1]["text"]), notify.MAX_LEN)

    def test_missing_pieces_send_nothing(self):
        self.assertFalse(notify.send_message("", 42, "hi"))
        self.assertFalse(notify.send_message("TOKEN", "", "hi"))
        self.assertFalse(notify.send_message("TOKEN", 42, ""))
        self.assertEqual(self.sent, [])

    def test_network_errors_never_raise(self):
        def boom(request, timeout=None):
            raise urllib.error.URLError("nope")

        notify.urllib.request.urlopen = boom
        self.assertFalse(notify.send_message("TOKEN", 42, "hi"))

    def test_api_not_ok_is_a_false_result(self):
        def not_ok(request, timeout=None):
            return FakeResponse(b'{"ok": false, "description": "blocked"}')

        notify.urllib.request.urlopen = not_ok
        self.assertFalse(notify.send_message("TOKEN", 42, "hi"))


class TestNotifier(NotifyCase):
    def test_from_env_builds_an_enabled_notifier(self):
        n = notify.Notifier.from_env({
            "TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_CHAT_ID": "123"})
        self.assertTrue(n.enabled)
        self.assertEqual(n.chat_id, "123")

    def test_from_env_uses_the_first_of_several_chats(self):
        n = notify.Notifier.from_env({
            "TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_CHAT_ID": "1,2"})
        self.assertEqual(n.chat_id, "1")

    def test_disabled_when_unconfigured_or_silenced(self):
        self.assertFalse(notify.Notifier.from_env({}).enabled)
        self.assertFalse(notify.Notifier.from_env({
            "TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_CHAT_ID": "1",
            "TELEGRAM_NOTIFY": "0"}).enabled)
        self.assertFalse(notify.Notifier("", "").enabled)

    def test_send_is_a_noop_when_disabled(self):
        n = notify.Notifier("", "")
        self.assertFalse(n.send("hi"))
        self.assertEqual(self.sent, [])

    def test_send_guard_and_send_run(self):
        n = notify.Notifier("TOKEN", "42")
        self.assertTrue(n.send_guard({
            "status": "acted", "chat_label": "My Group",
            "user": {"id": 7, "first": "Ada", "username": "ada"},
            "results": {"delete": "deleted the notice"},
        }))
        self.assertTrue(n.send_run("wipe", "wiped 5 message(s)"))
        self.assertEqual(len(self.sent), 2)
        self.assertIn("guard ACTED", self.sent[0][1]["text"])
        self.assertIn("@ada", self.sent[0][1]["text"])
        self.assertIn("wipe", self.sent[1][1]["text"])


class TestFormatting(unittest.TestCase):
    def test_format_guard_loud_statuses(self):
        rec = {"status": "acted", "chat_label": "G",
               "user": {"id": 1, "username": "x"},
               "results": {"kick": "removed the member"}}
        text = notify.format_guard(rec)
        self.assertIn("guard ACTED", text)
        self.assertIn("@x", text)
        self.assertIn("removed the member", text)

    def test_format_guard_falls_back_to_the_user_id(self):
        rec = {"status": "error", "chat_key": "-100", "user": {"id": 9},
               "reason": "flood wait 30s"}
        text = notify.format_guard(rec)
        self.assertIn("id 9", text)
        self.assertIn("flood wait 30s", text)

    def test_format_run(self):
        self.assertIn("clean-messages", notify.format_run("clean-messages"))
        self.assertIn("deleted 5", notify.format_run("wipe", "deleted 5"))


if __name__ == "__main__":
    unittest.main()
