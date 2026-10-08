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
| `delete-account` / "Delete my account" | Your **entire Telegram account**, permanently |

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

# 💀 the nuclear option: delete your account forever
python telegram_remover.py delete-account --yes      # you must also type DELETE
```

## Option B — Deploy the web UI on Vercel

No terminal needed — the repo ships a static web UI plus small **stateless**
Python serverless functions (`api/*.py`). Click the button:

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fsarakmacbook%2Ftelegram-remover-)

1. Click **Deploy** and import the repository.
2. (Optional) set environment variables in the Vercel dashboard:
   - `ACCESS_TOKEN` — if set, every request must send it as `X-Access-Token`.
     Use this to password-protect your deployment.
   - `API_ID` / `API_HASH` — if not set, you enter them in the web UI instead.
3. Open the deployment URL, log in with your phone number (Telegram sends you
   a code), and clean away.

**How it works:** your Telegram session is a Telethon `StringSession` kept in
your **browser's localStorage** and sent with each request — nothing is
stored on the server. Long cleanups run in **chunks** (200 messages or 10
chats per call) and the UI polls until done, so they work within Vercel's
function time limits (`maxDuration: 60` in `vercel.json`; raise it if you're
on a paid plan). Flood limits are surfaced to the UI, which waits and retries.

## Safety features

- CLI: dry run by default; `--yes` required to actually delete.
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
telegram_remover.py   CLI (argparse) — thin wrapper over remover_core
remover_core.py       shared async logic: chunked sweeps, leave, delete-account
api_common.py         request helpers + handlers for the serverless endpoints
api/*.py              one Vercel Python function per endpoint (Flask WSGI)
index.html            the web UI (vanilla JS, no build step)
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
