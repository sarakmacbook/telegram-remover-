"""Tests for the browser Telegram Bot API connection (no network calls)."""

import os
import unittest
from unittest.mock import patch

import api_common


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


if __name__ == "__main__":
    unittest.main()
