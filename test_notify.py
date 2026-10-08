"""Tests for Telegram Bot API notifications (notify.py) — no network.

Run with:  python -m unittest discover -v
"""

import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

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


class TestBotApiCall(NotifyCase):
    def test_bot_token_shape_validation(self):
        self.assertTrue(notify.valid_bot_token("123456789:ABC_def-123"))
        self.assertFalse(notify.valid_bot_token("no-colon-token"))
        self.assertFalse(notify.valid_bot_token("123456789:bad/token"))

    def test_calls_the_named_method_and_returns_result(self):
        result = notify.bot_api_call("TOKEN", "getMe", {"sample": True})
        self.assertEqual(result, {})
        self.assertEqual(len(self.sent), 1)
        url, payload, timeout = self.sent[0]
        self.assertIn("/botTOKEN/getMe", url)
        self.assertEqual(payload, {"sample": True})
        self.assertEqual(timeout, 10)

    def test_custom_bot_api_host_is_used(self):
        with patch.dict(os.environ, {
                "TELEGRAM_API_BASE": "https://bot-api.example/base/"}):
            notify.bot_api_call("TOKEN", "getMe")
        self.assertEqual(self.sent[0][0],
                         "https://bot-api.example/base/botTOKEN/getMe")

    def test_invalid_bot_api_host_is_rejected_without_a_request(self):
        with patch.dict(os.environ, {"TELEGRAM_API_BASE": "not-a-url"}):
            with self.assertRaises(notify.BotAPIError) as raised:
                notify.bot_api_call("TOKEN", "getMe")
        self.assertIn("TELEGRAM_API_BASE", str(raised.exception))
        self.assertEqual(self.sent, [])

    def test_telegram_error_description_is_reported_without_url(self):
        def not_ok(request, timeout=None):
            return FakeResponse(b'{"ok": false, "description": "Unauthorized"}')

        notify.urllib.request.urlopen = not_ok
        with self.assertRaises(notify.BotAPIError) as raised:
            notify.bot_api_call("TOKEN", "getMe")
        self.assertIn("Unauthorized", str(raised.exception))
        self.assertNotIn("TOKEN", str(raised.exception))


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


class TestBotHelpers(unittest.TestCase):
    """Shape checks and hints the web UI shows for connect problems."""

    def test_bot_token_problem_accepts_a_real_looking_token(self):
        self.assertIsNone(notify.bot_token_problem(
            "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"))

    def test_bot_token_problem_explains_common_mistakes(self):
        self.assertIn("required", notify.bot_token_problem(""))
        self.assertIn("URL", notify.bot_token_problem(
            "https://api.telegram.org/bot123456789:AAHdqTcvCH/getMe"))
        self.assertIn("\"bot\"", notify.bot_token_problem(
            "bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"))
        self.assertIn("colon", notify.bot_token_problem("123456789"))

    def test_chat_id_problem_explains_common_mistakes(self):
        self.assertIn("required", notify.chat_id_problem(""))
        self.assertIn("phone number", notify.chat_id_problem("+15551234567"))
        self.assertIn("single ID", notify.chat_id_problem("1, 2"))
        self.assertIn("single ID", notify.chat_id_problem("1 2"))
        self.assertIn("invalid", notify.chat_id_problem("x" * 200))

    def test_chat_id_problem_accepts_ids_and_usernames(self):
        for good in ("987654321", "-1001234567890", "@mychannel"):
            self.assertIsNone(notify.chat_id_problem(good), good)

    def test_bot_error_hint_maps_telegram_rejections(self):
        self.assertIn("userinfobot", notify.bot_error_hint("Bad Request: chat not found"))
        self.assertIn("/start", notify.bot_error_hint(
            "Forbidden: bot can't initiate conversation with a user"))
        self.assertIn("unblock", notify.bot_error_hint(
            "Forbidden: bot was blocked by the user"))
        self.assertIn("administrator", notify.bot_error_hint(
            "Bad Request: not enough rights"))
        self.assertIn("rate-limited", notify.bot_error_hint(
            "Too Many Requests: retry after 5"))
        self.assertEqual(notify.bot_error_hint("something else"), "")

    def test_api_host_never_contains_the_token(self):
        self.assertEqual(notify.api_host("https://api.telegram.org"),
                         "api.telegram.org")
        self.assertEqual(notify.api_host("http://localhost:8081"),
                         "localhost:8081")
        with patch.dict(os.environ, {"TELEGRAM_API_BASE": "not-a-url"}):
            self.assertIn("TELEGRAM_API_BASE", notify.api_host())


class TestBotApiErrors(NotifyCase):
    def test_http_errors_keep_the_status_and_retry_after(self):
        def too_many(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 429, "Too Many Requests", {},
                io.BytesIO(b'{"ok": false, "description": "Too Many Requests",'
                           b' "parameters": {"retry_after": 7}}'))

        notify.urllib.request.urlopen = too_many
        with self.assertRaises(notify.BotAPIError) as raised:
            notify.bot_api_call("123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
                                "sendMessage")
        self.assertEqual(raised.exception.http_status, 429)
        self.assertFalse(raised.exception.network)
        self.assertIn("retry after 7s", str(raised.exception))
        self.assertNotIn("123456789:AAHdqTcvCH", str(raised.exception))

    def test_http_error_without_a_body_names_the_host(self):
        def broken(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 502, "Bad Gateway",
                                         {}, io.BytesIO(b""))

        notify.urllib.request.urlopen = broken
        with self.assertRaises(notify.BotAPIError) as raised:
            notify.bot_api_call("TOKEN", "getMe")
        self.assertIn("api.telegram.org", str(raised.exception))
        self.assertEqual(raised.exception.http_status, 502)

    def test_network_errors_name_the_host_for_a_custom_base(self):
        def boom(request, timeout=None):
            raise urllib.error.URLError("connection refused")

        notify.urllib.request.urlopen = boom
        with patch.dict(os.environ, {"TELEGRAM_API_BASE": "http://localhost:8081"}):
            with self.assertRaises(notify.BotAPIError) as raised:
                notify.bot_api_call("TOKEN", "getMe")
        self.assertTrue(raised.exception.network)
        self.assertIn("localhost:8081", str(raised.exception))

    def test_probe_reports_an_answer_as_reachable(self):
        class Answer(io.BytesIO):
            status = 404

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        notify.urllib.request.urlopen = lambda request, timeout=None: Answer(b"{}")
        reachable, detail = notify.probe_api_base(timeout=1)
        self.assertTrue(reachable)
        self.assertIn("HTTP 404", detail)

    def test_probe_reports_a_dead_host_as_unreachable(self):
        def boom(request, timeout=None):
            raise urllib.error.URLError("Name or service not known")

        notify.urllib.request.urlopen = boom
        reachable, detail = notify.probe_api_base(timeout=1)
        self.assertFalse(reachable)
        self.assertIn("not reachable", detail)

    def test_probe_reports_a_malformed_api_base(self):
        with patch.dict(os.environ, {"TELEGRAM_API_BASE": "not-a-url"}):
            reachable, detail = notify.probe_api_base()
        self.assertFalse(reachable)
        self.assertIn("TELEGRAM_API_BASE", detail)


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
