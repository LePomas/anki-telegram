"""Telegram bot: vocab in (source/target language configurable), deduplicated Anki cards out."""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import ai
from .anki_store import AnkiStore, DeckFormat, is_audio_field, is_id_field, main_field, strip_html

log = logging.getLogger("anki_telegram")

POLL_TIMEOUT = 50


# -- config -------------------------------------------------------------------


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


_DEFAULT_MODELS = {
    "claude": "haiku",
    # direct Anthropic API with a key — for hosts where installing the
    # Claude Code CLI isn't an option. Cheap tier, matching "claude"'s default.
    "anthropic": "claude-haiku-4-5",
    # pinned "gemini-2.5-flash" etc. 404 with "no longer available to new
    # users" on newer AI Studio accounts — the rolling alias stays current.
    "gemini": "gemini-flash-latest",
    # nvidia's Nemotron 3 Ultra: largest free-tier model on OpenRouter, verified
    # working and produces clean monolingual JSON output.
    "openrouter": "nvidia/nemotron-3-ultra-550b-a55b:free",
    # same model family, also free on Ollama Cloud with this account.
    "ollama": "nemotron-3-ultra",
    # Antigravity CLI, billed through the caller's Google AI Pro/Ultra plan.
    "agy": "Gemini 3.5 Flash (Medium)",
}

# Picks offered by /settings → "Change AI model". Providers absent here only
# have the one _DEFAULT_MODELS entry, so no picker is offered for them.
_MODELS_BY_PROVIDER = {
    "anthropic": ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8"],
    "gemini": ["gemini-flash-lite-latest", "gemini-flash-latest", "gemini-pro-latest"],
    "agy": [
        "Gemini 3.5 Flash (Low)",
        "Gemini 3.5 Flash (Medium)",
        "Gemini 3.5 Flash (High)",
        "Gemini 3.1 Pro (Low)",
        "Gemini 3.1 Pro (High)",
        "Claude Sonnet 4.6 (Thinking)",
        "Claude Opus 4.6 (Thinking)",
        "GPT-OSS 120B (Medium)",
    ],
}


def _ai_config_from_env(provider: str | None = None) -> ai.AIConfig:
    provider = (provider or os.environ.get("AI_PROVIDER", "claude")).strip().lower()
    if provider not in _DEFAULT_MODELS:
        raise SystemExit(
            f"unknown AI_PROVIDER {provider!r} — choose one of {sorted(_DEFAULT_MODELS)}"
        )
    model_env = f"{provider.upper()}_MODEL"
    return ai.AIConfig(
        provider=provider,
        model=os.environ.get(model_env, _DEFAULT_MODELS[provider]),
        api_key=os.environ.get(f"{provider.upper()}_API_KEY", ""),
        claude_bin=os.environ.get("CLAUDE_BIN", "claude"),
        agy_bin=os.environ.get("AGY_BIN", "agy"),
        ollama_host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        fallback_model=os.environ.get(
            "GEMINI_FALLBACK_MODEL", "gemini-flash-lite-latest" if provider == "gemini" else ""
        ),
    )


def _validate_provider(cfg: ai.AIConfig) -> str | None:
    """None if usable, else a user-facing reason it isn't."""
    if cfg.provider == "claude" and shutil.which(cfg.claude_bin) is None:
        return f"'{cfg.claude_bin}' not found — install Claude Code or set CLAUDE_BIN"
    if cfg.provider == "agy" and shutil.which(cfg.agy_bin) is None:
        return f"'{cfg.agy_bin}' not found — install the Antigravity CLI or set AGY_BIN"
    if cfg.provider in ("anthropic", "gemini", "openrouter") and not cfg.api_key:
        return f"AI_PROVIDER={cfg.provider} needs {cfg.provider.upper()}_API_KEY set"
    return None


def available_providers() -> list[str]:
    """Providers whose env-configured keys/binaries actually check out right now."""
    return [
        provider
        for provider in _DEFAULT_MODELS
        if _validate_provider(_ai_config_from_env(provider)) is None
    ]


def available_langs() -> dict[str, str]:
    """{code: English name} for every language gTTS can speak — the single
    source of truth for valid LANG_SOURCE/LANG_TARGET codes and for the
    /settings language pickers."""
    from gtts.lang import tts_langs

    return tts_langs()


def lang_name(code: str) -> str:
    """English display name for a gTTS language code, e.g. 'de' -> 'German'.
    Falls back to the code itself if unrecognized."""
    return available_langs().get(code, code)


def _validate_lang(code: str, var_name: str) -> str | None:
    """None if usable, else a user-facing reason it isn't."""
    if code not in available_langs():
        return f"{var_name}={code!r} isn't a gTTS-supported language code"
    return None


