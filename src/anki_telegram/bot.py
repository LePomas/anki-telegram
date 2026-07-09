"""Telegram bot: German vocab in, deduplicated Anki cards out."""

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
from dataclasses import dataclass, field
from pathlib import Path

from . import ai
from .anki_store import AnkiStore, DeckFormat, is_audio_field, is_id_field, main_field

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


@dataclass
class Config:
    telegram_token: str
    chat_id: int
    ankiweb_username: str
    ankiweb_password: str
    claude_model: str
    claude_bin: str
    data_dir: Path

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            telegram_token=require_env("TELEGRAM_BOT_TOKEN"),
            chat_id=int(require_env("TELEGRAM_CHAT_ID")),
            ankiweb_username=require_env("ANKIWEB_USERNAME"),
            ankiweb_password=require_env("ANKIWEB_PASSWORD"),
            claude_model=os.environ.get("CLAUDE_MODEL", "haiku"),
            claude_bin=os.environ.get("CLAUDE_BIN", "claude"),
            data_dir=Path(os.environ.get("DATA_DIR", "data")),
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

    def send(self, chat_id: int, text: str, keyboard: list | None = None) -> dict:
        params: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if keyboard:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        return self.call("sendMessage", **params)

    def edit(self, chat_id: int, message_id: int, text: str, keyboard: list | None = None) -> None:
        params: dict = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if keyboard:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            self.call("editMessageText", **params)
        except (RuntimeError, urllib.error.HTTPError) as exc:
            log.warning("editMessageText failed (%s); sending instead", exc)
            self.send(chat_id, text, keyboard)

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


def esc(text: str) -> str:
    return html.escape(text, quote=False)


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

HELP = (
    "Send me a German word or phrase.\n"
    "I check your Anki collection for it (including other forms and example "
    "sentences), then offer options: create a card in your target deck, write "
    "one yourself, or skip.\n\n"
    "Commands:\n"
    "/deck — choose the target deck\n"
    "/cancel — abandon the current word\n"
    "/help — this message"
)


