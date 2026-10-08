# telegram-remover

Remove the shxt from **your own** Telegram account. Two ways to run it:

- **CLI** (`telegram_remover.py`) — runs on your machine
- **Web UI** (`index.html` + `api/*.py`) — deployable on **Vercel** in one click

Both can:

| Action | What it removes |
|---|---|
| `clean-messages` / "My messages" | Every message **you** sent, in a chat |
| `wipe` / "Wipe" | The **full history** of one chat (both sides where Telegram permits) |
| `leave-all` / "Leave ALL" | Every **group and channel** you joined (with a keep-list) |
| `guard` / "Join guard" | Auto-mod: deletes the "X joined the group" message and (opt-in, with a typed confirmation) removes or bans the joiner |
| `delete-account` / "Delete my account" | Your **entire Telegram account**, permanently |
| `db` | Inspect / purge the **SQL history database** (guard decisions, cleanup runs) |

Two optional add-ons turn it into a proper little platform (see below):

* **SQL history database** (`db.py`) — every guard decision, cleanup run,
  allow-list and cursor is stored in **SQLite / PostgreSQL / MySQL** and
  purged automatically every month.
* **Telegram bot** (`bot.py` + `notify.py`) — alerts for guard actions and
  cleanup runs, plus `/status`, `/recent`, `/pause` (kill switch) etc. from
  any device.

> ⚠️ Everything this tool does is **irreversible**. The CLI is a **dry run**
> unless you pass `--yes`; the web UI asks for confirmation before each action.

## Why not a bot?

