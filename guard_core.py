"""The "join guard" — automatic moderation of people joining a group.

Shared by the CLI (``telegram_remover.py guard``) and the Vercel web app
(``api/guard.py`` + the *Join guard* card in ``index.html``).

What it reacts to
-----------------
* ``MessageActionChatJoinedByRequest`` — "X joined the group", right after an
  admin accepted their join request.
* ``MessageActionChatJoinedByLink`` — joined through an invite link.
* ``MessageActionChatAddUser`` where the sender added themselves — a join via
  the group's public ``@username``.
* ``UpdateChatParticipantAdd`` / ``UpdateChannelParticipant`` — joins in small
  groups, or in groups where the member list is hidden, and Telegram sends no
  service message at all.
* With ``include_added``: also ``MessageActionChatAddUser`` where *another
  member* added them (off by default — invites by your own members are usually
  legitimate).

What it can do (every step is opt-in)
-------------------------------------
``delete``  delete the "X joined the group" service message.
``kick``    remove the joiner (they may rejoin if they still have a link).
``ban``     ban the joiner, permanently or for ``ban_seconds``.
``purge``   delete the messages that joiner already sent in that chat.

Safety model
------------
Nothing happens by accident:

* An action only runs when it is both **intended** (``GuardPolicy``) and
  **armed** (``JoinGuard(armed=...)``). Normal (non-``dry_run``) runs must arm
  ``kick``/``ban`` by typing the exact phrase from
  :func:`required_confirmation` — the CLI prompts for it, the web API refuses
  the request without ``confirm``.
* Admins, the account owner and everything in the allow-list are never
  touched (:meth:`JoinGuard.prepare`).
* A :class:`RateLimiter` pauses *all* further actions as soon as it would have
  to touch more than ``max_actions_per_hour`` members, so a mass-join raid can
  never turn into a silent mass-ban.
* Dry runs decide exactly as a live run would and record what *would* happen;
  they perform no write and consume no rate-limit budget.
* Every decision lands in an audit record (:func:`render_record`) that the CLI
  prints and, optionally, appends to a JSONL file.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from telethon import errors, types, utils
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.types import ChannelParticipantsAdmins

import remover_core

# --------------------------------------------------------------------------
# kinds of joins
# --------------------------------------------------------------------------

JOINED_BY_REQUEST = "joined_by_request"   # "X joined the group" after you accepted
JOINED_BY_LINK = "joined_by_link"         # invite link / public username
JOINED = "joined"                         # added themselves via the @username
ADDED = "added"                           # added by another member

SELF_JOIN_KINDS = (JOINED, JOINED_BY_REQUEST, JOINED_BY_LINK)
KIND_LABELS = {
    JOINED_BY_REQUEST: "joined after a join request was accepted",
    JOINED_BY_LINK: "joined via an invite link",
    JOINED: "joined via the public @username",
    ADDED: "was added by another member",
}

#: every action the guard knows about
ACTIONS = ("delete", "kick", "ban", "purge")
#: actions that touch a *person* — these need confirmation and rate limiting
MEMBER_ACTIONS = ("kick", "ban", "purge")

DEFAULT_MAX_ACTIONS_PER_HOUR = 30   # circuit breaker for member actions
DEDUPE_SECONDS = 30                 # ignore the same (chat, user) join twice
PURGE_LIMIT = 100                   # joiner messages deleted per join
DEFAULT_SCAN = 50                   # messages inspected per web/scan pass
MAX_SCAN = 200
DEFAULT_ACTIONS_PER_CALL = 10       # member actions per web call
MAX_ACTIONS_PER_CALL = 25

#: what each action does, phrased for the dry-run log ("would …")
ACTION_TEXTS = {
    "delete": "delete the join notice",
    "kick": "remove (kick) the member",
    "ban": "ban the member",
    "purge": "delete the messages they sent here",
}


class GuardError(Exception):
    """A guard action failed in a way worth reporting to the user."""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _user_id(peer):
    """Plain user id (positive int) for a PeerUser / User / int, else None."""
    if peer is None or isinstance(peer, bool):
        return None
    if isinstance(peer, types.User):
        return peer.id
    if isinstance(peer, types.PeerUser):
        return peer.user_id
    if isinstance(peer, (types.PeerChat, types.PeerChannel)):
        return None
    if isinstance(peer, int):
        return peer if peer > 0 else None
    uid = getattr(peer, "user_id", None)
    return uid if isinstance(uid, int) and not isinstance(uid, bool) else None


def chat_key(peer):
    """The "marked" id Telegram uses for a chat (-100… / -… / positive user)."""
    if peer is None:
        return None
    if isinstance(peer, int):
        return peer
    if isinstance(peer, (types.User, types.Chat, types.Channel)):
        try:
            return utils.get_peer_id(peer)
        except (TypeError, ValueError):
            return getattr(peer, "id", None)
    try:
        return utils.get_peer_id(peer)
    except (TypeError, ValueError):
        return getattr(peer, "id", None)


def entity_id(entity):
    """Key a chat entity is stored under (marked id, like the dialogs list)."""
    return chat_key(entity)


def entity_label(entity, fallback=None):
    """Display name of a chat."""
    return (getattr(entity, "title", None)
            or getattr(entity, "first_name", None)
            or (fallback if fallback is not None else None)
            or f"id {getattr(entity, 'id', '?')}")


def user_info(user=None, uid=None):
    """JSON-friendly description of a member."""
    uid = getattr(user, "id", None) or uid
    name = " ".join(x for x in (getattr(user, "first_name", None),
                                getattr(user, "last_name", None)) if x).strip()
    if not name:
        name = getattr(user, "title", None) or (f"id {uid}" if uid else "unknown")
    return {"id": uid, "name": name, "username": getattr(user, "username", None)}


def user_label(info):
    """``@someone (Some One)`` — for logs and the UI."""
    if not info:
        return "unknown"
    bits = []
    if info.get("username"):
        bits.append(f"@{info['username']}")
    if info.get("name"):
        bits.append(f"({info['name']})")
    if not bits:
        bits.append(f"id {info.get('id')}")
    return " ".join(bits)


def join_info(message):
    """Describe a *join* if ``message`` is a "somebody joined" service message.

    Returns ``None`` for ordinary messages and for every other service
    message. The dict has:

      kind        one of the ``JOINED*`` / ``ADDED`` constants
      user_ids    the members that joined (usually one)
      added_ids   members added by someone else in the same action
      message_id  id of the service message (``None`` if there is none)
      actor_id    who did it (the joiner, or whoever added them)
      chat_key    marked chat id
    """
    action = getattr(message, "action", None)
    if action is None:
        return None

    sender = _user_id(getattr(message, "from_id", None))
    if sender is None:
        sender = _user_id(getattr(message, "sender_id", None))

    if isinstance(action, types.MessageActionChatJoinedByRequest):
        # Telegram sends no fields here: the joiner is the message sender.
        kind, users, added = JOINED_BY_REQUEST, [sender], []
    elif isinstance(action, types.MessageActionChatJoinedByLink):
        kind, users, added = JOINED_BY_LINK, [sender], []
    elif isinstance(action, types.MessageActionChatAddUser):
        ids = [u for u in (_user_id(x) for x in action.users) if u]
        if sender is not None and sender in ids:
            kind, users, added = JOINED, [sender], [u for u in ids if u != sender]
        else:
            kind, users, added = ADDED, ids, []
    else:
        return None

    users = [u for u in users if u]
    if not users:
        return None  # nothing to act on (e.g. the sender is unknown)
    return {
        "kind": kind,
        "user_ids": users,
        "added_ids": added,
        "message_id": getattr(message, "id", None),
        "actor_id": sender,
        "chat_key": chat_key(getattr(message, "peer_id", None)),
    }


def join_info_from_update(update):
    """Join info for a raw update, or ``None``.

    Handles the two service-message updates *and* the member-list updates that
    Telegram sends instead of a service message (small groups, or groups with
    a hidden member list).
    """
    if isinstance(update, (types.UpdateNewMessage, types.UpdateNewChannelMessage)):
        message = getattr(update, "message", None)
        if not isinstance(message, types.MessageService):
            return None
        return join_info(message)

    if isinstance(update, types.UpdateChatParticipantAdd):
        uid = _user_id(getattr(update, "user_id", None))
        if not uid:
            return None
        actor = _user_id(getattr(update, "inviter_id", None))
        return {
            # inviter_id == the member means they joined on their own (via an
            # invite link they were given); otherwise somebody added them.
            "kind": JOINED if actor == uid else ADDED,
            "user_ids": [uid],
            "added_ids": [],
            "message_id": None,
            "actor_id": actor,
            "chat_key": chat_key(types.PeerChat(update.chat_id)),
        }

    if isinstance(update, types.UpdateChannelParticipant):
        if getattr(update, "new_participant", None) is None:
            return None  # somebody left
        if getattr(update, "prev_participant", None) is not None:
            return None  # changed role (promotion/demotion), not a join
        if not isinstance(update.new_participant, types.ChannelParticipant):
            return None  # a ban/restriction, not a join
        uid = _user_id(getattr(update, "user_id", None))
        if not uid:
            return None
        actor = _user_id(getattr(update, "actor_id", None))
        return {
            "kind": JOINED if actor == uid else ADDED,
            "user_ids": [uid],
            "added_ids": [],
            "message_id": None,
            "actor_id": actor,
            "chat_key": chat_key(types.PeerChannel(update.channel_id)),
        }

    return None


def required_confirmation(actions):
    """The phrase a human must type before member actions are armed.

    ``{"kick"}`` → ``"KICK"``, ``{"ban"}`` → ``"BAN"``, both → ``"KICK BAN"``.
    Deleting a "X joined the group" notice is not a member-level action, so
    ``{"delete"}`` needs no phrase.
    """
    actions = set(actions)
    words = []
    if "kick" in actions:
        words.append("KICK")
    if "ban" in actions:
        words.append("BAN")
    return " ".join(words)


def plan_actions(policy, armed, info):
    """Which actions apply to one join, and whether each is armed.

    Pure function: returns ``{action: armed_bool}``. ``ban`` supersedes
    ``kick``, and ``purge`` only runs together with one of them.
    """
    plan = {}
    if policy.delete_join_message and info.get("message_id"):
        plan["delete"] = True
    if policy.ban_joiner:
        plan["ban"] = True
    elif policy.remove_joiner:
        plan["kick"] = True
    if policy.purge_joiner_messages and (policy.ban_joiner or policy.remove_joiner):
        plan["purge"] = True
    armed = set(armed)
    return {action: action in armed for action in plan}


def _planned_member_actions(plan):
    return sum(1 for a, is_armed in plan.items() if is_armed and a in MEMBER_ACTIONS)


# --------------------------------------------------------------------------
# policy
# --------------------------------------------------------------------------

class GuardPolicy:
    """What the guard is *allowed* to do — before anything is armed.

    This is the declarative half of the safety model: the caller states what
    the guard may do (delete notices, kick, ban, purge, protect admins, …) and
    ``armed`` on :class:`JoinGuard` decides whether it actually may run this
    time. Both halves must agree before Telegram is touched.
    """

    def __init__(self, delete_join_message=True, remove_joiner=False,
                 ban_joiner=False, ban_seconds=0, purge_joiner_messages=False,
                 include_added=False, allow=(), allow_ids=(),
                 protect_admins=True,
                 max_actions_per_hour=DEFAULT_MAX_ACTIONS_PER_HOUR):
        self.delete_join_message = bool(delete_join_message)
        self.remove_joiner = bool(remove_joiner)
        self.ban_joiner = bool(ban_joiner)
        self.ban_seconds = max(0, int(ban_seconds or 0))  # 0 = permanent
        self.purge_joiner_messages = bool(purge_joiner_messages)
        self.include_added = bool(include_added)
        self.allow = [str(a).strip() for a in allow if str(a).strip()]
        self.allow_ids = {int(i) for i in allow_ids}
        self.protect_admins = bool(protect_admins)
        self.max_actions_per_hour = max(0, int(max_actions_per_hour))

    # -- derived views ----------------------------------------------------
    @property
    def triggers(self):
        """Join kinds that make the guard consider acting."""
        return SELF_JOIN_KINDS + ((ADDED,) if self.include_added else ())

    @property
    def intent(self):
        """Actions this policy is allowed to take at all."""
        actions = []
        if self.delete_join_message:
            actions.append("delete")
        if self.ban_joiner:
            actions.append("ban")
        if self.remove_joiner:
            actions.append("kick")
        if self.purge_joiner_messages:
            actions.append("purge")
        return actions

    @property
    def confirmation(self):
        """Phrase required to arm the member actions of this policy."""
        return required_confirmation(set(self.intent) & set(MEMBER_ACTIONS))

    @property
    def ban_text(self):
        if not self.ban_joiner:
            return "off"
        if not self.ban_seconds:
            return "PERMANENT ban"
        return f"ban for {_human_seconds(self.ban_seconds)}"

    def describe(self):
        """Human-readable review lines (CLI banner, API echo, UI panel)."""
        out = [
            f"delete the 'joined' message: {'on' if self.delete_join_message else 'off'}",
            f"remove (kick) the joiner:  {'on' if self.remove_joiner else 'off'}",
            f"ban the joiner:            {self.ban_text}",
            f"delete joiner's messages:  {'on' if self.purge_joiner_messages else 'off'}"
            + ("" if self.purge_joiner_messages else ""),
            f"people added by members:   {'also guarded' if self.include_added else 'left alone'}",
            "protect admins:            "
            + ("yes (creator, admins and you are never touched)"
               if self.protect_admins else "NO — admins can be removed too"),
            f"allow-list:                {len(self.allow) + len(self.allow_ids)} entr(y/ies)",
            "circuit breaker:           "
            + (f"pause after {self.max_actions_per_hour} member action(s)/hour"
               if self.max_actions_per_hour else "OFF (unlimited member actions)"),
        ]
        return out

    def to_dict(self):
        """JSON-friendly policy (used by the web UI panel)."""
        return {
            "delete_join_message": self.delete_join_message,
            "remove_joiner": self.remove_joiner,
            "ban_joiner": self.ban_joiner,
            "ban_seconds": self.ban_seconds,
            "purge_joiner_messages": self.purge_joiner_messages,
            "include_added": self.include_added,
            "protect_admins": self.protect_admins,
            "allow": list(self.allow),
            "max_actions_per_hour": self.max_actions_per_hour,
            "intent": self.intent,
            "confirmation": self.confirmation,
        }


def _human_seconds(seconds):
    seconds = int(seconds)
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


# --------------------------------------------------------------------------
# rate limiting + audit trail
# --------------------------------------------------------------------------

class RateLimiter:
    """Sliding-window counter for member actions (kick / ban / purge)."""

    def __init__(self, max_per_hour=DEFAULT_MAX_ACTIONS_PER_HOUR, window=3600,
                 now=time.time):
        self.max_per_hour = max(0, int(max_per_hour))
        self.window = int(window)
        self._now = now
        self._events = []

    def _prune(self, now=None):
        now = self._now() if now is None else now
        self._events = [t for t in self._events if now - t < self.window]
        return now

    def used(self, now=None):
        self._prune(now)
        return len(self._events)

    def allowed(self, count=1, now=None):
        """True if ``count`` more member actions fit in the window."""
        if not self.max_per_hour:
            return True
        return self.used(now) + max(0, int(count)) <= self.max_per_hour

    def retry_in(self, now=None):
        """Seconds until the oldest action leaves the window (0 = now)."""
        now = self._prune(now)
        if self.allowed(now=now):
            return 0
        return max(1, int(self.window - (now - min(self._events))))

    def record(self, count=1, now=None):
        now = self._now() if now is None else now
        self._events.extend([now] * max(0, int(count)))

    def reset(self):
        self._events = []


def write_audit(path, record):
    """Append one audit record as JSONL. Returns an error string or None."""
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return None
    except OSError as e:  # pragma: no cover - disk problems
        return str(e)


def render_record(record):
    """One human-readable log line for an audit record."""
    moment = record.get("time_short") or ""
    parts = [f"[{moment}]" if moment else "[]",
             record.get("chat_label") or f"chat {record.get('chat_id')}",
             KIND_LABELS.get(record.get("kind"), record.get("kind") or "join"),
             user_label(record.get("user"))]
    status = record.get("status")
    results = record.get("results") or {}
    detail = ", ".join(str(v) for v in results.values() if v)

    if status in ("acted", "dry_run") and detail:
        parts.append(f"— {'would ' if status == 'dry_run' else ''}{detail}")
    elif status == "protected":
        parts.append(f"— SKIPPED: {record.get('reason')}")
    elif status == "paused":
        parts.append(f"— PAUSED: {record.get('reason')}")
    elif status == "ignored":
        parts.append(f"— ignored: {record.get('reason')}")
    elif status == "error":
        parts.append(f"— ERROR: {record.get('reason')}")
    return " ".join(p for p in parts if p)


def summarize(records):
    """Counts per status — handy for API responses and the CLI footer."""
    out = {"total": len(records), "acted": 0, "dry_run": 0, "protected": 0,
           "paused": 0, "ignored": 0, "error": 0}
    for rec in records:
        status = rec.get("status")
        if status in out:
            out[status] += 1
    return out


# --------------------------------------------------------------------------
# telegram actions
# --------------------------------------------------------------------------

async def resolve_input_user(client, uid, message=None):
    """Best-effort entity (with access hash) for a user id seen in a join.

    ``message`` is the service message when we have one — the joiner is
    usually its sender, and Telethon already resolved that entity for us.
    Returns ``None`` when Telegram never told us who this id is (which can
    happen in the web/batch mode for someone who just joined); the caller
    then still deletes the notice but reports that the member could not be
    resolved.
    """
    if message is not None:
        try:
            sender = await message.get_sender()
        except Exception:  # noqa: BLE001 - best effort, any failure is fine
            sender = None
        if sender is not None and _user_id(sender) == uid:
            return sender
    # ``get_entity`` first: it returns the full user (nice names in the log)
    # and is a pure cache lookup for integer ids; ``get_input_entity`` is the
    # fallback that always gives us something usable for kick/ban.
    for getter in ("get_entity", "get_input_entity"):
        try:
            return await getattr(client, getter)(int(uid))
        except Exception:  # noqa: BLE001 - not in the entity cache
            continue
    return None


async def fetch_admin_ids(client, entity, sleep=True):
    """Ids of the creator + admins of a channel/supergroup (best effort)."""
    if not isinstance(entity, types.Channel):
        return set()  # small groups have no admin list we can rely on
    ids = set()
    offset = 0
    try:
        while offset < 1000:
            res = await remover_core.rpc(
                lambda: client(GetParticipantsRequest(
                    channel=entity, filter=ChannelParticipantsAdmins(),
                    offset=offset, limit=100, hash=0)),
                sleep=sleep)
            participants = list(getattr(res, "participants", []) or [])
            for p in participants:
                uid = (_user_id(getattr(p, "user_id", None))
                       or _user_id(getattr(p, "peer", None)))
                if uid:
                    ids.add(uid)
            if len(participants) < 100:
                break
            offset += len(participants)
    except errors.RPCError:
        return ids  # the guard still protects you and the allow-list
    return ids


async def kick_user(client, entity, user, sleep=True):
    """Remove a member without banning them (they may rejoin)."""
    if isinstance(entity, types.Chat):
        # Small groups only know "remove", and Telethon's helper builds the
        # right request for them.
        await remover_core.rpc(lambda: client.kick_participant(entity, user),
                               sleep=sleep)
        return "removed"
    # Supergroup: ban, then lift the ban — the classic "kick".
    await remover_core.rpc(
        lambda: client.edit_permissions(entity, user, view_messages=False),
        sleep=sleep)
    try:
        await remover_core.rpc(lambda: client.edit_permissions(entity, user),
                               sleep=sleep)
    except errors.RPCError as e:
        raise GuardError(
            f"banned but could not lift the ban ({type(e).__name__}) — the "
            f"member is still restricted, unban them manually") from e
    return "removed"


async def ban_user(client, entity, user, seconds=0, sleep=True):
    """Ban a member. ``seconds=0`` means permanent."""
    if isinstance(entity, types.Chat):
        # Telegram's small groups cannot ban, only remove.
        await kick_user(client, entity, user, sleep=sleep)
        return "removed (small groups cannot ban)"
    until = timedelta(seconds=int(seconds)) if seconds else None
    await remover_core.rpc(
        lambda: client.edit_permissions(entity, user, until_date=until,
                                        view_messages=False),
        sleep=sleep)
    return "banned" if not seconds else f"banned for {_human_seconds(seconds)}"


# --------------------------------------------------------------------------
# the guard
# --------------------------------------------------------------------------

class JoinGuard:
    """Applies a :class:`GuardPolicy` to joins, one join at a time."""

    def __init__(self, policy, dry_run=True, armed=(), now=time.time,
                 limiter=None, audit_path=None, on_record=None, verbose=True,
                 sleep=True, pause_check=None):
        self.policy = policy
        self.dry_run = bool(dry_run)
        self.armed = {a for a in armed if a in ACTIONS}
        self.verbose = bool(verbose)
        self.sleep = bool(sleep)
        self.audit_path = audit_path
        self.on_record = on_record
        self.pause_check = pause_check   # callable -> bool (e.g. the Telegram
                                         # bot's /pause flag in the database)
        self.records = []
        self.limiter = limiter if limiter is not None else RateLimiter(
            policy.max_actions_per_hour, now=now)
        self._now = now
        self._protected = {}   # chat id -> {user id: reason}
        self._seen = {}        # (chat id, user id) -> timestamp
        self._labels = {}      # chat id -> display name

    # -- setup ------------------------------------------------------------
    async def prepare(self, client, entity, label=None):
        """Resolve the allow-list and admins for one chat.

        Returns ``{user id: why protected}``. Raises ``ValueError`` if an
        allow-list entry cannot be resolved (fail loudly, never silently
        run with a weaker allow-list than the admin asked for).
        """
        label = label or entity_label(entity)
        protected = {}
        me = await client.get_me()
        protected[me.id] = "you (the account owner)"
        for entry in self.policy.allow:
            try:
                ent = await remover_core.resolve_entity(client, entry)
            except ValueError as e:
                raise ValueError(
                    f"allow-list entry {entry!r}: {e}") from e
            uid = _user_id(ent)
            if uid is None:
                raise ValueError(
                    f"allow-list entry {entry!r} is not a user "
                    f"(it looks like a group/channel)")
            protected[uid] = "allow-list"
        for uid in self.policy.allow_ids:
            protected[uid] = "allow-list"
        if self.policy.protect_admins:
            for uid in await fetch_admin_ids(client, entity, sleep=self.sleep):
                protected.setdefault(uid, "admin")
        self._protected[entity_id(entity)] = protected
        self._labels[entity_id(entity)] = label
        return protected

    def protected(self, entity, user_id):
        """Reason the user is protected in this chat, or ``None``."""
        return self._protected.get(entity_id(entity), {}).get(user_id)

    @property
    def paused(self):
        return not self.limiter.allowed()

    def pause_reason(self):
        return (f"circuit breaker: {self.limiter.used()} member action(s) in "
                f"the last hour (limit {self.limiter.max_per_hour}); "
                f"re-arm to continue, or wait "
                f"{_human_seconds(self.limiter.retry_in())}")

    # -- one join ---------------------------------------------------------
    async def handle(self, client, entity, info, label=None, dry_run=None,
                     remember=True, preview=False):
        """Apply the policy to one join. Returns the audit records.

        One record per joining user. ``dry_run`` overrides the guard's mode
        for this call; ``preview=True`` additionally reports what the *policy*
        would do as if everything were armed (used by the read-only previews),
        and ``remember=False`` keeps a preview from marking users as
        "already handled".
        """
        label = label or self._labels.get(entity_id(entity)) or entity_label(entity)
        dry_run = self.dry_run if dry_run is None else bool(dry_run)
        out = []
        for uid in info.get("user_ids") or []:
            record = await self._handle_one(client, entity, info, uid, label,
                                            dry_run=dry_run, remember=remember,
                                            preview=preview)
            if record is not None:
                out.append(record)
        return out

    async def _handle_one(self, client, entity, info, uid, label, dry_run,
                          remember, preview=False):
        now = self._now()
        chat_id = entity_id(entity)
        record = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "time_short": datetime.now().strftime("%H:%M:%S"),
            "chat_id": chat_id,
            "chat_label": label,
            "kind": info.get("kind"),
            "message_id": info.get("message_id"),
            "actor_id": info.get("actor_id"),
            "user": user_info(None, uid),
            "dry_run": dry_run,
            "preview": bool(preview),
            "armed": sorted(self.armed),
            "status": "ignored",
            "reason": None,
            "results": {},
        }

        if info.get("kind") not in self.policy.triggers:
            record["reason"] = ("the member was added by someone else "
                                "(include_added is off)")
            return self._finish(record, verbose_only=True)

        key = (chat_id, uid)
        last = self._seen.get(key)
        if last is not None and now - last < DEDUPE_SECONDS:
            record["reason"] = "duplicate join update (already handled)"
            if remember:
                self._seen[key] = now
            return self._finish(record, verbose_only=True)
        if remember:
            self._seen[key] = now

        reason = self.protected(entity, uid)
        if reason:
            record["status"] = "protected"
            record["reason"] = f"{reason} is never touched"
            return self._finish(record)

        # A remote kill switch (the Telegram bot's /pause, shared through the
        # SQL database) holds every live action. Dry runs and previews are
        # read-only, so they keep reporting what would happen.
        if not dry_run and not preview and self.pause_check and self.pause_check():
            record["status"] = "paused"
            record["reason"] = ("paused remotely (Telegram bot /pause) — "
                                "send /resume to continue")
            return self._finish(record)

        user = await resolve_input_user(client, uid, info.get("message"))
        record["user"] = user_info(user, uid)
        # A preview is read-only, so it reports what the policy *would* do —
        # i.e. as if every intended action had been armed.
        plan = plan_actions(self.policy,
                            set(ACTIONS) if preview else self.armed, info)
        needed = _planned_member_actions(plan)

        # The circuit breaker only applies to write runs: a dry run consumes
        # no budget, so it must keep reporting what would happen.
        if needed and not dry_run and not self.limiter.allowed(needed, now=now):
            record["status"] = "paused"
            record["reason"] = self.pause_reason()
            return self._finish(record)

        record["plan"] = plan
        if dry_run:
            record["status"] = "dry_run"
            for action, is_armed in plan.items():
                suffix = "" if is_armed else " [NOT ARMED — confirmation required]"
                record["results"][action] = ACTION_TEXTS[action] + suffix
            return self._finish(record)

        performed = 0
        for action, is_armed in plan.items():
            if not is_armed:
                record["results"][action] = (
                    "skipped: not armed (confirmation required)")
                continue
            if user is None and action in MEMBER_ACTIONS:
                record["results"][action] = (
                    f"error: Telegram did not give us access to id {uid} "
                    f"(no username or entity was sent) — remove them from the "
                    f"app or by @username")
                continue
            try:
                record["results"][action] = await self._act(
                    client, entity, action, info, user)
                if action in MEMBER_ACTIONS:
                    performed += 1
            except GuardError as e:
                record["results"][action] = f"error: {e}"
            except errors.RPCError as e:
                record["results"][action] = f"error: {type(e).__name__}: {e}"
            except remover_core.FloodWait as e:
                # Web/serverless mode: report and let the caller resume from
                # this very message instead of losing the join.
                record["results"][action] = f"error: flood wait {e.seconds}s"
                record["status"] = "error"
                record["reason"] = f"Telegram flood wait {e.seconds}s — retry later"
                record["member_actions"] = performed
                self._record_actions(performed, now)
                self._finish(record)
                raise
        self._record_actions(performed, now)
        record["member_actions"] = performed

        done = [str(v) for v in record["results"].values()
                if not str(v).startswith("skipped")]
        failures = [v for v in done if v.startswith("error")]
        if not done:
            record["status"] = "ignored"
            record["reason"] = "nothing was armed"
        elif failures and len(failures) == len(done):
            record["status"] = "error"
            record["reason"] = "; ".join(failures)
        else:
            record["status"] = "acted"
        return self._finish(record)

    def _record_actions(self, count, now):
        if count:
            self.limiter.record(count, now=now)

    async def _act(self, client, entity, action, info, user):
        """Run one action, return a short human-readable result string."""
        if action == "delete":
            failed = await remover_core.delete_ids(
                client, entity, [info["message_id"]], revoke=True,
                sleep=self.sleep)
            if failed:
                return ("error: could not delete the join notice "
                        "(needs the 'delete messages' admin right)")
            return "deleted the notice"

        if action == "kick":
            return await kick_user(client, entity, user, sleep=self.sleep)

        if action == "ban":
            return await ban_user(client, entity, user,
                                  seconds=self.policy.ban_seconds,
                                  sleep=self.sleep)

        if action == "purge":
            res = await remover_core.sweep_chunk(
                client, entity, PURGE_LIMIT, 0, from_user=user, yes=True,
                sleep=self.sleep)
            text = f"deleted {res['deleted']} message(s) of theirs"
            if res["failed"]:
                text += f", {res['failed']} failed"
            return text

        return f"error: unknown action {action}"

    def _finish(self, record, verbose_only=False):
        if verbose_only and not self.verbose:
            return None
        self.records.append(record)
        if self.audit_path and not record.get("preview"):
            # previews decide nothing real, so they stay out of the audit log
            error = write_audit(self.audit_path, record)
            if error:
                record["audit_error"] = error
        if self.on_record:
            self.on_record(record)
        return record

    # -- batch scanning (web app, and CLI preview) ------------------------
    async def _newest_id(self, client, entity):
        async for m in client.iter_messages(entity, limit=1):
            return m.id
        return 0

    async def _collect_newer(self, client, entity, cursor, scan):
        """Up to ``scan`` messages newer than ``cursor``, newest first."""
        out = []
        offset = 0
        while len(out) < scan:
            page = [m async for m in client.iter_messages(
                entity, limit=min(100, scan - len(out)), offset_id=offset)]
            if not page:
                break
            for message in page:
                if message.id <= cursor:
                    return out
                out.append(message)
            offset = page[-1].id
        return out

    async def preview(self, client, entity, limit=DEFAULT_SCAN, label=None):
        """Report what the guard *would* do to the newest ``limit`` messages.

        Never writes anything, never moves a cursor, never touches the
        rate-limit budget. Returns the audit records.
        """
        messages = [m async for m in client.iter_messages(entity, limit=limit)]
        records = []
        for message in reversed(messages):
            info = join_info(message)
            if info:
                info["message"] = message
                records.extend(await self.handle(client, entity, info,
                                                 label=label, dry_run=True,
                                                 remember=False, preview=True))
        return records

    async def scan(self, client, entity, cursor=0, scan=DEFAULT_SCAN,
                   max_actions=DEFAULT_ACTIONS_PER_CALL, label=None,
                   preview=False):
        """One resumable pass over the newest messages of one chat.

        Examines the messages newer than ``cursor`` (a message id), oldest
        first, and acts on the joins among them. Returns a dict for the
        caller/web UI:

          baseline       True when no cursor was given yet: nothing was
                         examined, and ``next_cursor`` is simply the newest
                         message id (the guard never acts on old history by
                         accident)
          records        audit records (see :func:`render_record`)
          next_cursor    pass this back next time to continue
          scanned        how many messages were examined
          truncated      True when the per-call action cap stopped the pass;
                         ``next_cursor`` is then rewound so nothing is lost
          paused         True when the circuit breaker is holding actions
          flood_wait     seconds Telegram asked us to wait (retry later)
          summary        counts per status
        """
        scan = max(1, min(int(scan or DEFAULT_SCAN), MAX_SCAN))
        max_actions = max(1, min(int(max_actions if max_actions is not None
                                     else DEFAULT_ACTIONS_PER_CALL),
                                 MAX_ACTIONS_PER_CALL))
        label = label or self._labels.get(entity_id(entity)) or entity_label(entity)
        before = len(self.records)

        if preview:
            records = await self.preview(client, entity, limit=scan, label=label)
            return {
                "baseline": False,
                "preview": True,
                "records": records,
                "next_cursor": int(cursor or 0),
                "scanned": scan,
                "truncated": False,
                "paused": False,
                "flood_wait": 0,
                "summary": summarize(records),
            }

        if not cursor:
            newest = await self._newest_id(client, entity)
            return {
                "baseline": True,
                "records": [],
                "next_cursor": newest,
                "scanned": 0,
                "truncated": False,
                "paused": False,
                "flood_wait": 0,
                "summary": summarize([]),
            }

        newest = await self._collect_newer(client, entity, cursor, scan)
        ordered = list(reversed(newest))       # oldest first
        flood_wait = 0
        truncated = False
        performed = 0
        processed = 0

        for index, message in enumerate(ordered):
            info = join_info(message)
            if info:
                info["message"] = message
                plan = plan_actions(self.policy, self.armed, info)
                needed = _planned_member_actions(plan)
                if (not self.dry_run and needed
                        and performed + needed > max_actions):
                    # Leave this join (and everything newer) for the next call
                    # instead of acting on a burst in one go.
                    truncated = True
                    cursor = min(m.id for m in ordered[index:]) - 1
                    break
                try:
                    records = await self.handle(client, entity, info, label=label)
                except remover_core.FloodWait as e:
                    flood_wait = e.seconds
                    cursor = min(m.id for m in ordered[index:]) - 1
                    break
                performed += sum(int(r.get("member_actions") or 0)
                                 for r in (records or []))
            processed += 1
            cursor = max(cursor, message.id)

        records = self.records[before:]
        return {
            "baseline": False,
            "records": records,
            "next_cursor": cursor,
            "scanned": processed,
            "truncated": truncated,
            "paused": self.paused,
            "flood_wait": flood_wait,
            "summary": summarize(records),
        }
