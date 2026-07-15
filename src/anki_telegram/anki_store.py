"""Headless Anki collection access: AnkiWeb sync, search, note creation."""

from __future__ import annotations

import html
import logging
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from anki.collection import Collection
from anki.sync_pb2 import SyncAuth, SyncCollectionResponse

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_SOUND_RE = re.compile(r"\[sound:[^\]]+\]")
_ID_FIELD_RE = re.compile(r"\bid\b")


def strip_html(value: str) -> str:
    return html.unescape(_SOUND_RE.sub("", _TAG_RE.sub(" ", value))).strip()


def dodge_gtts_abbreviation_bug(text: str) -> str:
    """gTTS silently drops the period after words ending in dr/jr/mr/mrs/ms/
    msgr/prof/sr/st (e.g. "sonst." -> "sonst"), mistaking them for English
    abbreviations like "Dr." or "St." regardless of lang= — this defeats the
    TTS pause. Double the period so one survives gTTS's own stripping."""
    from gtts.tokenizer.symbols import ABBREVIATIONS

    pattern = r"(" + "|".join(re.escape(a) for a in ABBREVIATIONS) + r")\.(?!\.)"
    return re.sub(pattern, r"\1..", text, flags=re.IGNORECASE)


_CASE_ABBR_RE = re.compile(r"\bAkk\.?\b|\bDat\.?\b")
_CASE_ABBR_EXPANSION = {"Akk": "Akkusativ", "Dat": "Dativ"}