@dataclass
class Config:
    telegram_token: str
    chat_id: int
    ankiweb_username: str
    ankiweb_password: str
    ai: ai.AIConfig
    data_dir: Path
    read_deck: str
    write_deck: str
    lang_source: str
    lang_target: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            telegram_token=require_env("TELEGRAM_BOT_TOKEN"),
            chat_id=int(require_env("TELEGRAM_CHAT_ID")),
            ankiweb_username=require_env("ANKIWEB_USERNAME"),
            ankiweb_password=require_env("ANKIWEB_PASSWORD"),
            ai=_ai_config_from_env(),
            data_dir=Path(os.environ.get("DATA_DIR", "data")),
            read_deck=os.environ.get("ANKI_READ_DECK", "").strip(),
            write_deck=os.environ.get("ANKI_WRITE_DECK", "").strip(),
            lang_source=os.environ.get("LANG_SOURCE", "de").strip(),
            lang_target=os.environ.get("LANG_TARGET", "es").strip(),
        )


# -- telegram api -------------------------------------------------------------


class Telegram:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, http_timeout: int = 30, **params) -> dict | list:
        req = urllib.request.Request(
            f"{self.base}/{method}",
            data=json.dumps(params).encode(),
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            data = json.loads(resp.read())
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method} failed: {data}")
        return data["result"]

    def send(
        self,
        chat_id: int,
        text: str,
        keyboard: list | None = None,
        force_reply: bool = False,
    ) -> dict:
        params: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if keyboard:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        elif force_reply:
            # ties the user's plain-text answer back to this message via
            # reply_to_message, so concurrent words can each collect their
            # own manual fields without stepping on each other.
            params["reply_markup"] = {"force_reply": True, "selective": True}
        return self.call("sendMessage", **params)

    def edit(self, chat_id: int, message_id: int, text: str, keyboard: list | None = None) -> int:
        """Returns the id of the message that now holds `text` — same as
        `message_id` on success, or a new message's id if Telegram rejected
        the edit and this fell back to sending."""
        params: dict = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if keyboard is not None:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            self.call("editMessageText", **params)
            return message_id
        except (RuntimeError, urllib.error.HTTPError) as exc:
            if "message is not modified" in str(exc).lower():
                return message_id
            log.warning("editMessageText failed (%s); sending instead", exc)
            return self.send(chat_id, text, keyboard)["message_id"]

    def clear_keyboard(self, chat_id: int, message_id: int) -> None:
        # edit()'s `if keyboard:` can only ever set a non-empty keyboard, never
        # clear one — editMessageReplyMarkup with an explicit empty list is the
        # only way to actually remove the buttons from an older menu message.
        try:
            self.call(
                "editMessageReplyMarkup",
                chat_id=chat_id,
                message_id=message_id,
                reply_markup={"inline_keyboard": []},
            )
        except (RuntimeError, urllib.error.HTTPError) as exc:
            log.warning("clearing keyboard on %s failed: %s", message_id, exc)

    def typing(self, chat_id: int) -> None:
        try:
            self.call("sendChatAction", chat_id=chat_id, action="typing")
        except Exception:
            pass


@contextmanager
def keep_typing(tg: "Telegram", chat_id: int, interval: float = 4.0):
    """Re-send the 'typing…' action every few seconds for the duration of a
    slow call — Telegram's indicator fades ~5s after a single ping."""
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            tg.typing(chat_id)
            stop.wait(interval)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=1)


def btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def opt_btn(text: str, action: str, sid: int) -> dict:
    return btn(text, f"opt:{action}:{sid}")


def deck_btn(text: str, sid: int, index: int) -> dict:
    return btn(text, f"deck:{sid}:{index}")


def parse_callback_data(data: str) -> tuple[str, int, int | None]:
    """(action, sid, index) — index is only set for a deck pick."""
    if data.startswith("opt:"):
        _, action, sid_str = data.split(":", 2)
        return action, int(sid_str), None
    if data.startswith("deck:"):
        _, sid_str, idx_str = data.split(":", 2)
        return "deck_pick", int(sid_str), int(idx_str)
    if data.startswith("prov:"):
        _, sid_str, idx_str = data.split(":", 2)
        return "provider_pick", int(sid_str), int(idx_str)
    if data.startswith("model:"):
        _, sid_str, idx_str = data.split(":", 2)
        return "model_pick", int(sid_str), int(idx_str)
    if data.startswith("slang:"):
        _, sid_str, idx_str = data.split(":", 2)
        return "source_lang_pick", int(sid_str), int(idx_str)
    if data.startswith("tlang:"):
        _, sid_str, idx_str = data.split(":", 2)
        return "target_lang_pick", int(sid_str), int(idx_str)
    raise ValueError(f"unrecognized callback data: {data}")


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def _friendly_ai_error(exc: Exception) -> str:
    """Turn a raw provider exception into something a non-dev can act on."""
    msg = str(exc)
    if "429" in msg or "quota" in msg.lower() or "rate limit" in msg.lower():
        return "AI backend is rate-limited or out of quota — try again later, or switch AI_PROVIDER."
    return "AI backend didn't respond. Check the bot's logs for details."


# -- persisted state ----------------------------------------------------------