The official Telegram Bot API cannot read or delete your private chats, act
as you, or leave groups on your behalf. This tool uses a **user session**
(exactly like Telegram Desktop does) via [Telethon](https://github.com/LonamiWebs/Telethon).

## Option A — CLI

### Setup

1. Get API credentials: <https://my.telegram.org> → *API development tools* →
   create an app → copy `api_id` and `api_hash`.
2. Install dependencies (in a virtualenv):

   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Configure credentials:

   ```bash
   cp .env.example .env
   # edit .env: API_ID, API_HASH, PHONE (with country code, e.g. +855...)
   ```

4. Log in once — Telegram sends you a code (and your 2FA password, if enabled):

   ```bash
   python telegram_remover.py login
   ```

   The session is saved to `telegram_remover.session`. **Keep this file
   secret** — anyone with it can act as you. It is gitignored.

### Usage

```bash
# see what you've got
python telegram_remover.py dialogs
python telegram_remover.py dialogs --type groups

# delete all messages YOU sent, everywhere
python telegram_remover.py clean-messages            # dry run first
python telegram_remover.py clean-messages --yes      # actually delete
python telegram_remover.py clean-messages --chat @somechat --limit 500 --yes

# wipe one chat completely (both sides where permitted)
python telegram_remover.py wipe @somechat --yes

# leave every group/channel you joined
python telegram_remover.py leave-all                 # dry run first
python telegram_remover.py leave-all --yes
python telegram_remover.py leave-all --keep @bestfriend --keep "Family Group" --yes
python telegram_remover.py leave-all --include-private --yes   # also wipe DMs

# auto-mod: watch a group and act the moment somebody joins
python telegram_remover.py guard --chat @mygroup --preview 50
python telegram_remover.py guard --chat @mygroup --yes        # delete "X joined" notices
python telegram_remover.py guard --chat @mygroup --yes --remove-joiner
python telegram_remover.py guard --chat @mygroup --yes --ban-joiner --ban-hours 24 \
       --purge-joiner-messages --allow @bestfriend --log guard.jsonl

# 💀 the nuclear option: delete your account forever
python telegram_remover.py delete-account --yes      # you must also type DELETE

# 🗄 the SQL history database (on by default — sqlite:///telegram_remover.db)
python telegram_remover.py db status                 # what is stored
python telegram_remover.py db events -n 20           # recent guard decisions
python telegram_remover.py db runs                   # recent cleanup runs
python telegram_remover.py db purge                  # monthly cleanup, now
python telegram_remover.py --no-db wipe @chat --yes  # skip the database once
```

## The join guard (auto-mod)

`guard` watches a group and reacts the moment somebody joins:

| What it reacts to | |
|---|---|
| "X joined the group" after **you accepted their join request** | ✔ |
| "X joined the group" via an **invite link** | ✔ |
| X joined via the group's **public @username** (they added themselves) | ✔ |
| a member **added** someone else | only with `--include-added` |
| joins in small groups / groups with hidden members (no service message) | ✔ (nothing to delete there, but kick/ban works) |

What it can do — every step is opt-in, and the last two are opt-in *twice*:

| Action | Flag | Notes |
|---|---|---|
| delete the "X joined the group" message | `--yes` (on by default) | needs the "delete messages" admin right |
| remove (kick) the joiner | `--remove-joiner` | needs a **typed confirmation** |
| ban the joiner (forever, or `--ban-hours N`) | `--ban-joiner` | needs a **typed confirmation** |
| delete the messages the joiner already sent | `--purge-joiner-messages` | needs kick/ban armed too |

```bash
# dry run (nothing is changed) — start here
python telegram_remover.py guard --chat @mygroup

# what would the current settings do to the newest 50 messages?
python telegram_remover.py guard --chat @mygroup --ban-joiner --preview 50

# delete the join notices, leave people alone
python telegram_remover.py guard --chat @mygroup --yes

# invite-only style: ban whoever joins (you will be asked to type BAN)
python telegram_remover.py guard --chat @mygroup --yes --ban-joiner --ban-hours 24

# headless (systemd/container): same confirmation, passed as a flag
python telegram_remover.py guard --chat @mygroup --yes --ban-joiner \
       --confirm-with BAN --log /var/log/tg-guard.jsonl
```

### Safety rails (the important part)

- **Nothing happens by accident.** An action runs only when it is both
  *intended* (`--remove-joiner` / `--ban-joiner`) and *armed*. Arming anything
  that touches a **person** means typing the exact phrase — `KICK`, `BAN` or
  `KICK BAN` — at the prompt (or passing it with `--confirm-with`, which is
  equally explicit). Deleting the "joined" notices only needs `--yes`.
- **Admins, the group owner and you are never touched**, and `--allow @user`
  protects anyone else. An allow-list entry that cannot be resolved aborts the
  run instead of silently weakening the list.
- **Circuit breaker.** After `--max-actions` (default **30**) member actions in
  an hour the guard **pauses itself** and says so loudly — it will not turn a
  mass-join raid into a mass-ban. Re-arm it when you are ready.
- **Dry run by default**, `--preview N` shows what it *would* do (no
  confirmation needed, nothing is written), and `--log file.jsonl` keeps an
  audit trail of every join, decision and result.
- The guard only ever looks at messages **newer than the moment you started
  it** — it can never act on your group's history by accident.

> Telegram lets you ban for 30 seconds … 366 days, or forever; anything outside
> that range becomes a permanent ban. The web UI / CLI spell out which one you
> are arming. In *small* (non-super) groups Telegram cannot ban, only remove —
> the log says so when that happens.

## Option B — Deploy the web UI on Vercel

No terminal needed — the repo ships a static web UI plus small **stateless**
Python serverless functions (`api/*.py`). Click the button:

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fsarakmacbook%2Ftelegram-remover-)

1. Click **Deploy** and import the repository.
2. (Optional) set environment variables in the Vercel dashboard:
   - `ACCESS_TOKEN` — if set, every request must send it as `X-Access-Token`.
     Use this to password-protect your deployment.
   - `API_ID` / `API_HASH` — if not set, you enter them in the web UI instead.
   - `DATABASE_URL` — store guard/cleanup history in SQL (SQLite / Postgres /
     MySQL) and let the companion bot report on it. Without it the deployment
     stores nothing server-side.
   - `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — default alerts for CLI and
     web actions (see below). In the web UI, you can connect a browser-specific
     bot instead without setting these environment variables.
   - `TELEGRAM_API_BASE` — optional Bot API root for a custom/self-hosted
     Telegram Bot API server; defaults to `https://api.telegram.org`. It must
     be reachable from the deployment (a local `localhost` is not reachable
     from Vercel).
