"""Tests for the SQL storage layer (db.py) — no network, no real database.

Run with:  python -m unittest discover -v
"""

import os
import time
import unittest

import db


class StoreCase(unittest.TestCase):
    def setUp(self):
        db.reset_stores()
        self.store = db.Store("sqlite:///:memory:", auto_purge=False)

    def tearDown(self):
        db.reset_stores()


class TestEvents(StoreCase):
    def test_record_event_and_list(self):
        eid = self.store.record_event(
            kind="join", ts=1000.0, chat_key="-1001", chat_label="My Group",
            user_id=42, user_label="@ada", status="acted",
            reason=None, dry_run=False, source="cli",
            payload={"results": {"delete": "deleted the notice"}})
        self.assertIsInstance(eid, int)
        events = self.store.list_events()
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["kind"], "join")
        self.assertEqual(e["chat_label"], "My Group")
        self.assertEqual(e["user_label"], "@ada")
        self.assertEqual(e["status"], "acted")
        self.assertEqual(e["source"], "cli")
        self.assertEqual(e["payload"]["results"]["delete"], "deleted the notice")
        self.assertTrue(e["time"].startswith("1970-01-01"))

    def test_record_guard_maps_an_audit_record(self):
        record = {
            "time": "2026-10-08T10:00:00+00:00",
            "kind": "join",
            "chat_id": -100123,
            "chat_label": "My Group",
            "user": {"id": 42, "first": "Ada", "last": "L", "username": "ada"},
            "status": "acted",
            "reason": None,
            "dry_run": False,
            "results": {"delete": "deleted the notice"},
        }
        self.store.record_guard(record, source="web")
        e = self.store.list_events()[0]
        self.assertEqual(e["chat_key"], "-100123")
        self.assertEqual(e["user_id"], "42")
        self.assertEqual(e["user_label"], "@ada")
        self.assertEqual(e["source"], "web")
        self.assertEqual(e["payload"], record)
        self.assertTrue(e["time"].startswith("2026-10-08"))

    def test_person_label_fallbacks(self):
        self.assertEqual(db._person_label({"id": 7, "first": "Ada"}), "Ada")
        self.assertEqual(db._person_label({"id": 7}), "id 7")
        self.assertEqual(db._person_label("@bob"), "@bob")
        self.assertIsNone(db._person_label(None))

    def test_list_filters_by_status(self):
        self.store.record_event(status="acted", ts=1.0)
        self.store.record_event(status="protected", ts=2.0)
        self.assertEqual(len(self.store.list_events()), 2)
        self.assertEqual(
            [e["status"] for e in self.store.list_events(status="acted")],
            ["acted"])


class TestRuns(StoreCase):
    def test_record_and_list_runs(self):
        self.store.record_run("clean-messages", status="done", chat="@x",
                              details={"deleted": 5}, source="cli", ts=10.0)
        runs = self.store.list_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["action"], "clean-messages")
        self.assertEqual(runs[0]["chat"], "@x")
        self.assertEqual(runs[0]["details"], {"deleted": 5})
        self.assertEqual(runs[0]["source"], "cli")

    def test_newest_first(self):
        self.store.record_run("wipe", ts=1.0)
        self.store.record_run("leave-all", ts=2.0)
        self.assertEqual([r["action"] for r in self.store.list_runs()],
                         ["leave-all", "wipe"])


class TestKv(StoreCase):
    def test_set_get_overwrite(self):
        self.assertIsNone(self.store.kv_get("guard:paused"))
        self.store.kv_set("guard:paused", True)
        self.assertIs(self.store.kv_get("guard:paused"), True)
        self.store.kv_set("guard:paused", False)
        self.assertIs(self.store.kv_get("guard:paused"), False)

    def test_structured_values_round_trip(self):
        self.store.kv_set("guard:policy", {"ban": True, "allow": ["@a"]})
        self.assertEqual(self.store.kv_get("guard:policy"),
                         {"ban": True, "allow": ["@a"]})

    def test_delete_and_items_with_prefix(self):
        self.store.kv_set("guard:cursor:a", 1)
        self.store.kv_set("guard:cursor:b", 2)
        self.store.kv_set("other", 3)
        items = self.store.kv_items("guard:cursor")
        self.assertEqual(items, {"guard:cursor:a": 1, "guard:cursor:b": 2})
        self.store.kv_delete("guard:cursor:a")
        self.assertEqual(self.store.kv_items("guard:cursor"),
                         {"guard:cursor:b": 2})