class StateStore:
    """Tiny JSON store: telegram offset + chosen target deck."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {}
        if path.is_file():
            try:
                self.data = json.loads(path.read_text())
            except json.JSONDecodeError:
                log.warning("corrupt state file, starting fresh")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        tmp.replace(self.path)


# -- bot ------------------------------------------------------------------------


@dataclass
class Session:
    """One word's conversation state, keyed by sid in Bot.sessions.
    Lost on restart — user just resends the word."""

    sid: int
    phase: str = "idle"  # idle | awaiting_choice | awaiting_deck | awaiting_confirm | awaiting_fields
    analysis: dict = field(default_factory=dict)
    deck_format: DeckFormat | None = None
    draft: dict = field(default_factory=dict)
    draft_model: str = ""  # "provider/model" that produced `draft`, "" if hand-typed
    example_cache: dict | None = None  # last draft seen with an example, for instant restore
    example_cache_base: dict | None = None  # that draft with the example line stripped
    decks: list[str] = field(default_factory=list)
    deck_pick_reason: str = ""  # "create" (continue to draft), "set" or "read_deck" (just save)
    providers: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    langs: list[str] = field(default_factory=list)  # reused by both source/target lang pickers
    menu_message_ids: list[int] = field(default_factory=list)  # every message sent with buttons
    anchor_message_id: int | None = None  # the one message this word's flow edits in place


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tg = Telegram(cfg.telegram_token)
        self.store = AnkiStore(cfg.data_dir, cfg.ankiweb_username, cfg.ankiweb_password)
        self.state = StateStore(cfg.data_dir / "state.json")
        if (
            cfg.write_deck
            and not self.state.data.get("deck")
            and cfg.write_deck in self.store.deck_names()
        ):
            self.state.data["deck"] = cfg.write_deck
            self.state.save()
        saved_provider = self.state.data.get("ai_provider")
        if saved_provider in _DEFAULT_MODELS:
            candidate = _ai_config_from_env(saved_provider)
            if _validate_provider(candidate) is None:
                self.cfg.ai = candidate
        saved_model = self.state.data.get("ai_model")
        if saved_model and saved_model in _MODELS_BY_PROVIDER.get(self.cfg.ai.provider, ()):
            self.cfg.ai = replace(self.cfg.ai, model=saved_model)
        saved_lang_source = self.state.data.get("lang_source")
        if saved_lang_source and saved_lang_source in available_langs():
            self.cfg.lang_source = saved_lang_source
        saved_lang_target = self.state.data.get("lang_target")
        if saved_lang_target and saved_lang_target in available_langs():
            self.cfg.lang_target = saved_lang_target
        self.sessions: dict[int, Session] = {}
        # telegram message_id -> sid, for every message that expects a reply
        # (buttons or free text) — lets /cancel and manual-field replies find
        # their session regardless of how many other words are in flight.
        self._next_sid = 1
        self.message_sid: dict[int, int] = {}

    def _new_session(self, **kwargs) -> Session:
        sid = self._next_sid
        self._next_sid += 1
        session = Session(sid=sid, **kwargs)
        self.sessions[sid] = session
        return session

    def _send_tracked(
        self, session: Session, text: str, keyboard: list | None = None, force_reply: bool = False
    ) -> None:
        msg = self.tg.send(self.cfg.chat_id, text, keyboard, force_reply=force_reply)
        self.message_sid[msg["message_id"]] = session.sid
        if keyboard:
            session.menu_message_ids.append(msg["message_id"])

    def _update_menu(self, session: Session, text: str, keyboard: list | None = None) -> None:
        """Edit this word's one anchor message in place (choice → deck pick →
        preview all reuse it) instead of spawning a new message each step —
        keeps a busy chat with several words in flight from filling up with
        stale button messages. Sends fresh only the first time."""
        if session.anchor_message_id is not None:
            new_id = self.tg.edit(self.cfg.chat_id, session.anchor_message_id, text, keyboard)
            if new_id != session.anchor_message_id:
                self.message_sid[new_id] = session.sid
                session.anchor_message_id = new_id
            if keyboard and session.anchor_message_id not in session.menu_message_ids:
                session.menu_message_ids.append(session.anchor_message_id)
            return
        msg = self.tg.send(self.cfg.chat_id, text, keyboard)
        self.message_sid[msg["message_id"]] = session.sid
        session.anchor_message_id = msg["message_id"]
        if keyboard:
            session.menu_message_ids.append(msg["message_id"])

    def effective_read_deck(self) -> str:
        return self.state.data.get("read_deck", self.cfg.read_deck)

    def _collapse_menus(self, session: Session) -> None:
        """Remove buttons from every menu this word's thread has sent so far,
        so a stale tap can't act on a session that's about to be gone."""
        for message_id in session.menu_message_ids:
            self.tg.clear_keyboard(self.cfg.chat_id, message_id)

    # -- update dispatch ----------------------------------------------------

    def handle_update(self, update: dict) -> None:
        if "message" in update:
            msg = update["message"]
            if msg.get("chat", {}).get("id") != self.cfg.chat_id:
                log.info("ignoring message from chat %s", msg.get("chat", {}).get("id"))
                return
            text = (msg.get("text") or "").strip()
            if text:
                reply_to = msg.get("reply_to_message", {}).get("message_id")
                self.on_text(text, reply_to)
        elif "callback_query" in update:
            cq = update["callback_query"]
            try:
                self.tg.call("answerCallbackQuery", callback_query_id=cq["id"])
            except Exception:
                pass
            if cq.get("from", {}).get("id") != self.cfg.chat_id:
                return
            msg = cq.get("message", {})
            self.on_callback(cq.get("data", ""), msg.get("message_id", 0))

    def on_text(self, text: str, reply_to: int | None = None) -> None:
        cmd = text.split("@")[0].lower() if text.startswith("/") else ""
        if cmd in ("/start", "/help"):
            self.tg.send(self.cfg.chat_id, self.help_text())
            return
        if cmd == "/cancel":
            self.on_cancel(reply_to)
            return
        if cmd == "/deck":
            session = self._new_session(phase="awaiting_deck")
            self.prompt_deck(session, reason="set")
            return
        if cmd == "/settings":
            self.open_settings()
            return

        sid = self.message_sid.get(reply_to) if reply_to is not None else None
        if sid is not None:
            session = self.sessions.get(sid)
            if session is None:
                self.tg.send(self.cfg.chat_id, "That word's session is gone — send it again.")
                return
            if session.phase == "awaiting_fields":
                self.on_manual_fields(session, text)
                return
            # reply landed on a non-text-collecting prompt — fall through and
            # treat the message as a new word.
        else:
            waiting = [s for s in self.sessions.values() if s.phase == "awaiting_fields"]
            if len(waiting) == 1:
                self.on_manual_fields(waiting[0], text)
                return
            if len(waiting) > 1:
                self.tg.send(
                    self.cfg.chat_id,
                    "Multiple words are waiting for manual fields — reply directly "
                    "to the one you mean.",
                )
                return
        for line in text.splitlines():
            line = line.strip()
            if line:
                self.on_new_word(line)

    def on_cancel(self, reply_to: int | None) -> None:
        sid = self.message_sid.get(reply_to) if reply_to is not None else None
        if sid is not None:
            session = self.sessions.pop(sid, None)
            if session is not None:
                self.tg.send(self.cfg.chat_id, "Cancelled that word.")
                self._collapse_menus(session)
            else:
                self.tg.send(self.cfg.chat_id, "Already finished — nothing to cancel.")
            return
        sessions = list(self.sessions.values())
        self.sessions.clear()
        self.tg.send(
            self.cfg.chat_id,
            f"Cancelled {len(sessions)} word(s) in flight." if sessions else "Nothing in flight.",
        )
        for session in sessions:
            self._collapse_menus(session)

    # -- flow: incoming word --------------------------------------------------

    def on_new_word(self, text: str) -> None:
        thinking_id = self.tg.send(self.cfg.chat_id, "🤔 Thinking…")["message_id"]
        with keep_typing(self.tg, self.cfg.chat_id):
            try:
                self.store.sync(allow_full_download=True)
            except Exception as exc:
                log.exception("sync failed")
                self.tg.edit(self.cfg.chat_id, thinking_id, f"⚠️ AnkiWeb sync failed: {esc(str(exc))}")
                return
            try:
                analysis = ai.analyze_word(
                    self.cfg.ai, text, lang_name(self.cfg.lang_source), cwd=self.cfg.data_dir
                )
            except Exception as exc:
                log.exception("analyze failed")
                self.tg.edit(
                    self.cfg.chat_id, thinking_id, f"⚠️ AI analysis failed: {esc(_friendly_ai_error(exc))}"
                )
                return

        matches = self.store.search(analysis["search_terms"], read_deck=self.effective_read_deck() or None)
        main_hits = [m for m in matches if m.in_main_field]
        sentence_hits = [m for m in matches if not m.in_main_field]

        lines = [f"<b>{esc(analysis['display'])}</b>"]
        if main_hits:
            lines.append("")
            lines.append("Already a card:")
            for m in main_hits[:5]:
                form = "" if m.matched_term.lower() == analysis["lemma"].lower() else f" (matched form: {esc(m.matched_term)})"
                lines.append(f"✅ <b>{esc(m.main_value)}</b> — {esc(m.deck)}{form}")
        if sentence_hits:
            lines.append("")
            lines.append("Appears inside another card:")
            for m in sentence_hits[:5]:
                lines.append(
                    f"📝 in <b>{esc(m.main_value)}</b> ({esc(', '.join(m.matched_fields))}) — {esc(m.deck)}"
                )
        if not matches:
            lines.append("")
            lines.append("Not in your collection.")

        session = self._new_session(phase="awaiting_choice", analysis=analysis)
        session.anchor_message_id = thinking_id
        self.message_sid[thinking_id] = session.sid

        create_label = "Create card" if main_hits else "⭐ Create card"
        keyboard = []
        if main_hits:
            keyboard.append([opt_btn("⭐ Skip — already exists", "skip", session.sid)])
        keyboard.append([opt_btn(create_label, "create", session.sid)])
        keyboard.append([opt_btn("Write it myself", "manual", session.sid)])
        keyboard.append([opt_btn("Cancel", "cancel", session.sid)])

        self._update_menu(session, "\n".join(lines), keyboard)

    # -- flow: option picked ---------------------------------------------------

    def on_callback(self, data: str, message_id: int) -> None:
        try:
            action, sid, index = parse_callback_data(data)
        except ValueError:
            log.warning("unrecognized callback data: %s", data)
            return
        session = self.sessions.get(sid)
        if session is None:
            self.tg.edit(
                self.cfg.chat_id, message_id, "This word's session is gone — send it again."
            )
            return
        if action == "deck_pick":
            assert index is not None
            self.on_deck_picked(session, index)
        elif action == "provider_pick":
            assert index is not None
            self.on_provider_picked(session, index)
        elif action == "model_pick":
            assert index is not None
            self.on_model_picked(session, index)
        elif action == "source_lang_pick":
            assert index is not None
            self.on_source_lang_picked(session, index)
        elif action == "target_lang_pick":
            assert index is not None
            self.on_target_lang_picked(session, index)
        elif action == "menu_deck":
            self.prompt_deck(session, reason="set")
        elif action == "menu_read_deck":
            self.prompt_read_deck(session)
        elif action == "menu_source_lang":
            self.prompt_source_lang(session)
        elif action == "menu_target_lang":
            self.prompt_target_lang(session)
        elif action == "menu_provider":
            self.prompt_provider(session)
        elif action == "menu_model":
            self.prompt_model(session)
        elif action == "menu_close":
            self.sessions.pop(sid, None)
            self.tg.edit(self.cfg.chat_id, message_id, "Settings closed.")
            self._collapse_menus(session)
        elif action == "skip":
            self.sessions.pop(sid, None)
            self.tg.edit(self.cfg.chat_id, message_id, "👍 Skipped — card already exists.")
            self._collapse_menus(session)
        elif action == "cancel":
            self.sessions.pop(sid, None)
            self.tg.edit(self.cfg.chat_id, message_id, "Cancelled.")
            self._collapse_menus(session)
        elif action == "create":
            deck = self.state.data.get("deck")
            if deck and deck in self.store.deck_names():
                self.draft_and_preview(session, deck)
            else:
                self.prompt_deck(session, reason="create")
        elif action == "manual":
            self.prompt_manual(session)
        elif action == "edit":
            self.prompt_manual(session, prefill=session.draft)
        elif action == "add_example":
            self.add_example(session)
        elif action == "remove_example":
            self.remove_example(session)
        elif action == "confirm":
            self.create_card(session, message_id)
        else:
            log.warning("unrecognized opt action: %s", action)

    def prompt_deck(self, session: Session, reason: str) -> None:
        decks = self.store.deck_names()
        if not decks:
            self.sessions.pop(session.sid, None)
            self.tg.send(self.cfg.chat_id, "No decks in your collection.")
            self._collapse_menus(session)
            return
        session.decks = decks
        session.deck_pick_reason = reason
        if session.phase == "idle":
            session.phase = "awaiting_deck"
        current = self.state.data.get("deck")
        keyboard = [
            [deck_btn(("✅ " if d == current else "") + d, session.sid, i)]
            for i, d in enumerate(decks)
        ]
        self._update_menu(session, "Pick the target deck:", keyboard)

    def on_deck_picked(self, session: Session, index: int) -> None:
        decks = session.decks or self.store.deck_names()
        if not 0 <= index < len(decks):
            self.tg.send(self.cfg.chat_id, "Stale deck list — try again.")
            return
        deck = decks[index]
        if session.deck_pick_reason == "read_deck":
            self.state.data["read_deck"] = deck
            self.state.save()
            self.sessions.pop(session.sid, None)
            self.tg.edit(
                self.cfg.chat_id,
                session.anchor_message_id,
                f"Search scope: <b>{esc(deck or 'whole collection')}</b>",
                [],
            )
            return
        self.state.data["deck"] = deck
        self.state.save()
        if session.deck_pick_reason == "create" and session.analysis:
            self.draft_and_preview(session, deck)
        else:
            self.sessions.pop(session.sid, None)
            self.tg.edit(
                self.cfg.chat_id, session.anchor_message_id, f"Target deck: <b>{esc(deck)}</b>", []
            )

    def prompt_read_deck(self, session: Session) -> None:
        decks = ["", *self.store.deck_names()]  # "" = whole collection
        session.decks = decks
        session.deck_pick_reason = "read_deck"
        current = self.effective_read_deck()
        keyboard = [
            [deck_btn(("✅ " if d == current else "") + (d or "🌐 Whole collection"), session.sid, i)]
            for i, d in enumerate(decks)
        ]
        self._update_menu(session, "Pick search scope (where to look for existing cards):", keyboard)

    def prompt_provider(self, session: Session) -> None:
        providers = available_providers()
        if not providers:
            self.tg.send(self.cfg.chat_id, "No AI provider is fully configured (missing keys/binaries).")
            return
        session.providers = providers
        current = self.cfg.ai.provider
        keyboard = [
            [btn(("✅ " if p == current else "") + p, f"prov:{session.sid}:{i}")]
            for i, p in enumerate(providers)
        ]
        self._update_menu(session, "Pick AI provider:", keyboard)

    def on_provider_picked(self, session: Session, index: int) -> None:
        providers = session.providers
        if not 0 <= index < len(providers):
            self.tg.send(self.cfg.chat_id, "Stale provider list — try again.")
            return
        provider = providers[index]
        candidate = _ai_config_from_env(provider)
        err = _validate_provider(candidate)
        if err:
            self.tg.send(self.cfg.chat_id, f"⚠️ {esc(err)}")
            return
        self.cfg.ai = candidate
        self.state.data["ai_provider"] = provider
        self.state.data.pop("ai_model", None)
        self.state.save()
        self.sessions.pop(session.sid, None)
        self.tg.edit(
            self.cfg.chat_id,
            session.anchor_message_id,
            f"AI provider: <b>{esc(f'{candidate.provider}/{candidate.model}')}</b>",
            [],
        )

    def prompt_model(self, session: Session) -> None:
        models = _MODELS_BY_PROVIDER.get(self.cfg.ai.provider)
        if not models:
            self.tg.send(
                self.cfg.chat_id,
                f"'{self.cfg.ai.provider}' only has one model available: {esc(self.cfg.ai.model)}",
            )
            return
        session.models = models
        current = self.cfg.ai.model
        keyboard = [
            [btn(("✅ " if m == current else "") + m, f"model:{session.sid}:{i}")]
            for i, m in enumerate(models)
        ]
        self._update_menu(session, "Pick AI model:", keyboard)

    def on_model_picked(self, session: Session, index: int) -> None:
        models = session.models
        if not 0 <= index < len(models):
            self.tg.send(self.cfg.chat_id, "Stale model list — try again.")
            return
        model = models[index]
        self.cfg.ai = replace(self.cfg.ai, model=model)
        self.state.data["ai_model"] = model
        self.state.save()
        self.sessions.pop(session.sid, None)
        self.tg.edit(
            self.cfg.chat_id,
            session.anchor_message_id,
            f"AI model: <b>{esc(f'{self.cfg.ai.provider}/{model}')}</b>",
            [],
        )

    def prompt_source_lang(self, session: Session) -> None:
        langs = available_langs()
        codes = sorted(langs, key=lambda c: (langs[c], c))
        session.langs = codes
        current = self.cfg.lang_source
        keyboard = [
            [btn(("✅ " if c == current else "") + langs[c], f"slang:{session.sid}:{i}")]
            for i, c in enumerate(codes)
        ]
        self._update_menu(session, "Pick source language (the language you send words in):", keyboard)

    def on_source_lang_picked(self, session: Session, index: int) -> None:
        codes = session.langs
        if not 0 <= index < len(codes):
            self.tg.send(self.cfg.chat_id, "Stale language list — try again.")
            return
        code = codes[index]
        self.cfg.lang_source = code
        self.state.data["lang_source"] = code
        self.state.save()
        self.sessions.pop(session.sid, None)
        self.tg.edit(
            self.cfg.chat_id,
            session.anchor_message_id,
            f"Source language: <b>{esc(lang_name(code))}</b>",
            [],
        )

    def prompt_target_lang(self, session: Session) -> None:
        langs = available_langs()
        codes = sorted(langs, key=lambda c: (langs[c], c))
        session.langs = codes
        current = self.cfg.lang_target
        keyboard = [
            [btn(("✅ " if c == current else "") + langs[c], f"tlang:{session.sid}:{i}")]
            for i, c in enumerate(codes)
        ]
        self._update_menu(session, "Pick target language (what cards get translated into):", keyboard)

    def on_target_lang_picked(self, session: Session, index: int) -> None:
        codes = session.langs
        if not 0 <= index < len(codes):
            self.tg.send(self.cfg.chat_id, "Stale language list — try again.")
            return
        code = codes[index]
        self.cfg.lang_target = code
        self.state.data["lang_target"] = code
        self.state.save()
        self.sessions.pop(session.sid, None)
        self.tg.edit(
            self.cfg.chat_id,
            session.anchor_message_id,
            f"Target language: <b>{esc(lang_name(code))}</b>",
            [],
        )

    def open_settings(self) -> None:
        session = self._new_session()
        deck = self.state.data.get("deck") or "(not set)"
        read_deck = self.effective_read_deck() or "whole collection"
        lines = [
            "<b>Settings</b>",
            f"Target deck: {esc(deck)}",
            f"Search scope: {esc(read_deck)}",
            f"Source language: {esc(lang_name(self.cfg.lang_source))}",
            f"Target language: {esc(lang_name(self.cfg.lang_target))}",
            f"AI provider: {esc(f'{self.cfg.ai.provider}/{self.cfg.ai.model}')}",
        ]
        keyboard = [
            [opt_btn("Change target deck", "menu_deck", session.sid)],
            [opt_btn("Change search scope", "menu_read_deck", session.sid)],
            [opt_btn("Change source language", "menu_source_lang", session.sid)],
            [opt_btn("Change target language", "menu_target_lang", session.sid)],
            [opt_btn("Change AI provider", "menu_provider", session.sid)],
            [opt_btn("Change AI model", "menu_model", session.sid)],
            [opt_btn("Close", "menu_close", session.sid)],
        ]
        self._update_menu(session, "\n".join(lines), keyboard)

    # -- flow: draft + preview -------------------------------------------------

    def draft_and_preview(self, session: Session, deck: str) -> None:
        fmt = self.store.deck_format(deck)
        if fmt is None:
            self.tg.send(
                self.cfg.chat_id,
                f"Deck <b>{esc(deck)}</b> is empty — no format to mirror. Pick another with /deck.",
            )
            return
        self._update_menu(session, "🤔 Drafting card…", [])
        try:
            with keep_typing(self.tg, self.cfg.chat_id):
                draft = ai.draft_fields(
                    self.cfg.ai,
                    session.analysis.get("display", ""),
                    fmt.field_names,
                    fmt.examples,
                    deck,
                    lang_name(self.cfg.lang_source),
                    lang_name(self.cfg.lang_target),
                    cwd=self.cfg.data_dir,
                )
        except Exception as exc:
            log.exception("draft failed")
            self._update_menu(session, f"⚠️ AI draft failed: {esc(_friendly_ai_error(exc))}", [])
            return
        session.deck_format = fmt
        session.draft = draft
        session.draft_model = f"{self.cfg.ai.provider}/{self.cfg.ai.model}"
        self.send_preview(session)

    def send_preview(self, session: Session) -> None:
        fmt = session.deck_format
        assert fmt is not None
        lines = [f"Preview — <b>{esc(fmt.deck)}</b> ({esc(fmt.notetype)}):"]
        if session.draft_model:
            lines.append(f"<i>drafted by {esc(session.draft_model)}</i>")
        lines.append("")
        for name in fmt.field_names:
            if is_audio_field(name):
                lines.append(f"<i>{esc(name)}</i>: 🔊 generated on save")
            elif is_id_field(name) or not session.draft.get(name):
                continue
            else:
                lines.append(f"<i>{esc(name)}</i>: {esc(session.draft[name].replace('<br>', chr(10)))}")
        main = main_field(fmt.field_names)
        has_example = bool(main) and "<br>" in session.draft.get(main, "")
        if has_example:
            session.example_cache = dict(session.draft)
            session.example_cache_base = {
                n: v.split("<br>", 1)[0] for n, v in session.draft.items()
            }
        keyboard = [[opt_btn("⭐ Save card", "confirm", session.sid)]]
        if has_example:
            keyboard.append([opt_btn("➖ Remove example sentence", "remove_example", session.sid)])
        else:
            keyboard.append([opt_btn("➕ Add example sentence", "add_example", session.sid)])
        keyboard.append(
            [opt_btn("Write it myself", "manual", session.sid), opt_btn("Edit fields", "edit", session.sid)]
        )
        keyboard.append([opt_btn("Cancel", "cancel", session.sid)])
        session.phase = "awaiting_confirm"
        self._update_menu(session, "\n".join(lines), keyboard)

    def add_example(self, session: Session) -> None:
        fmt = session.deck_format
        if fmt is None or not session.draft:
            self.tg.send(self.cfg.chat_id, "Nothing to add to — send the word again.")
            return
        # if nothing changed since we last had a cached example, restore it
        # instead of burning another AI call.
        if session.example_cache is not None and session.example_cache_base == session.draft:
            session.draft = dict(session.example_cache)
            self.send_preview(session)
            return
        self._update_menu(session, "🤔 Adding example…", [])
        try:
            with keep_typing(self.tg, self.cfg.chat_id):
                draft = ai.add_example(
                    self.cfg.ai,
                    fmt.field_names,
                    fmt.examples,
                    session.draft,
                    fmt.deck,
                    lang_name(self.cfg.lang_source),
                    cwd=self.cfg.data_dir,
                )
        except Exception as exc:
            log.exception("add example failed")
            self._update_menu(session, f"⚠️ AI draft failed: {esc(_friendly_ai_error(exc))}", [])
            return
        session.draft = draft
        self.send_preview(session)

    def remove_example(self, session: Session) -> None:
        if not session.draft:
            self.tg.send(self.cfg.chat_id, "Nothing to remove — send the word again.")
            return
        session.draft = {
            n: v.split("<br>", 1)[0] if "<br>" in v else v for n, v in session.draft.items()
        }
        self.send_preview(session)

    # -- flow: manual fields -----------------------------------------------------

    def editable_fields(self, session: Session) -> list[str]:
        fmt = session.deck_format
        assert fmt is not None
        return [
            n for n in fmt.field_names if not is_audio_field(n) and not is_id_field(n)
        ]

    def prompt_manual(self, session: Session, prefill: dict | None = None) -> None:
        if session.deck_format is None:
            deck = self.state.data.get("deck")
            fmt = self.store.deck_format(deck) if deck else None
            if fmt is None:
                self.prompt_deck(session, reason="create")
                return
            session.deck_format = fmt
        names = self.editable_fields(session)
        lines = ["Reply to this message, one line per field, in this order:", ""]
        for n in names:
            lines.append(f"<i>{esc(n)}</i>")
        if prefill:
            values = "\n".join(esc((prefill or {}).get(n, "")) for n in names)
            lines.append(f"<pre>{values}</pre>")
            lines.append("(tap to copy, edit, reply with values only, one per line)")
        session.phase = "awaiting_fields"
        self._send_tracked(session, "\n".join(lines), force_reply=True)

    def on_manual_fields(self, session: Session, text: str) -> None:
        names = self.editable_fields(session)
        values = [line.strip() for line in text.split("\n")]
        if len(values) > len(names):
            self.tg.send(
                self.cfg.chat_id,
                f"Got {len(values)} lines but the deck has {len(names)} fields — try again.",
            )
            return
        draft = dict(zip(names, values))
        for n in names:
            draft.setdefault(n, "")
        session.draft = draft
        session.draft_model = ""  # hand-typed, not model output
        self.send_preview(session)

    # -- flow: save ---------------------------------------------------------------

    def create_card(self, session: Session, message_id: int) -> None:
        fmt = session.deck_format
        if fmt is None or not session.draft:
            self.tg.edit(self.cfg.chat_id, message_id, "Nothing to save — send the word again.")
            return
        fields = {n: session.draft.get(n, "") for n in fmt.field_names}
        main = main_field(fmt.field_names)
        audio_fields = [n for n in fmt.field_names if is_audio_field(n)]
        self.tg.edit(self.cfg.chat_id, message_id, "🤔 Saving…", [])
        with keep_typing(self.tg, self.cfg.chat_id):
            if main and fields.get(main):
                try:
                    # ponytail: TTS covers the main field only; extend to sentence audio if wanted
                    sound = self.store.add_audio(strip_html(fields[main]), self.cfg.lang_source)
                    if audio_fields:
                        fields[audio_fields[0]] = sound
                    else:
                        # notetype has no dedicated audio field (e.g. KontextB1Plus Basic) —
                        # inline the sound tag into the main field, matching that deck's
                        # existing convention of "text [sound:...]" in one field.
                        fields[main] = f"{fields[main]} {sound}"
                except Exception as exc:
                    log.warning("TTS failed, saving without audio: %s", exc)
            try:
                self.store.add_note(fmt.deck, fmt.notetype, fields)
                self.store.sync(allow_full_download=False)
            except Exception as exc:
                log.exception("card creation failed")
                self.tg.edit(self.cfg.chat_id, message_id, f"⚠️ Failed: {esc(str(exc))}")
                return
        word = fields.get(main, "") if main else ""
        self.sessions.pop(session.sid, None)
        self.tg.edit(
            self.cfg.chat_id,
            message_id,
            f"✅ Saved <b>{esc(word)}</b> to <b>{esc(fmt.deck)}</b> and synced.",
        )
        self._collapse_menus(session)

    def help_text(self) -> str:
        source = lang_name(self.cfg.lang_source)
        return (
            f"Send me a {source} word or phrase.\n"
            "I check your Anki collection for it (including other forms and example "
            "sentences), then offer options: create a card in your target deck, write "
            "one yourself, or skip.\n\n"
            "You can send several words before answering any of them — each gets its "
            "own thread. When asked to write fields yourself, reply directly to that "
            "prompt (tap it, then Reply) so I know which word it's for.\n\n"
            "Commands:\n"
            "/settings — configure target deck, search scope, source/target "
            "language, and AI provider\n"
            "/deck — choose the target deck (shortcut into /settings)\n"
            "/cancel — reply to a word's message to abandon just that one, or send "
            "bare to abandon everything in flight\n"
            "/help — this message"
        )

    def _register_commands(self) -> None:
        try:
            self.tg.call(
                "setMyCommands",
                commands=[
                    {"command": "settings", "description": "target deck, search scope, language, AI provider"},
                    {"command": "deck", "description": "choose the target deck"},
                    {"command": "cancel", "description": "abandon a word (reply) or everything in flight"},
                    {"command": "help", "description": "show usage"},
                ],
            )
        except Exception:
            log.exception("setMyCommands failed")

    # -- main loop ------------------------------------------------------------------

    def run(self, once: bool = False) -> None:
        log.info("initial AnkiWeb sync")
        self.store.sync(allow_full_download=True)
        self._register_commands()
        log.info("starting; %d decks in collection", len(self.store.deck_names()))
        while True:
            offset = self.state.data.get("offset", 0)
            try:
                updates = self.tg.call(
                    "getUpdates",
                    http_timeout=POLL_TIMEOUT + 10,
                    offset=offset,
                    allowed_updates=["message", "callback_query"],
                    timeout=0 if once else POLL_TIMEOUT,
                )
            except Exception as exc:
                log.warning("getUpdates failed: %s", exc)
                time.sleep(5)
                continue
            for update in updates:
                self.state.data["offset"] = update["update_id"] + 1
                self.state.save()
                try:
                    self.handle_update(update)
                except Exception:
                    log.exception("error handling update")
                    try:
                        self.tg.send(self.cfg.chat_id, "⚠️ Internal error — check the logs.")
                    except Exception:
                        pass
            if once:
                return


def main() -> None:
    parser = argparse.ArgumentParser(description="Anki vocab Telegram bot")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--once", action="store_true", help="process pending updates and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    load_env_file(args.env_file)
    cfg = Config.from_env()
    err = _validate_provider(cfg.ai)
    if err:
        raise SystemExit(err)
    for var_name, code in (("LANG_SOURCE", cfg.lang_source), ("LANG_TARGET", cfg.lang_target)):
        lang_err = _validate_lang(code, var_name)
        if lang_err:
            raise SystemExit(lang_err)
    Bot(cfg).run(once=args.once)


if __name__ == "__main__":
    main()