3. Open the deployment URL, log in with your phone number (Telegram sends you
   a code), and clean away.

**The join guard in the browser.** Vercel functions cannot stay awake, so the
*Join guard* card works by **polling**: the first check only records "watch
from here", and every poll (default: every 8 s) asks `/api/guard` to handle the
joins that arrived since the last one. Press **Guard** next to a group, choose
the actions, then *Start dry run* or *Start LIVE*. Live kick/ban asks you to
type the confirmation phrase, the server refuses requests without it, and the
guard disarms itself when the circuit breaker trips. The audit trail scrolls in
the Log panel. Without a database, nothing about the guard is stored
server-side — the cursor and the browser log live in your browser, like the
session itself (set `DATABASE_URL` to also keep the history in SQL, below).

Want to try the UI before deploying? `python dev_server.py` serves the same
page and the same `/api/*` endpoints locally (`HOST=0.0.0.0 PORT=8000` to
expose it to your LAN).

**How it works:** your Telegram session is a Telethon `StringSession` kept in
your **browser's localStorage** and sent with each request — nothing is
stored on the server. If you connect a bot in the UI, its token and recipient
ID are also kept in localStorage and sent only with requests that may produce
an alert; the server uses them transiently. Long cleanups run in **chunks**
(200 messages or 10 chats per call) and the UI polls until done, so they work
within Vercel's function time limits (`maxDuration: 60` in `vercel.json`; raise
it if you're on a paid plan). Flood limits are surfaced to the UI, which waits
and retries.

## SQL history database (optional)

