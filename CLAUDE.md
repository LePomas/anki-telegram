# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Telegram bot that turns vocabulary messages into deduplicated Anki cards. Source/target language is a runtime setting (`LANG_SOURCE`/`LANG_TARGET`, changeable via `/settings`; default German→Spanish) rather than hardcoded. It runs headlessly against a real Anki collection (via the official `anki` Python library, no Anki desktop/AnkiConnect) and syncs that collection with AnkiWeb. Card content is drafted by AI, defaulting to the **Claude Code CLI in headless mode** (`claude -p`), not the Anthropic API — auth comes from the user's existing Claude subscription (or `CLAUDE_CODE_OAUTH_TOKEN`), so no API key handling exists for that path. `AI_PROVIDER` can route through the Anthropic API directly (a key, no CLI install) or Gemini/OpenRouter/Ollama/Antigravity instead.

## Commands

```sh
uv sync                                 # install deps (Python 3.13+, uv)
uv run anki-telegram                    # long-poll loop
uv run anki-telegram --once             # process pending Telegram updates and exit
uv run python tests/test_helpers.py     # run the test suite
```

There is no pytest/lint/type-check setup — `tests/test_helpers.py` is a standalone script (not pytest-based) that collects every `test_*` function from its own globals and runs them in a loop under `if __name__ == "__main__"`. Run it directly with `python`, not `pytest`. It covers pure logic (field-name heuristics, search-string escaping, JSON extraction) plus `Bot`'s message-bookkeeping methods (`_send_tracked`, `_update_menu`, `_collapse_menus`) exercised against `MagicMock`/`SimpleNamespace` stand-ins for `Telegram` — nothing touches a real Anki collection, AnkiWeb, or the `claude` CLI.

Config comes from a `.env` file (see `.env.example` for the full list) loaded manually by `bot.load_env_file` — required vars: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ANKIWEB_USERNAME`, `ANKIWEB_PASSWORD`.

## Architecture

Three modules, each with one job:

- **`bot.py`** — Telegram long-polling loop (`Bot.run`), a hand-rolled `Telegram` API client over `urllib` (no library), and the conversation state machine.
- **`anki_store.py`** — `AnkiStore` wraps a headless `anki.collection.Collection`: AnkiWeb sync, deck/notetype introspection, note search, note creation, TTS audio generation (`gTTS`).
- **`ai.py`** — `call_ai` dispatches to whichever `AI_PROVIDER` is configured (`claude -p --output-format json` by default, or an HTTP call via `_post_json` for anthropic/gemini/openrouter/ollama, or the `agy` CLI) for three tasks: `analyze_word` (canonicalize + generate search terms), `draft_fields` (write new note field values mirroring example notes from the target deck), and `add_example` (add an example sentence to an existing draft). Each task's system prompt is built by a `_*_system(source_name, target_name)` function that interpolates the configured language pair; German/Spanish-specific grammar notes (der/die/das article rule, Latin American Mexican Spanish dialect hint) are gated behind small lookup dicts (`_ARTICLE_HINTS`, `_GRAMMAR_NOTES`, `_DIALECT_HINTS`) keyed by language name, not forced onto every pair.

### Conversation flow (`bot.py`)

`Bot` holds one in-memory `Session` (phase: `idle` → `awaiting_choice` → one of `awaiting_deck` / `awaiting_confirm` / `awaiting_fields`). Session state is **not** persisted — a restart mid-conversation just means the user resends the word. The state that *is* persisted (`StateStore`, `data/state.json`) is the Telegram update offset, the chosen target deck, the AI provider/model, and the source/target language pair — all applied as overrides on top of env-var defaults in `Bot.__init__`.

Callback buttons encode intent as `callback_data` prefixes dispatched in `Bot.on_callback`: `opt:<action>` (skip/create/manual/edit/confirm/cancel) and `deck:<index>` (index into the last-sent deck list — stale if decks changed since, hence the bounds check).

Every keyboard step in a session (choice → deck pick → preview → settings submenus) edits one **anchor message** in place via `Bot._update_menu`/`Session.anchor_message_id`, instead of sending a new message each step — a busy chat with several words in flight doesn't fill up with stale button messages. The one exception is `prompt_manual`, which still sends a standalone message: Telegram can't attach `force_reply` to an edit, and that prompt needs the user's plain-text reply routed back via `message_sid`. `Telegram.edit` returns the id of the message that now holds the text (normally the same id, but a new one if Telegram rejected the edit and it fell back to `send` — `_update_menu` re-anchors to that id).

Flow for a new word (`on_new_word`): sync AnkiWeb (full download allowed) → `ai.analyze_word` → `AnkiStore.search` across the word's inflected forms → present matches split into "already a card" (hit in the note's main field) vs "appears inside another card" (hit in some other field, e.g. an example sentence) → offer skip/create/manual/cancel, all in that one anchor message.

"Create card" mirrors the target deck's *actual* note type and existing notes (`AnkiStore.deck_format`, sampled from the deck's most common notetype) rather than any hardcoded template — this is why field names, languages, and formatting conventions in drafted cards come from the user's own collection. The preview keyboard offers both "Write it myself" (blank manual entry) and "Edit fields" (manual entry prefilled with the AI draft) since the original choice message's "Write it myself" button is gone once the anchor has moved on to the preview.

### Field-kind heuristics (`anki_store.py`)

Every notetype's fields are classified by name pattern, not by a fixed schema, since decks can use arbitrary note types:
- `is_id_field`: matches `\bid\b` (word boundary — `"Video"` must not match).
- `is_audio_field`: `"audio"` or `"sound"` substring.
- `main_field`: first field that is neither of the above — the word/headword the card is about.

`create_card` writes TTS audio into a dedicated audio field if the notetype has one; if not (e.g. some decks intentionally have no audio field), it inlines the `[sound:...]` tag into the main field instead, matching that deck's existing convention.

### Sync safety

`AnkiStore.sync(allow_full_download)` is the one dangerous edge: AnkiWeb can demand a full sync (schema mismatch), which is destructive in one direction or the other. Full download is allowed before reads (start of `on_new_word`, bot startup) but **never** after a local write — `create_card` calls `sync(allow_full_download=False)`, so a forced full sync there raises instead of silently uploading-over or downloading-over the just-created card. The user has to resolve that conflict in Anki desktop once; normal syncs resume after.

### Deploying

`anki-telegram.service` is a systemd **user** unit template (`WorkingDirectory=%h/anki-telegram`, runs `uv run anki-telegram`). See the README's "Deploy as a service" section for the enable/linger steps. Note `CLAUDE_BIN` may need to be an absolute path under systemd, since `claude` often isn't on a service's `PATH`.