class TestPurge(StoreCase):
    def test_purge_removes_only_old_rows(self):
        old = time.time() - 40 * 86400      # 40 days ago
        self.store.record_event(ts=old, status="acted")
        self.store.record_event(ts=time.time(), status="acted")
        self.store.record_run("wipe", ts=old)
        self.store.record_run("wipe", ts=time.time())
        self.store.kv_set("tmp:old", 1)     # kv is only purged with include_kv

        res = self.store.purge_old()
        self.assertEqual(res["events"], 1)
        self.assertEqual(res["runs"], 1)
        self.assertEqual(res["days"], 30)
        self.assertEqual(len(self.store.list_events()), 1)
        self.assertEqual(len(self.store.list_runs()), 1)
        self.assertIsNotNone(self.store.kv_get("tmp:old"))

    def test_purge_days_override_and_kv(self):
        old = time.time() - 10 * 86400
        self.store.record_event(ts=old)
        self.store.kv_set("tmp:x", 1)
        with self.store.engine.begin() as conn:      # backdate the kv entry
            conn.execute(db.kv.update().where(db.kv.c.key == "tmp:x")
                         .values(updated_at=old))
        res = self.store.purge_old(days=5, include_kv=True)
        self.assertEqual(res["events"], 1)
        self.assertEqual(res["days"], 5)
        self.assertEqual(self.store.kv_items(), {})

    def test_auto_purge_on_open(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "auto.db")
        store = db.Store(f"sqlite:///{path}", auto_purge=False)
        store.record_event(ts=time.time() - 40 * 86400)
        store.record_run("wipe", ts=time.time() - 40 * 86400)
        store.close()
        store2 = db.Store(f"sqlite:///{path}")       # auto-purge on open
        self.assertEqual(store2.stats()["events"], 0)
        self.assertEqual(store2.stats()["runs"], 0)
    def test_retention_env_is_honoured(self):
        store = db.Store("sqlite:///:memory:", retention_days_=3)
        self.assertEqual(store.retention_days, 3)
        self.assertEqual(db.retention_days({"DB_RETENTION_DAYS": "12"}), 12)
        self.assertEqual(db.retention_days({"DB_RETENTION_DAYS": "junk"}),
                         db.DEFAULT_RETENTION_DAYS)


class TestGetStore(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("TELEGRAM_REMOVER_DB_URL", None)
        db.reset_stores()

    def test_disabled_without_config(self):
        os.environ.pop("DATABASE_URL", None)
        self.assertIsNone(db.get_store())

    def test_default_uses_sqlite_file(self):
        os.environ.pop("DATABASE_URL", None)
        store = db.get_store("sqlite:///:memory:", default=True)
        self.assertIsNotNone(store)

    def test_env_url_is_used(self):
        os.environ["DATABASE_URL"] = "sqlite:///:memory:"
        store = db.get_store()
        self.assertIsNotNone(store)
        self.assertEqual(store.url, "sqlite:///:memory:")

    def test_postgres_url_scheme_is_normalized(self):
        self.assertEqual(
            db.Store._normalize("postgres://u:p@h/db"),
            "postgresql://u:p@h/db")
        self.assertEqual(
            db.Store._normalize("postgresql://u:p@h/db"),
            "postgresql://u:p@h/db")

    def test_url_redaction_hides_passwords(self):
        self.assertEqual(db.redact_url("postgresql://me:secret@host/db"),
                         "postgresql://me:***@host/db")
        self.assertEqual(db.redact_url("sqlite:///x.db"), "sqlite:///x.db")

    def test_stats_shape(self):
        store = db.Store("sqlite:///:memory:", auto_purge=False)
        store.record_event()
        stats = store.stats()
        self.assertEqual(stats["events"], 1)
        self.assertEqual(stats["runs"], 0)
        self.assertEqual(stats["retention_days"], db.DEFAULT_RETENTION_DAYS)
        self.assertIsNotNone(stats["newest_event"])


if __name__ == "__main__":
    unittest.main()
