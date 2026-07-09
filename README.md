# anki-telegram

Telegram bot for German vocabulary. Send it a word; it checks your Anki
collection (via AnkiWeb sync) for existing cards — including other
forms/tenses and appearances inside other cards' example sentences — then
offers options: create a card that mirrors your target deck's format, write
the fields yourself, or skip. Cards get German TTS audio and are synced back
to AnkiWeb immediately.

Fully standalone: no Anki desktop, no AnkiConnect, no API key. Uses the
official `anki` Python library headlessly, plain `urllib` for Telegram,
`gTTS` for audio, and the [Claude Code CLI](https://claude.com/claude-code)
in headless mode (`claude -p`) for AI — authenticated by your existing
Claude subscription.

## Flow

```
you: läuft
bot: laufen
     ✅ laufen — Languages::Deutsch::Goethe A1
     📝 in einkaufen (de_sentence) — Netzwerk B1
     [⭐ Skip — already exists] [Create card] [Write it myself] [Cancel]
```

Picking **Create card** drafts all fields with Claude, mirroring the note
type, formatting, and languages of real notes from your chosen target deck,
shows a preview, and saves on confirmation (with generated audio in the
deck's audio field, if it has one).

## Setup

Requires Python 3.13+, [uv](https://docs.astral.sh/uv/), and the
[Claude Code CLI](https://claude.com/claude-code):

```sh
npm install -g @anthropic-ai/claude-code
claude   # log in once with your Claude account, then exit
```

(On a machine where you can't log in interactively, run `claude setup-token`
elsewhere and put the token in `.env` as `CLAUDE_CODE_OAUTH_TOKEN`.)

```sh
uv sync
cp .env.example .env   # then fill in the values
```

`.env` values:

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | Your numeric user ID (from [@userinfobot](https://t.me/userinfobot)); all other chats are ignored |
| `ANKIWEB_USERNAME` / `ANKIWEB_PASSWORD` | AnkiWeb account for sync |
| `CLAUDE_CODE_OAUTH_TOKEN` | Optional — long-lived token from `claude setup-token` if the host isn't logged in |
| `CLAUDE_MODEL` | Optional, passed to `claude --model` (default `haiku`) |
| `CLAUDE_BIN` | Optional path to the `claude` binary (useful under systemd) |
| `DATA_DIR` | Optional, defaults to `./data` (local collection, media, state) |

## Run

```sh
uv run anki-telegram            # long-poll loop
uv run anki-telegram --once     # process pending updates and exit
```

First run performs a full download of your collection from AnkiWeb into
`DATA_DIR`. The bot syncs before every lookup and after every card it
creates, so it stays consistent with your other devices.

## Commands

- `/deck` — pick the target deck for new cards (remembered across restarts)
- `/cancel` — abandon the current word
- `/help` — usage

## Deploy as a service

`anki-telegram.service` is a systemd user unit template:

```sh
mkdir -p ~/.config/systemd/user
cp anki-telegram.service ~/.config/systemd/user/
# edit WorkingDirectory/ExecStart paths if you cloned elsewhere
systemctl --user daemon-reload
systemctl --user enable --now anki-telegram
loginctl enable-linger "$USER"   # keep it running without an open session
```

## Tests

```sh
uv run python tests/test_helpers.py
```

## Notes

- Sync safety: after a card is written, the bot refuses full-sync
  resolutions (which could discard the new card) and asks you to resolve
  the conflict in Anki once — normal syncs then resume.
- The bot mirrors whatever note type a deck actually uses, so field names,
  languages, and formatting conventions come from your own notes, not from
  hardcoded templates.