def dodge_gtts_rection_shorthand(text: str) -> str:
    """Verb-rection notes write case shorthand like "warten auf + Akk." —
    gTTS reads that literally ("plus Akk Punkt") instead of speaking German.
    Expand Akk./Dat. to full words, drop the now-redundant "+", and read
    "/" between alternatives as "oder" so the phrase comes out speakable."""
    text = _CASE_ABBR_RE.sub(lambda m: _CASE_ABBR_EXPANSION[m.group().rstrip(".")], text)
    text = re.sub(r"\s*\+\s*", " ", text)
    text = re.sub(r"\s*/\s*", " oder ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def is_audio_field(name: str) -> bool:
    return "audio" in name.lower() or "sound" in name.lower()


def is_id_field(name: str) -> bool:
    return bool(_ID_FIELD_RE.search(name.lower()))


def main_field(field_names: list[str]) -> str | None:
    """First field that is not an ID or audio field — the word the card is about."""
    for name in field_names:
        if not is_audio_field(name) and not is_id_field(name):
            return name
    return None


def escape_search(term: str) -> str:
    """Escape a term for Anki search syntax and quote it."""
    term = term.replace("\\", "\\\\").replace('"', '\\"')
    term = term.replace("*", r"\*").replace("_", r"\_").replace(":", r"\:")
    return f'"{term}"'


@dataclass
class Match:
    note_id: int
    deck: str
    notetype: str
    main_value: str
    matched_term: str
    in_main_field: bool
    matched_fields: list[str] = field(default_factory=list)


@dataclass
class DeckFormat:
    deck: str
    notetype: str
    field_names: list[str]
    examples: list[dict[str, str]]  # cleaned field dicts from existing notes


class AnkiStore:
    def __init__(self, data_dir: Path, username: str, password: str):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.col_path = data_dir / "collection.anki2"
        self.col = Collection(str(self.col_path))
        self.username = username
        self.password = password
        self._auth: SyncAuth | None = None

    # -- sync ---------------------------------------------------------------

    def _get_auth(self) -> SyncAuth:
        if self._auth is None:
            self._auth = self.col.sync_login(self.username, self.password, None)
        return self._auth

    def sync(self, allow_full_download: bool) -> None:
        """Sync with AnkiWeb. allow_full_download=False after local writes,
        so a schema conflict never silently discards a just-created card."""
        auth = self._get_auth()
        out = self.col.sync_collection(auth, sync_media=True)
        if out.new_endpoint:
            self._auth = SyncAuth(hkey=auth.hkey, endpoint=out.new_endpoint)
        required = out.required
        R = SyncCollectionResponse.ChangesRequired
        if required in (R.FULL_SYNC, R.FULL_DOWNLOAD, R.FULL_UPLOAD):
            if not allow_full_download:
                raise RuntimeError(
                    "AnkiWeb requires a full sync; resolve it in Anki desktop first"
                )
            log.info("full download from AnkiWeb")
            self.col.close_for_full_sync()
            try:
                self.col.full_upload_or_download(
                    auth=self._auth or auth,
                    server_usn=out.server_media_usn,
                    upload=False,
                )
            finally:
                self.col.reopen(after_full_sync=True)
            self.col.sync_media(self._auth or auth)
        self._wait_media_sync()

    def _wait_media_sync(self, timeout: float = 300.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                status = self.col.media_sync_status()
            except Exception as exc:  # backend raises if media sync failed
                log.warning("media sync failed: %s", exc)
                return
            if not status.active:
                return
            time.sleep(1)
        log.warning("media sync still running after %ss; continuing", timeout)

    # -- introspection ------------------------------------------------------

    def deck_names(self) -> list[str]:
        return sorted(
            d.name for d in self.col.decks.all_names_and_ids(skip_empty_default=True)
        )

    def deck_format(self, deck: str) -> DeckFormat | None:
        """Note type and example notes of a deck, so new cards mirror it."""
        nids = self.col.find_notes(f"deck:{escape_search(deck)}")
        if not nids:
            return None
        # most common notetype among a sample
        counts: dict[int, int] = {}
        for nid in nids[-50:]:
            mid = self.col.get_note(nid).mid
            counts[mid] = counts.get(mid, 0) + 1
        mid = max(counts, key=lambda k: counts[k])
        nt = self.col.models.get(mid)
        field_names = [f["name"] for f in nt["flds"]]
        examples = []
        for nid in reversed(nids):
            note = self.col.get_note(nid)
            if note.mid != mid:
                continue
            examples.append(
                # ponytail: keep real tags (e.g. "<br>") so the AI mirrors the
                # deck's actual formatting, only sound tags are noise here
                {n: html.unescape(_SOUND_RE.sub("", v)).strip() for n, v in zip(field_names, note.fields)}
            )
            if len(examples) == 3:
                break
        return DeckFormat(deck, nt["name"], field_names, examples)

    # -- search -------------------------------------------------------------

    def search(self, terms: list[str], read_deck: str | None = None) -> list[Match]:
        matches: dict[int, Match] = {}
        deck_filter = f" deck:{escape_search(read_deck)}" if read_deck else ""
        for term in terms:
            for nid in self.col.find_notes(escape_search(term) + deck_filter):
                if nid in matches:
                    continue
                note = self.col.get_note(nid)
                nt = self.col.models.get(note.mid)
                field_names = [f["name"] for f in nt["flds"]]
                main = main_field(field_names)
                needle = term.lower()
                hit_fields = [
                    n
                    for n, v in zip(field_names, note.fields)
                    if not is_audio_field(n) and needle in strip_html(v).lower()
                ]
                if not hit_fields:
                    continue
                deck = self.col.decks.name(self.col.get_note(nid).cards()[0].did)
                main_value = strip_html(dict(zip(field_names, note.fields)).get(main, ""))
                matches[nid] = Match(
                    note_id=nid,
                    deck=deck,
                    notetype=nt["name"],
                    main_value=main_value,
                    matched_term=term,
                    in_main_field=main in hit_fields,
                    matched_fields=hit_fields,
                )
        return list(matches.values())

    # -- write --------------------------------------------------------------

    def add_note(self, deck: str, notetype: str, fields: dict[str, str]) -> int:
        nt = self.col.models.by_name(notetype)
        if nt is None:
            raise ValueError(f"unknown note type: {notetype}")
        note = self.col.new_note(nt)
        for name, value in fields.items():
            if name in note:
                note[name] = value
        did = self.col.decks.id_for_name(deck)
        if did is None:
            raise ValueError(f"unknown deck: {deck}")
        self.col.add_note(note, did)
        return note.id

    def add_audio(self, text: str) -> str:
        """Generate German TTS, store in media, return [sound:...] tag."""
        from gtts import gTTS

        speakable = dodge_gtts_abbreviation_bug(dodge_gtts_rection_shorthand(text))
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            gTTS(text=speakable, lang="de").write_to_fp(tmp)
            tmp_path = tmp.name
        try:
            fname = self.col.media.add_file(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        return f"[sound:{fname}]"
