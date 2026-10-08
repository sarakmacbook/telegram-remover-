# telegram-remover

Remove the shxt from **your own** Telegram account. A CLI that logs into your
account (MTProto user session) and can:

| Command | What it removes |
|---|---|
| `clean-messages` | Every message **you** sent, in every chat |
| `wipe` | The **full history** of one chat (both sides where Telegram permits) |
| `leave-all` | Every **group and channel** you joined (with a keep-list) |
| `delete-account` | Your **entire Telegram account**, permanently |

> ⚠️ Everything this tool does is **irreversible**. Every destructive command
> is a **dry run** unless you pass `--yes`.

## Why not a bot?

The official Telegram Bot API cannot read or delete your private chats, act
as you, or leave groups on your behalf. This tool uses a **user session**
(exactly like Telegram Desktop does) via [Telethon](https://github.com/LonamiWebs/Telethon).

## Setup

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

## Usage

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

## Safety features

- **Dry run by default** — destructive commands only show what *would* happen
  until you pass `--yes`.
- `delete-account` additionally requires typing `DELETE`.
- Messages are deleted in batches of 100 (Telegram's per-call limit).
- Flood limits are handled automatically — the tool sleeps and retries.
- If you are not an admin in a group/channel, deleting "for everyone" is not
  permitted; the tool falls back to deleting for yourself only and tells you
  how many failed.
- If a big cleanup gets cut off by rate limits, just **re-run the command** —
  it picks up where it left off.
- Secret chats are not supported by the Telegram API and are skipped.

## Development

```bash
python -m unittest test_telegram_remover -v
```

## Disclaimer

This tool only touches **your own account** and is intended for privacy
cleanup (e.g. before switching numbers or selling a device). It is not
affiliated with Telegram. Deleted data cannot be recovered — use at your own
risk.