`db.py` stores everything the tool does in a SQL database — any
[SQLAlchemy](https://www.sqlalchemy.org/) URL works:

| Engine | `DATABASE_URL` |
|---|---|
| SQLite (default) | `sqlite:///telegram_remover.db` (a local file) |
| PostgreSQL | `postgresql://user:pass@host/db` (Vercel Postgres, Neon, ...) |
| MySQL | `mysql://user:pass@host/db` (PlanetScale, ...) |

What lands in there:

* **events** — every join-guard decision (join, acted, protected, paused,
  error) with who/where/what, from the CLI *and* the web UI
* **runs** — cleanup history: `clean-messages`, `wipe`, `leave-all`,
  `delete-account`, guard sessions, with counts
* **kv** — small shared state: the bot's pause flag, live guard status,
  allow-lists, cursors

**Monthly cleanup:** rows older than `DB_RETENTION_DAYS` (default **30**)
are deleted automatically every time the database is opened, so it never
grows forever — "store everything, remove everything every month".
`python telegram_remover.py db purge` and the bot's `/purge` do the same
on demand (add `--all` to also drop old kv config).

The CLI records by default (SQLite file next to the commands); turn it off
per run with `--no-db`, or point it at a server with `--db postgresql://...`
or `DATABASE_URL`. The web UI records **only** when `DATABASE_URL` is set —
without it the deployment stays fully stateless, exactly as before. Either
way a database problem can never break a cleanup: storage is best effort.

The web UI shows what is stored in its **Database history** card
(`GET /api/events`), and `python telegram_remover.py db status|events|runs`
shows the same from a terminal.

## Telegram bot: alerts + status commands (optional)

The Bot API cannot access or clean your personal-account chats — that stays
the user session's job — but it is perfect for **alerts and remote control**.
The web UI has a separate **Telegram bot alerts** connection: enter the token
from [@BotFather](https://t.me/BotFather) and your Telegram user/chat ID, then
choose **Connect & send test alert**. The UI verifies the bot with `getMe`,
sends a test message, and saves the token/ID in that browser only. Subsequent
web cleanup and guard requests use those values for alerts; they are not
saved by the server. `TELEGRAM_NOTIFY=0` still disables automatic alerts.
For private alerts, message your bot with `/start` first. The chat ID is not
your phone number: use your numeric user ID (for example, from
[@userinfobot](https://t.me/userinfobot)); group/supergroup IDs are usually
negative, and the bot must be a member of that group. Public channel usernames
such as `@mychannel` are also accepted by Telegram. If the deployment has
`ACCESS_TOKEN` enabled, enter it in the web UI before connecting the bot. This
connection only sends notifications — personal-account actions still require
the phone-based user session above. To use alerts from the CLI or run the
companion command bot, configure the server/CLI environment as below.

1. Create a bot with [@BotFather](https://t.me/BotFather), put the token in
   `TELEGRAM_BOT_TOKEN`.
2. Send that bot any message and run `python bot.py` — it prints your chat
   id. Put it in `TELEGRAM_CHAT_ID` (comma-separated for several admins) and
   restart the bot. Group/supergroup IDs are negative (commonly `-100…`).
3. (Optional) Set `TELEGRAM_API_BASE` to the root URL of a custom/self-hosted
   Bot API server. It defaults to `https://api.telegram.org` and is used by
   both the web UI and companion bot.

Now you get, in that chat:

* **Alerts** from the CLI and the web UI: every guard action, circuit-breaker
  pause and cleanup summary (`TELEGRAM_NOTIFY=0` keeps the bot silent).
* **Commands** — the bot shares state with the guard through the SQL database:

| Command | What it does |
|---|---|
| `/status` | guard mode, last decision, circuit breaker, database stats |
| `/recent [n]` | the last n guard decisions |
| `/runs [n]` | the last n cleanup runs |
| `/pause` | **kill switch** — every running guard holds all actions |
| `/resume` | let the guards act again |
| `/purge [days]` | delete records older than the retention window, now |
| `/db` | database statistics |

`/pause` is the one to remember: typed from your phone, it stops a running
guard (CLI *or* web) from deleting anything or touching anyone until
`/resume` — independent of the circuit breaker. Commands only work in the
chats listed in `TELEGRAM_CHAT_ID`; everybody else is ignored.

## Safety features

- CLI: dry run by default; `--yes` required to actually delete.
- The join guard adds a second layer on top: kicking/banning/purging needs the
  typed confirmation phrase (CLI prompt, `--confirm-with`, or the web UI's
  confirmation box — the server rejects `/api/guard` without it), admins and
  the allow-list are protected, and the circuit breaker pauses the guard after
  30 member actions an hour instead of mass-banning.
- The companion bot's `/pause` is a **kill switch** shared through the SQL
  database: it holds every running guard (CLI and web) from deleting or
  removing anything until `/resume` — handy when you are away from the
  keyboard and something looks wrong.
- `delete-account` additionally requires typing `DELETE` (CLI) or typing
  `DELETE` into a prompt (web).
- Messages are deleted in batches of 100 (Telegram's per-call limit).
- Flood limits are handled automatically — the tool sleeps (CLI) or reports
  a wait time (web) and retries.
- If you are not an admin in a group/channel, deleting "for everyone" is not
  permitted; the tool falls back to deleting for yourself only and tells you
  how many failed.
- If a big cleanup gets cut off by rate limits, just **re-run it** — it
  picks up where it left off (cursor-based).
- Secret chats are not supported by the Telegram API and are skipped.

## Project layout

```
telegram_remover.py   CLI (argparse) — thin wrapper over remover_core + guard_core
remover_core.py       shared async logic: chunked sweeps, leave, delete-account
guard_core.py         the join guard: join detection, policy, confirmation,
                      rate limiting, kick/ban/purge, resumable cursor scans
db.py                 SQL storage (SQLite/PostgreSQL/MySQL): events, runs, kv,
                      monthly auto-purge
notify.py             Telegram Bot API alerts (stdlib urllib, best effort)
bot.py                companion bot: /status /recent /runs /pause /purge /db
api_common.py         request helpers + handlers for the serverless endpoints
api/*.py              one Vercel Python function per endpoint (Flask WSGI)
                       including the browser bot-token test connection
index.html            the web UI (vanilla JS, no build step)
dev_server.py         run that web UI locally instead of on Vercel
vercel.json           function config (maxDuration, includeFiles)
test_*.py             unit tests — no real Telegram account needed
```

## Development

```bash
python -m unittest discover -v
```

## Disclaimer

This tool only touches **your own account** and is intended for privacy
cleanup (e.g. before switching numbers or selling a device). It is not
affiliated with Telegram. Deleted data cannot be recovered — use at your own
risk.
