"""Tests for the companion Telegram bot (bot.py) — no network.

Run with:  python -m unittest discover -v
"""

import unittest

import bot as companion
import db


def msg(text, chat_id=42):
    return {"text": text, "chat": {"id": chat_id}}


class BotCase(unittest.TestCase):
    def setUp(self):
        db.reset_stores()
        self.store = db.Store("sqlite:///:memory:", auto_purge=False)
        self.bot = companion.CompanionBot("TOKEN", ["42"], store=self.store,
                                          log=lambda *a, **k: None)

    def tearDown(self):
        db.reset_stores()

    def reply(self, text, chat_id=42):
        return self.bot.handle_message(msg(text, chat_id))


class TestRouting(BotCase):
    def test_non_commands_are_ignored(self):
        self.assertIsNone(self.reply("hello there"))

    def test_unauthorized_chats_are_ignored(self):
        self.assertIsNone(self.reply("/status", chat_id=99))

    def test_unknown_commands_show_help(self):
        self.assertIn("/status", self.reply("/wat"))

    def test_command_with_bot_suffix(self):
        self.assertIn("SQL store", self.reply("/status@mybot"))

    def test_learn_mode_when_no_chat_is_allowed_yet(self):
        bot = companion.CompanionBot("TOKEN", [], store=self.store,
                                     log=lambda *a, **k: None)
        reply = bot.handle_message(msg("/status", chat_id=7))
        self.assertIn("TELEGRAM_CHAT_ID=7", reply)


class TestCommands(BotCase):
    def test_help_lists_the_commands(self):
        for name in ("/status", "/recent", "/runs", "/pause", "/resume",
                     "/purge", "/db"):
            self.assertIn(name, self.reply("/help"))

    def test_status_reports_pause_and_store(self):
        self.assertIn("Guard pause: no", self.reply("/status"))
        self.store.kv_set("guard:paused", True)
        self.assertIn("Guard pause: YES", self.reply("/status"))
        self.assertIn("sqlite", self.reply("/status").lower())

    def test_status_shows_the_guard_session(self):
        self.store.kv_set("guard:status", {
            "mode": "LIVE", "chats": ["My Group"], "started": "2026-10-08",
            "summary": {"acted": 2, "error": 0}})
        text = self.reply("/status")
        self.assertIn("LIVE on My Group", text)
        self.assertIn("acted 2", text)

    def test_recent_shows_events(self):
        self.assertIn("No guard events", self.reply("/recent"))
        self.store.record_event(chat_label="G", user_label="@a",
                                status="acted", ts=1.0)
        self.assertIn("@a", self.reply("/recent"))
        self.assertIn("acted", self.reply("/recent 3"))

    def test_runs_shows_cleanup_history(self):
        self.assertIn("No cleanup runs", self.reply("/runs"))
        self.store.record_run("wipe", chat="G", details={"deleted": 3})
        text = self.reply("/runs")
        self.assertIn("wipe", text)
        self.assertIn("deleted 3", text)

    def test_pause_and_resume_flip_the_shared_flag(self):
        self.assertIn("PAUSED", self.reply("/pause"))
        self.assertIs(self.store.kv_get("guard:paused"), True)
        self.assertIn("resumed", self.reply("/resume"))
        self.assertIs(self.store.kv_get("guard:paused"), False)

    def test_purge_removes_old_rows(self):
        import time
        self.store.record_event(ts=time.time() - 40 * 86400)
        self.store.record_event(ts=time.time())
        text = self.reply("/purge")
        self.assertIn("Purged 1 event(s)", text)
        self.assertEqual(len(self.store.list_events()), 1)

    def test_purge_with_a_bad_argument_shows_usage(self):
        self.assertIn("usage", self.reply("/purge soon"))

    def test_db_stats(self):
        self.assertIn("retention", self.reply("/db"))


class TestDisabledStore(BotCase):
    def test_commands_degrade_gracefully(self):
        self.bot.store = None
        self.assertIn("disabled", self.reply("/recent"))
        self.assertIn("disabled", self.reply("/pause"))
        self.assertIn("DISABLED", self.reply("/status"))


class TestHelpers(unittest.TestCase):
    def test_command_of(self):
        self.assertEqual(companion.command_of(msg("/recent 10")), ("recent", "10"))
        self.assertEqual(companion.command_of(msg("/status@bot")), ("status", ""))
        self.assertEqual(companion.command_of(msg("hi")), (None, ""))

    def test_parse_n(self):
        self.assertEqual(companion._parse_n("", 5), 5)
        self.assertEqual(companion._parse_n("3", 5), 3)
        self.assertEqual(companion._parse_n("300", 5), companion.MAX_RECENT)
        self.assertEqual(companion._parse_n("junk", 5), 5)

    def test_format_event_line(self):
        line = companion.format_event_line({
            "time": "2026-10-08T10:00:00+00:00", "chat_label": "G",
            "user_label": "@a", "status": "acted",
            "payload": {"results": {"delete": "deleted the notice"}}})
        self.assertIn("2026-10-08", line)
        self.assertIn("@a", line)
        self.assertIn("deleted the notice", line)


if __name__ == "__main__":
    unittest.main()
