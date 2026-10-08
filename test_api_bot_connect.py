"""Tests for the browser Telegram Bot API connection (no network calls)."""

import os
import unittest
from unittest.mock import patch

from flask import Flask

import api_common
import notify


VALID_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"


class BotConnectTests(unittest.TestCase):
    def test_requires_token_and_chat_id(self):
        self.assertEqual(api_common.handle_bot_connect({})[1], 400)
        self.assertIn("bot_token", api_common.handle_bot_connect({})[0]["error"])
        result, status = api_common.handle_bot_connect({"bot_token": VALID_TOKEN})
        self.assertEqual(status, 400)
        self.assertIn("chat_id", result["error"])

    @patch("api_common.notify.bot_api_call")
    def test_verifies_bot_and_sends_a_test_message(self, call):
        call.side_effect = [
            {"id": 123456789, "username": "alert_bot", "first_name": "Alerts"},
            {"message_id": 1},
        ]
        result = api_common.handle_bot_connect({
            "bot_token": VALID_TOKEN,
            "chat_id": "987654321",
        })

        self.assertTrue(result["connected"])
        self.assertTrue(result["test_message_sent"])
        self.assertEqual(result["bot"]["username"], "alert_bot")
        self.assertEqual(result["chat_id"], "987654321")
        self.assertNotIn(VALID_TOKEN, str(result))
        self.assertEqual(call.call_args_list[0].args[:2], (VALID_TOKEN, "getMe"))
        self.assertEqual(call.call_args_list[1].args[:2],
                         (VALID_TOKEN, "sendMessage"))
        self.assertEqual(call.call_args_list[1].args[2]["chat_id"], "987654321")
        self.assertIn("test alert", call.call_args_list[1].args[2]["text"])

    @patch("api_common.notify.bot_api_call")
    def test_accepts_group_ids_and_public_channel_usernames(self, call):
        for chat_id in ("-1001234567890", "@mychannel"):
            call.reset_mock()
            call.side_effect = [
                {"id": 123456789, "username": "alert_bot"},
                {"message_id": 1},
            ]
            result = api_common.handle_bot_connect({
                "bot_token": VALID_TOKEN,
                "chat_id": chat_id,
            })
            self.assertTrue(result["connected"])
            self.assertEqual(result["chat_id"], chat_id)
            self.assertEqual(call.call_args_list[1].args[2]["chat_id"], chat_id)

    @patch("api_common.notify.bot_api_call")
    def test_rejects_malformed_token_before_calling_telegram(self, call):
        result, status = api_common.handle_bot_connect({
            "bot_token": "not/a/token",
            "chat_id": "987654321",
        })
        self.assertEqual(status, 400)
        self.assertIn("invalid format", result["error"])
        call.assert_not_called()

    @patch("api_common.notify.bot_api_call")
    def test_explains_that_user_must_start_bot_if_test_send_fails(self, call):
        call.side_effect = [
            {"id": 123456789, "username": "alert_bot"},
            api_common.notify.BotAPIError("Forbidden: bot can't initiate conversation"),
        ]
        result, status = api_common.handle_bot_connect({
            "bot_token": VALID_TOKEN,
            "chat_id": "987654321",
        })
        self.assertEqual(status, 400)
        self.assertIn("send /start first", result["error"])
        self.assertNotIn(VALID_TOKEN, result["error"])

    def test_request_notifier_uses_valid_browser_config_without_leaking_to_env(self):
        headers = {
            "X-Tg-Bot-Token": VALID_TOKEN,
            "X-Tg-Bot-Chat-Id": "987654321",
        }
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "987654321:ANOTHER_VALID_TOKEN",
            "TELEGRAM_CHAT_ID": "111111111",
            "TELEGRAM_NOTIFY": "1",
        }):
            notifier = api_common._notifier(headers)
        self.assertEqual(notifier.token, VALID_TOKEN)
        self.assertEqual(notifier.chat_id, "987654321")
        self.assertTrue(notifier.enabled)

    def test_partial_browser_config_does_not_fall_back_to_server_bot(self):
        headers = {"X-Tg-Bot-Chat-Id": "987654321"}
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "987654321:ANOTHER_VALID_TOKEN",
            "TELEGRAM_CHAT_ID": "111111111",
            "TELEGRAM_NOTIFY": "1",
        }):
            notifier = api_common._notifier(headers)
        self.assertFalse(notifier.enabled)
        self.assertEqual(notifier.token, "")