@dataclass
class Session:
    """In-memory conversation state. Lost on restart — user just resends the word."""

    phase: str = "idle"  # idle | awaiting_choice | awaiting_deck | awaiting_confirm | awaiting_fields
    analysis: dict = field(default_factory=dict)
    deck_format: DeckFormat | None = None
    draft: dict = field(default_factory=dict)
    decks: list[str] = field(default_factory=list)
    deck_pick_reason: str = ""  # "create" (continue to draft) or "set" (just save)


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tg = Telegram(cfg.telegram_token)
        self.store = AnkiStore(cfg.data_dir, cfg.ankiweb_username, cfg.ankiweb_password)
        self.state = StateStore(cfg.data_dir / "state.json")
        self.session = Session()

    # -- update dispatch ----------------------------------------------------

    def handle_update(self, update: dict) -> None:
        if "message" in update:
            msg = update["message"]
            if msg.get("chat", {}).get("id") != self.cfg.chat_id:
                log.info("ignoring message from chat %s", msg.get("chat", {}).get("id"))
                return
            text = (msg.get("text") or "").strip()
            if text:
                self.on_text(text)
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

    def on_text(self, text: str) -> None:
        cmd = text.split("@")[0].lower() if text.startswith("/") else ""
        if cmd in ("/start", "/help"):
            self.tg.send(self.cfg.chat_id, HELP)
            return
        if cmd == "/cancel":
            self.session = Session()
            self.tg.send(self.cfg.chat_id, "Cancelled.")
            return
        if cmd == "/deck":
            self.prompt_deck(reason="set")
            return
        if self.session.phase == "awaiting_fields":
            self.on_manual_fields(text)
            return
        self.on_new_word(text)

    # -- flow: incoming word --------------------------------------------------

    def on_new_word(self, text: str) -> None:
        with keep_typing(self.tg, self.cfg.chat_id):
            try:
                self.store.sync(allow_full_download=True)
            except Exception as exc:
                log.exception("sync failed")
                self.tg.send(self.cfg.chat_id, f"⚠️ AnkiWeb sync failed: {esc(str(exc))}")
                return
            try:
                analysis = ai.analyze_word(
                    self.cfg.claude_model,
                    text,
                    claude_bin=self.cfg.claude_bin,
                    cwd=self.cfg.data_dir,
                )
            except Exception as exc:
                log.exception("analyze failed")
                self.tg.send(self.cfg.chat_id, f"⚠️ AI analysis failed: {esc(str(exc))}")
                return

        matches = self.store.search(analysis["search_terms"])
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

        create_label = "Create card" if main_hits else "⭐ Create card"
        keyboard = []
        if main_hits:
            keyboard.append([btn("⭐ Skip — already exists", "opt:skip")])
        keyboard.append([btn(create_label, "opt:create")])
        keyboard.append([btn("Write it myself", "opt:manual")])
        keyboard.append([btn("Cancel", "opt:cancel")])

        self.session = Session(phase="awaiting_choice", analysis=analysis)
        self.tg.send(self.cfg.chat_id, "\n".join(lines), keyboard)

    # -- flow: option picked ---------------------------------------------------

    def on_callback(self, data: str, message_id: int) -> None:
        s = self.session
        if data == "opt:skip":
            self.session = Session()
            self.tg.edit(self.cfg.chat_id, message_id, "👍 Skipped — card already exists.")
        elif data == "opt:cancel":
            self.session = Session()
            self.tg.edit(self.cfg.chat_id, message_id, "Cancelled.")
        elif data == "opt:create":
            deck = self.state.data.get("deck")
            if deck and deck in self.store.deck_names():
                self.draft_and_preview(deck)
            else:
                self.prompt_deck(reason="create")
        elif data == "opt:manual":
            self.prompt_manual()
        elif data == "opt:edit":
            self.prompt_manual(prefill=s.draft)
        elif data == "opt:confirm":
            self.create_card(message_id)
        elif data.startswith("deck:"):
            self.on_deck_picked(int(data.split(":", 1)[1]))
        else:
            log.warning("unknown callback data: %s", data)

    def prompt_deck(self, reason: str) -> None:
        decks = self.store.deck_names()
        if not decks:
            self.tg.send(self.cfg.chat_id, "No decks in your collection.")
            return
        self.session.decks = decks
        self.session.deck_pick_reason = reason
        if self.session.phase == "idle":
            self.session.phase = "awaiting_deck"
        current = self.state.data.get("deck")
        keyboard = [
            [btn(("✅ " if d == current else "") + d, f"deck:{i}")]
            for i, d in enumerate(decks)
        ]
        self.tg.send(self.cfg.chat_id, "Pick the target deck:", keyboard)

    def on_deck_picked(self, index: int) -> None:
        decks = self.session.decks or self.store.deck_names()
        if not 0 <= index < len(decks):
            self.tg.send(self.cfg.chat_id, "Stale deck list — try again.")
            return
        deck = decks[index]
        self.state.data["deck"] = deck
        self.state.save()
        if self.session.deck_pick_reason == "create" and self.session.analysis:
            self.draft_and_preview(deck)
        else:
            self.session = Session()
            self.tg.send(self.cfg.chat_id, f"Target deck: <b>{esc(deck)}</b>")

    # -- flow: draft + preview -------------------------------------------------

    def draft_and_preview(self, deck: str) -> None:
        fmt = self.store.deck_format(deck)
        if fmt is None:
            self.tg.send(
                self.cfg.chat_id,
                f"Deck <b>{esc(deck)}</b> is empty — no format to mirror. Pick another with /deck.",
            )
            return
        try:
            with keep_typing(self.tg, self.cfg.chat_id):
                draft = ai.draft_fields(
                    self.cfg.claude_model,
                    self.session.analysis.get("display", ""),
                    fmt.field_names,
                    fmt.examples,
                    deck,
                    claude_bin=self.cfg.claude_bin,
                    cwd=self.cfg.data_dir,
                )
        except Exception as exc:
            log.exception("draft failed")
            self.tg.send(self.cfg.chat_id, f"⚠️ AI draft failed: {esc(str(exc))}")
            return
        self.session.deck_format = fmt
        self.session.draft = draft
        self.send_preview()

    def send_preview(self) -> None:
        s = self.session
        fmt = s.deck_format
        assert fmt is not None
        lines = [f"Preview — <b>{esc(fmt.deck)}</b> ({esc(fmt.notetype)}):", ""]
        for name in fmt.field_names:
            if is_audio_field(name):
                lines.append(f"<i>{esc(name)}</i>: 🔊 generated on save")
            elif is_id_field(name) or not s.draft.get(name):
                continue
            else:
                lines.append(f"<i>{esc(name)}</i>: {esc(s.draft[name])}")
        keyboard = [
            [btn("⭐ Save card", "opt:confirm")],
            [btn("Edit fields", "opt:edit"), btn("Cancel", "opt:cancel")],
        ]
        s.phase = "awaiting_confirm"
        self.tg.send(self.cfg.chat_id, "\n".join(lines), keyboard)

    # -- flow: manual fields -----------------------------------------------------

    def editable_fields(self) -> list[str]:
        fmt = self.session.deck_format
        assert fmt is not None
        return [
            n for n in fmt.field_names if not is_audio_field(n) and not is_id_field(n)
        ]

    def prompt_manual(self, prefill: dict | None = None) -> None:
        if self.session.deck_format is None:
            deck = self.state.data.get("deck")
            fmt = self.store.deck_format(deck) if deck else None
            if fmt is None:
                self.prompt_deck(reason="create")
                return
            self.session.deck_format = fmt
        names = self.editable_fields()
        lines = ["Send one line per field, in this order:", ""]
        for n in names:
            lines.append(f"<i>{esc(n)}</i>")
        if prefill:
            values = "\n".join(esc((prefill or {}).get(n, "")) for n in names)
            lines.append(f"<pre>{values}</pre>")
            lines.append("(tap to copy, edit, send back — values only, one per line)")
        self.session.phase = "awaiting_fields"
        self.tg.send(self.cfg.chat_id, "\n".join(lines))

    def on_manual_fields(self, text: str) -> None:
        names = self.editable_fields()
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
        self.session.draft = draft
        self.send_preview()

    # -- flow: save ---------------------------------------------------------------

    def create_card(self, message_id: int) -> None:
        s = self.session
        fmt = s.deck_format
        if fmt is None or not s.draft:
            self.tg.edit(self.cfg.chat_id, message_id, "Nothing to save — send the word again.")
            return
        fields = {n: s.draft.get(n, "") for n in fmt.field_names}
        main = main_field(fmt.field_names)
        audio_fields = [n for n in fmt.field_names if is_audio_field(n)]
        with keep_typing(self.tg, self.cfg.chat_id):
            if main and fields.get(main):
                try:
                    # ponytail: TTS covers the main field only; extend to sentence audio if wanted
                    sound = self.store.add_audio(fields[main])
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
        self.session = Session()
        self.tg.edit(
            self.cfg.chat_id,
            message_id,
            f"✅ Saved <b>{esc(word)}</b> to <b>{esc(fmt.deck)}</b> and synced.",
        )

    # -- main loop ------------------------------------------------------------------

    def run(self, once: bool = False) -> None:
        log.info("initial AnkiWeb sync")
        self.store.sync(allow_full_download=True)
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
    if shutil.which(cfg.claude_bin) is None:
        raise SystemExit(
            f"'{cfg.claude_bin}' not found — install Claude Code "
            "(npm install -g @anthropic-ai/claude-code) and log in, "
            "or set CLAUDE_BIN to its path"
        )
    Bot(cfg).run(once=args.once)


if __name__ == "__main__":
    main()
