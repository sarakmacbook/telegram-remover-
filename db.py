"""SQL storage for telegram-remover: guard events, cleanup runs and config.

Backed by SQLAlchemy, so any of these work via ``DATABASE_URL``:

* SQLite (default) — ``sqlite:///telegram_remover.db`` (a local file)
* PostgreSQL      — ``postgresql://user:pass@host/db`` (Vercel Postgres, Neon, ...)
* MySQL           — ``mysql://user:pass@host/db`` (PlanetScale, ...)

What is stored:

* **events**  — every join-guard decision (join, acted, protected, paused, ...)
* **runs**    — cleanup runs (clean-messages, wipe, leave-all, delete-account)
* **kv**      — small config/state shared between the CLI guard, the web API
  and the Telegram bot: pause flag, guard status, allow-lists, cursors.

Monthly cleanup: rows older than ``DB_RETENTION_DAYS`` (default **30**) are
purged automatically every time the store is opened, so the database never
grows without bound. ``python telegram_remover.py db purge`` and the bot's
``/purge`` command do the same on demand.

The store is *best effort*: the helpers in the CLI/web layer swallow errors,
so a database problem can never break a cleanup. Without SQLAlchemy installed
everything here disables itself and the rest of the app works as before.
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timezone

try:
    from sqlalchemy import (Boolean, Column, Float, Integer, MetaData, String,
                            Table, Text, create_engine, delete, func, select)
except ImportError:  # pragma: no cover - sqlalchemy is in requirements.txt
    create_engine = None

DEFAULT_URL = "sqlite:///telegram_remover.db"
DEFAULT_RETENTION_DAYS = 30
EVENT_KINDS = ("join", "added", "clean", "wipe", "leave", "guard", "other")

_metadata = MetaData()

events = Table(
    "events", _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", Float, nullable=False, index=True),
    Column("kind", String(32), index=True),
    Column("chat_key", String(64), index=True),
    Column("chat_label", String(255)),
    Column("user_id", String(32), index=True),
    Column("user_label", String(255)),
    Column("status", String(32), index=True),
    Column("reason", Text),
    Column("dry_run", Boolean, default=False),
    Column("source", String(16)),
    Column("payload", Text),
)

runs = Table(
    "runs", _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", Float, nullable=False, index=True),
    Column("action", String(32), index=True),
    Column("chat", String(255)),
    Column("status", String(32)),
    Column("details", Text),
    Column("source", String(16)),
)

kv = Table(
    "kv", _metadata,
    Column("key", String(96), primary_key=True),
    Column("value", Text),
    Column("updated_at", Float),
)

_lock = threading.Lock()
_stores = {}


def _now():
    return time.time()


def _iso(ts):
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(
            timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def _json_dumps(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_loads(text, default=None):
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _person_label(user):
    """Short label for a guard_core ``user_info`` dict (or a plain string)."""
    if isinstance(user, str):
        return user
    if not isinstance(user, dict):
        return None
    if user.get("username"):
        return "@" + str(user["username"]).lstrip("@")
    name = " ".join(str(user.get(k) or "") for k in ("first", "last")).strip()
    if name:
        return name
    if user.get("id") is not None:
        return f"id {user['id']}"
    return None


def redact_url(url):
    """Hide any password in a database URL (safe to print / store)."""
    return re.sub(r"://([^:/@]+):([^@/]*)@", r"://\1:***@", str(url or ""))


def retention_days(environ=None):
    env = os.environ if environ is None else environ
    try:
        days = int(env.get("DB_RETENTION_DAYS") or DEFAULT_RETENTION_DAYS)
    except (TypeError, ValueError):
        days = DEFAULT_RETENTION_DAYS
    return max(1, days)


class Store:
    """One SQL database: events + runs + kv, with monthly auto-purge."""

    def __init__(self, url, retention_days_=None, auto_purge=True):
        if create_engine is None:
            raise RuntimeError(
                "SQL storage needs SQLAlchemy — pip install sqlalchemy")
        self.url = url
        self.retention_days = (retention_days_
                               if retention_days_ is not None
                               else retention_days())
        connect_args = {}
        if url.startswith("sqlite"):
            # The dev server and the bot may touch the store from threads.
            connect_args["check_same_thread"] = False
        self.engine = create_engine(self._normalize(url),
                                    future=True, connect_args=connect_args)
        _metadata.create_all(self.engine)
        if auto_purge:
            self.purge_old()

    @staticmethod
    def _normalize(url):
        # Heroku/Vercel style URLs use postgres:// — SQLAlchemy wants postgresql://
        if url.startswith("postgres://"):
            return "postgresql://" + url[len("postgres://"):]
        return url

    # -- events -----------------------------------------------------------

    def record_event(self, kind="join", ts=None, chat_key=None, chat_label=None,
                     user_id=None, user_label=None, status=None, reason=None,
                     dry_run=False, source=None, payload=None):
        """Insert one event row. Returns its id."""
        row = {
            "ts": float(ts if ts is not None else _now()),
            "kind": str(kind or "other")[:32],
            "chat_key": str(chat_key) if chat_key is not None else None,
            "chat_label": (str(chat_label)[:255] if chat_label else None),
            "user_id": str(user_id) if user_id is not None else None,
            "user_label": (str(user_label)[:255] if user_label else None),
            "status": (str(status)[:32] if status else None),
            "reason": reason,
            "dry_run": bool(dry_run),
            "source": (str(source)[:16] if source else None),
            "payload": _json_dumps(payload) if payload is not None else None,
        }
        with self.engine.begin() as conn:
            return conn.execute(events.insert().values(**row)).inserted_primary_key[0]

    def record_guard(self, record, source="cli"):
        """Store one guard_core audit record. Returns the event id."""
        user = record.get("user") or {}
        ts = None
        when = record.get("time")
        if when:
            try:
                ts = datetime.fromisoformat(when).timestamp()
            except (TypeError, ValueError):
                ts = None
        return self.record_event(
            kind=record.get("kind") or "join",
            ts=ts,
            chat_key=(record.get("chat_key")
                      if record.get("chat_key") is not None
                      else record.get("chat_id")),
            chat_label=record.get("chat_label"),
            user_id=user.get("id"),
            user_label=_person_label(user),
            status=record.get("status"),
            reason=record.get("reason"),
            dry_run=bool(record.get("dry_run")),
            source=source,
            payload=record,
        )

    def list_events(self, limit=20, status=None, kind=None):
        """Newest events first, as plain dicts."""
        limit = max(1, min(int(limit or 20), 200))
        stmt = select(events).order_by(events.c.ts.desc(), events.c.id.desc())
        if status:
            stmt = stmt.where(events.c.status == str(status))
        if kind:
            stmt = stmt.where(events.c.kind == str(kind))
        stmt = stmt.limit(limit)
        with self.engine.connect() as conn:
            return [self._event_dict(r) for r in conn.execute(stmt)]

    @staticmethod
    def _event_dict(row):
        return {
            "id": row.id,
            "ts": row.ts,
            "time": _iso(row.ts),
            "kind": row.kind,
            "chat_key": row.chat_key,
            "chat_label": row.chat_label,
            "user_id": row.user_id,
            "user_label": row.user_label,
            "status": row.status,
            "reason": row.reason,
            "dry_run": bool(row.dry_run),
            "source": row.source,
            "payload": _json_loads(row.payload),
        }

    # -- runs -------------------------------------------------------------

    def record_run(self, action, status="done", chat=None, details=None,
                   source=None, ts=None):
        """Insert one cleanup-run row. Returns its id."""
        row = {
            "ts": float(ts if ts is not None else _now()),
            "action": str(action or "other")[:32],
            "chat": (str(chat)[:255] if chat else None),
            "status": (str(status)[:32] if status else None),
            "details": _json_dumps(details) if details is not None else None,
            "source": (str(source)[:16] if source else None),
        }
        with self.engine.begin() as conn:
            return conn.execute(runs.insert().values(**row)).inserted_primary_key[0]

    def list_runs(self, limit=20):
        """Newest runs first, as plain dicts."""
        limit = max(1, min(int(limit or 20), 200))
        stmt = select(runs).order_by(runs.c.ts.desc(), runs.c.id.desc()).limit(limit)
        with self.engine.connect() as conn:
            out = []
            for r in conn.execute(stmt):
                out.append({
                    "id": r.id,
                    "ts": r.ts,
                    "time": _iso(r.ts),
                    "action": r.action,
                    "chat": r.chat,
                    "status": r.status,
                    "details": _json_loads(r.details),
                    "source": r.source,
                })
            return out

    # -- kv (config + shared guard state) ---------------------------------

    def kv_get(self, key, default=None):
        with self.engine.connect() as conn:
            row = conn.execute(
                select(kv.c.value).where(kv.c.key == str(key))).first()
        return _json_loads(row[0], default) if row else default

    def kv_set(self, key, value):
        payload = _json_dumps(value)
        key = str(key)
        with self.engine.begin() as conn:
            updated = conn.execute(
                kv.update().where(kv.c.key == key)
                .values(value=payload, updated_at=_now())).rowcount
            if not updated:
                conn.execute(kv.insert().values(key=key, value=payload,
                                                updated_at=_now()))

    def kv_delete(self, key):
        with self.engine.begin() as conn:
            conn.execute(delete(kv).where(kv.c.key == str(key)))

    def kv_items(self, prefix=None):
        """All kv entries (optionally by key prefix) as {key: value}."""
        stmt = select(kv.c.key, kv.c.value)
        if prefix:
            stmt = stmt.where(kv.c.key.like(str(prefix).replace("%", "") + "%"))
        with self.engine.connect() as conn:
            return {r[0]: _json_loads(r[1]) for r in conn.execute(stmt)}

    # -- maintenance -------------------------------------------------------

    def purge_old(self, days=None, include_kv=False):
        """Delete rows older than the retention window ("every month").

        Returns ``{"events": n, "runs": n, "kv": n, "days": d, "cutoff": iso}``.
        """
        days = int(days if days is not None else self.retention_days)
        days = max(1, days)
        cutoff = _now() - days * 86400
        with self.engine.begin() as conn:
            n_events = conn.execute(
                delete(events).where(events.c.ts < cutoff)).rowcount
            n_runs = conn.execute(
                delete(runs).where(runs.c.ts < cutoff)).rowcount
            n_kv = 0
            if include_kv:
                n_kv = conn.execute(
                    delete(kv).where(kv.c.updated_at < cutoff)).rowcount
        return {"events": n_events, "runs": n_runs, "kv": n_kv,
                "days": days, "cutoff": _iso(cutoff)}

    def stats(self):
        with self.engine.connect() as conn:
            n_events = conn.execute(select(func.count()).select_from(events)).scalar() or 0
            n_runs = conn.execute(select(func.count()).select_from(runs)).scalar() or 0
            n_kv = conn.execute(select(func.count()).select_from(kv)).scalar() or 0
            oldest = conn.execute(select(func.min(events.c.ts))).scalar()
            newest = conn.execute(select(func.max(events.c.ts))).scalar()
        return {
            "url": redact_url(self.url),
            "events": n_events,
            "runs": n_runs,
            "kv": n_kv,
            "retention_days": self.retention_days,
            "oldest_event": _iso(oldest) if oldest else None,
            "newest_event": _iso(newest) if newest else None,
        }

    def close(self):
        self.engine.dispose()


def resolve_url(url=None, environ=None):
    """Explicit URL > DATABASE_URL env > None."""
    if url:
        return url
    env = os.environ if environ is None else environ
    for name in ("DATABASE_URL", "TELEGRAM_REMOVER_DB_URL"):
        value = (env.get(name) or "").strip()
        if value:
            return value
    return None


def get_store(url=None, default=False, environ=None, auto_purge=True):
    """The process-wide :class:`Store`, or ``None`` when SQL storage is off.

    Storage is on when ``DATABASE_URL`` is set or an explicit ``url`` is
    passed. ``default=True`` falls back to a local SQLite file — that is what
    the CLI and the companion bot use, so they share one database.
    """
    resolved = resolve_url(url, environ=environ)
    if not resolved:
        if not default:
            return None
        resolved = DEFAULT_URL
    if create_engine is None:
        return None
    with _lock:
        store = _stores.get(resolved)
        if store is None:
            store = Store(resolved, auto_purge=auto_purge)
            _stores[resolved] = store
        return store


def reset_stores():
    """Drop cached stores (tests)."""
    with _lock:
        for store in _stores.values():
            store.close()
        _stores.clear()