class BotConnectMessageTests(unittest.TestCase):
    """A failure must say what is wrong and what to do about it."""

    def test_non_object_body_is_a_400_not_a_crash(self):
        for body in ([1, 2], "text", 7, None, 3.5):
            result, status = api_common.handle_bot_connect(body)
            self.assertEqual(status, 400)
            self.assertIn("JSON object", result["error"])

    def test_token_mistakes_are_explained(self):
        cases = {
            "": "bot_token is required",
            "not/a/token": "invalid format",
            "https://api.telegram.org/bot123456789:AAHdqTcvCH/getMe":
                "invalid format",
            "bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw": "invalid format",
        }
        for token, expected in cases.items():
            result, status = api_common.handle_bot_connect(
                {"bot_token": token, "chat_id": "987654321"})
            self.assertEqual(status, 400, token)
            self.assertIn(expected, result["error"], token)
            if token:
                # the rejected value is never echoed back
                self.assertNotIn(token, result["error"], token)

    def test_chat_id_mistakes_are_explained(self):
        cases = {
            "": "chat_id is required",
            "+15551234567": "phone number",
            "123456789, -1001234567890": "single ID",
            "123456789 -1001234567890": "single ID",
            "@ab": "at least 3 characters",
            "x" * 200: "chat_id is invalid",
        }
        for chat_id, expected in cases.items():
            result, status = api_common.handle_bot_connect(
                {"bot_token": VALID_TOKEN, "chat_id": chat_id})
            self.assertEqual(status, 400, chat_id)
            self.assertIn(expected, result["error"], chat_id)

    def test_chat_id_accepts_ids_and_channel_usernames(self):
        for chat_id in ("987654321", "-1001234567890", "@mychannel"):
            self.assertIsNone(notify.chat_id_problem(chat_id), chat_id)

    @patch("api_common.notify.bot_api_call")
    def test_unreachable_bot_api_names_the_host(self, call):
        call.side_effect = notify.BotAPIError(
            "could not reach the Telegram Bot API at api.telegram.org: boom",
            network=True)
        result, status = api_common.handle_bot_connect({
            "bot_token": VALID_TOKEN, "chat_id": "987654321"})
        self.assertEqual(status, 502)
        self.assertIn("api.telegram.org", result["error"])
        self.assertIn("could not reach", result["error"])

    @patch("api_common.notify.bot_api_call")
    def test_chat_not_found_explains_user_id_and_start(self, call):
        call.side_effect = [
            {"id": 1, "username": "alert_bot"},
            notify.BotAPIError("Bad Request: chat not found"),
        ]
        result, status = api_common.handle_bot_connect({
            "bot_token": VALID_TOKEN, "chat_id": "987654321"})
        self.assertEqual(status, 400)
        self.assertIn("userinfobot", result["error"])
        self.assertIn("/start", result["error"])
        self.assertNotIn(VALID_TOKEN, result["error"])


class BotDiagnosticsTests(unittest.TestCase):
    @patch("api_common.notify.probe_api_base", return_value=(False, "no route"))
    def test_reports_an_unreachable_bot_api_host(self, probe):
        result = api_common.handle_bot_diagnostics()
        self.assertEqual(result["endpoint"], "bot_connect")
        self.assertFalse(result["api_host_reachable"])
        self.assertIn("no route", result["detail"])
        self.assertTrue(any("cannot reach" in h for h in result["hints"]))
        self.assertIn("userinfobot", result["chat_id_help"])

    @patch("api_common.notify.probe_api_base", return_value=(True, "HTTP 404"))
    def test_reports_access_token_and_muted_alerts(self, probe):
        with patch.dict(os.environ, {"ACCESS_TOKEN": "s3cr3t-token",
                                     "TELEGRAM_NOTIFY": "0"}):
            result = api_common.handle_bot_diagnostics()
        self.assertTrue(result["access_token_required"])
        self.assertFalse(result["notify_enabled"])
        self.assertNotIn("s3cr3t-token", str(result))
        self.assertTrue(any("ACCESS_TOKEN" in h for h in result["hints"]))
        self.assertTrue(any("TELEGRAM_NOTIFY=0" in h for h in result["hints"]))

    @patch("api_common.notify.probe_api_base", return_value=(True, "HTTP 404"))
    def test_reports_a_broken_api_base_env_var(self, probe):
        with patch.dict(os.environ, {"TELEGRAM_API_BASE": "not-a-url"}):
            result = api_common.handle_bot_diagnostics()
        self.assertFalse(result["api_base"])
        self.assertTrue(any("TELEGRAM_API_BASE" in h for h in result["hints"]))


class JsonErrorTests(unittest.TestCase):
    """A crashing function must answer JSON, never an HTML error page."""

    def setUp(self):
        app = Flask(__name__)
        api_common.json_errors(app)

        @app.route("/boom")
        def boom():            # pragma: no cover - raised on purpose
            raise RuntimeError("kaboom " + VALID_TOKEN)

        self.client = app.test_client()

    def test_unknown_route_is_json(self):
        response = self.client.get("/nope")
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.get_json())

    def test_wrong_method_is_json(self):
        response = self.client.post("/nope")
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.get_json())

    def test_unhandled_exception_is_json_and_hides_tokens(self):
        response = self.client.get("/boom")
        self.assertEqual(response.status_code, 500)
        body = response.get_json()
        self.assertIn("kaboom", body["error"])
        self.assertNotIn(VALID_TOKEN, body["error"])
        self.assertIn("bot<redacted>", body["error"])

    def test_redact_only_touches_token_shaped_text(self):
        self.assertEqual(api_common.redact("plain text 12345"),
                         "plain text 12345")
        self.assertEqual(api_common.redact("987654321"),
                         "987654321")
        self.assertNotIn(VALID_TOKEN, api_common.redact(VALID_TOKEN))


if __name__ == "__main__":
    unittest.main()
