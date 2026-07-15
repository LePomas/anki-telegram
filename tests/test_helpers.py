"""Checks for the pure logic: field heuristics, search escaping, JSON extraction,
and the AI provider dispatch (mocking subprocess/urlopen at the process boundary
so no real CLI or network call ever happens)."""

import io
import os
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from anki_telegram import ai
from anki_telegram.anki_store import (
    DeckFormat,
    dodge_gtts_abbreviation_bug,
    dodge_gtts_rection_shorthand,
    escape_search,
    is_audio_field,
    is_id_field,
    main_field,
    strip_html,
)
from anki_telegram.bot import (
    Bot,
    Session,
    Telegram,
    _ai_config_from_env,
    _friendly_ai_error,
    parse_callback_data,
)


def test_main_field():
    # Goethe Vocab List: skip Note ID, land on de_word
    assert main_field(["Note ID", "de_word", "de_sentence", "de_audio"]) == "de_word"
    assert main_field(["Front", "Back", "ID", "Audio"]) == "Front"
    assert main_field(["Video", "Back"]) == "Video"  # "id" substring must not match
    assert main_field(["Audio", "Sound"]) is None


def test_field_kind_helpers():
    assert is_audio_field("de_audio")
    assert is_audio_field("Sound")
    assert not is_audio_field("de_word")
    assert is_id_field("Note ID")
    assert not is_id_field("Video")


def test_escape_search():
    assert escape_search("laufen") == '"laufen"'
    assert escape_search('a"b') == '"a\\"b"'
    assert escape_search("a*b_c:d") == '"a\\*b\\_c\\:d"'


def test_strip_html():
    assert strip_html("<b>der Hund</b> [sound:x.mp3]") == "der Hund"
    assert strip_html("a&amp;b") == "a&b"


def test_dodge_gtts_abbreviation_bug():
    from gtts.tokenizer.pre_processors import abbreviations

    # gTTS's own preprocessor mistakes these for "Dr."/"St." and eats the period —
    # confirm our doubled period survives that stripping with exactly one left.
    for text in ["sonst. Beeil dich.", "Herbst. Es wird kalt."]:
        dodged = dodge_gtts_abbreviation_bug(text)
        assert abbreviations(dodged) == text
    # words that aren't gTTS abbreviations must pass through untouched
    assert dodge_gtts_abbreviation_bug("der Hund.") == "der Hund."


def test_dodge_gtts_rection_shorthand():
    # single case, trailing period
    assert dodge_gtts_rection_shorthand("verzichten auf + Akk.") == "verzichten auf Akkusativ."
    # two case alternatives joined by "/"
    assert (
        dodge_gtts_rection_shorthand("sich bewerben um + Akk. / bei + Dat.")
        == "sich bewerben um Akkusativ. oder bei Dativ."
    )
    # bare case abbreviation, no preposition, no period
    assert dodge_gtts_rection_shorthand("vertrauen + Dat") == "vertrauen Dativ"
    # already-spelled-out case name must not be double-expanded
    assert dodge_gtts_rection_shorthand("Verben mit Präposition + Dativ") == "Verben mit Präposition Dativ"
    # no shorthand present must pass through untouched
    assert dodge_gtts_rection_shorthand("der Hund") == "der Hund"


def test_extract_json():
    assert ai.extract_json('noise {"a": 1} noise') == {"a": 1}
    assert ai.extract_json('```json\n{"a": {"b": 2}}\n```') == {"a": {"b": 2}}


def test_extract_json_no_json_raises():
    try:
        ai.extract_json("no json here")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_parse_callback_data():
    assert parse_callback_data("opt:skip:3") == ("skip", 3, None)
    assert parse_callback_data("opt:create:42") == ("create", 42, None)
    assert parse_callback_data("deck:7:2") == ("deck_pick", 7, 2)
    try:
        parse_callback_data("bogus")
        assert False, "expected ValueError"
    except ValueError:
        pass


# -- ai.call_ai dispatch ------------------------------------------------------


def test_call_ai_unknown_provider_raises():
    # Arrange
    cfg = ai.AIConfig(provider="bogus", model="m")
    # Act / Assert
    try:
        ai.call_ai(cfg, "sys", "usr")
        assert False, "expected ValueError"
    except ValueError:
        pass


# -- ai._call_gemini (via call_ai) --------------------------------------------


def _fake_response(payload: dict):
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.read.return_value = ai.json.dumps(payload).encode()
    return resp


def _http_429(retry_after: str | None = None):
    hdrs = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError("url", 429, "Too Many Requests", hdrs, io.BytesIO(b"slow down"))


def test_call_gemini_missing_api_key_raises():
    # Arrange: null/empty input boundary
    cfg = ai.AIConfig(provider="gemini", model="gemini-2.5-flash", api_key="")
    # Act / Assert
    try:
        ai.call_ai(cfg, "sys", "usr")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "GEMINI_API_KEY" in str(exc)


def test_call_gemini_success():
    # Arrange
    cfg = ai.AIConfig(provider="gemini", model="gemini-2.5-flash", api_key="key")
    payload = {"candidates": [{"content": {"parts": [{"text": "hallo"}]}}]}
    with patch("urllib.request.urlopen", return_value=_fake_response(payload)):
        # Act
        result = ai.call_ai(cfg, "sys", "usr")
    # Assert
    assert result == "hallo"


def test_call_gemini_unexpected_response_raises():
    # Arrange: response missing the expected shape
    cfg = ai.AIConfig(provider="gemini", model="gemini-2.5-flash", api_key="key")
    with patch("urllib.request.urlopen", return_value=_fake_response({"nope": True})):
        # Act / Assert
        try:
            ai.call_ai(cfg, "sys", "usr")
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert "unexpected Gemini response" in str(exc)


def test_call_gemini_retry_after_header_controls_backoff():
    # Arrange: first attempt 429s with an explicit Retry-After, second succeeds
    cfg = ai.AIConfig(provider="gemini", model="m", api_key="key")
    payload = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
    with patch(
        "urllib.request.urlopen", side_effect=[_http_429("7"), _fake_response(payload)]
    ), patch("anki_telegram.ai.time.sleep") as mock_sleep:
        # Act
        result = ai.call_ai(cfg, "sys", "usr")
    # Assert
    assert result == "ok"
    mock_sleep.assert_called_once_with(7.0)


def test_call_gemini_invalid_retry_after_falls_back_to_default_delay():
    # Arrange: boundary — a non-numeric Retry-After must not crash the retry
    cfg = ai.AIConfig(provider="gemini", model="m", api_key="key")
    payload = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
    with patch(
        "urllib.request.urlopen",
        side_effect=[_http_429("not-a-number"), _fake_response(payload)],
    ), patch("anki_telegram.ai.time.sleep") as mock_sleep:
        # Act
        result = ai.call_ai(cfg, "sys", "usr")
    # Assert
    assert result == "ok"
    mock_sleep.assert_called_once_with(3.0)


def test_call_gemini_falls_back_to_secondary_model_after_rate_limit():
    # Arrange: primary model 429s on both its own attempts, fallback then succeeds
    cfg = ai.AIConfig(
        provider="gemini",
        model="gemini-2.5-pro",
        api_key="key",
        fallback_model="gemini-2.5-flash-lite",
    )
    payload = {"candidates": [{"content": {"parts": [{"text": "fallback ok"}]}}]}
    with patch(
        "urllib.request.urlopen",
        side_effect=[_http_429(), _http_429(), _fake_response(payload)],
    ), patch("anki_telegram.ai.time.sleep") as mock_sleep:
        # Act
        result = ai.call_ai(cfg, "sys", "usr")
    # Assert
    assert result == "fallback ok"
    mock_sleep.assert_called_once()  # one backoff inside the primary's own retry


def test_call_gemini_no_fallback_when_model_is_its_own_fallback():
    # Arrange: edge case — fallback_model equal to the primary is a no-op
    cfg = ai.AIConfig(provider="gemini", model="m", api_key="key", fallback_model="m")
    with patch(
        "urllib.request.urlopen", side_effect=[_http_429(), _http_429()]
    ), patch("anki_telegram.ai.time.sleep"):
        # Act / Assert
        try:
            ai.call_ai(cfg, "sys", "usr")
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert "HTTP 429" in str(exc)


def test_call_gemini_no_fallback_on_non_429_error():
    # Arrange: fallback configured, but the error condition doesn't qualify
    cfg = ai.AIConfig(provider="gemini", model="m", api_key="key", fallback_model="other")
    err = urllib.error.HTTPError("url", 500, "Server Error", {}, io.BytesIO(b"boom"))
    with patch("urllib.request.urlopen", side_effect=[err, err]), patch("anki_telegram.ai.time.sleep"):
        # Act / Assert
        try:
            ai.call_ai(cfg, "sys", "usr")
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert "HTTP 500" in str(exc)


# -- ai._call_agy_cli (via call_ai) -------------------------------------------


def _fake_proc(returncode=0, stdout="", stderr=""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def test_call_agy_cli_success():
    # Arrange
    cfg = ai.AIConfig(provider="agy", model="Gemini 3.5 Flash (Medium)", agy_bin="agy")
    with patch(
        "anki_telegram.ai.subprocess.run", return_value=_fake_proc(0, "hallo welt\n")
    ) as mock_run:
        # Act
        result = ai.call_ai(cfg, "sys", "usr")
    # Assert
    assert result == "hallo welt"
    cmd = mock_run.call_args[0][0]
    assert cmd == ["agy", "--model", "Gemini 3.5 Flash (Medium)", "-p", "sys\n\nusr"]


def test_call_agy_cli_retries_when_output_is_blank():
    # Arrange: edge case — returncode 0 with empty stdout must still retry
    cfg = ai.AIConfig(provider="agy", model="m", agy_bin="agy")
    with patch(
        "anki_telegram.ai.subprocess.run",
        side_effect=[_fake_proc(0, "   ", ""), _fake_proc(0, "done", "")],
    ), patch("anki_telegram.ai.time.sleep") as mock_sleep:
        # Act
        result = ai.call_ai(cfg, "sys", "usr")
    # Assert
    assert result == "done"
    mock_sleep.assert_called_once()


def test_call_agy_cli_exhausts_retries_raises():
    # Arrange: error condition — both attempts fail
    cfg = ai.AIConfig(provider="agy", model="m", agy_bin="agy")
    fail = _fake_proc(1, "", "boom")
    with patch("anki_telegram.ai.subprocess.run", side_effect=[fail, fail]), patch(
        "anki_telegram.ai.time.sleep"
    ):
        # Act / Assert
        try:
            ai.call_ai(cfg, "sys", "usr")
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert "boom" in str(exc)


# -- bot._ai_config_from_env ---------------------------------------------------


def _env_config(**env):
    # clear=True: isolate from the real process environment, not just overlay it
    with patch.dict(os.environ, env, clear=True):
        return _ai_config_from_env()


def test_ai_config_from_env_defaults_to_claude():
    # Arrange / Act: no env at all
    cfg = _env_config()
    # Assert
    assert cfg.provider == "claude"
    assert cfg.model == "haiku"
    assert cfg.fallback_model == ""


def test_ai_config_from_env_unknown_provider_raises():
    # Arrange / Act / Assert: error condition
    try:
        _env_config(AI_PROVIDER="bogus")
        assert False, "expected SystemExit"
    except SystemExit:
        pass


def test_ai_config_from_env_provider_is_trimmed_and_lowercased():
    # Arrange: boundary — stray whitespace/case in the env var
    cfg = _env_config(AI_PROVIDER=" Gemini ")
    # Assert
    assert cfg.provider == "gemini"


def test_ai_config_from_env_agy_reads_bin_and_model_override():
    # Arrange / Act
    cfg = _env_config(AI_PROVIDER="agy", AGY_BIN="/opt/agy/bin/agy", AGY_MODEL="custom-model")
    # Assert
    assert cfg.provider == "agy"
    assert cfg.agy_bin == "/opt/agy/bin/agy"
    assert cfg.model == "custom-model"


def test_post_json_error_never_leaks_query_string_secrets():
    # Arrange: url carries an API key in the query string, like Gemini's
    err = _http_429()
    with patch("urllib.request.urlopen", side_effect=[err, err]), patch("anki_telegram.ai.time.sleep"):
        # Act / Assert
        try:
            ai._post_json("https://example.com/v1/models/x:generateContent?key=SECRET123", {}, {})
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert "SECRET123" not in str(exc)
            assert "key=" not in str(exc)


# -- bot menu-keyboard collapsing ----------------------------------------------


def test_clear_keyboard_sends_explicit_empty_markup():
    # Arrange: edit()'s truthy-check on `keyboard` can't express "explicitly
    # empty", so clear_keyboard must go through editMessageReplyMarkup instead.
    tg = Telegram("token")
    with patch.object(tg, "call") as mock_call:
        # Act
        tg.clear_keyboard(42, 99)
    # Assert
    mock_call.assert_called_once_with(
        "editMessageReplyMarkup", chat_id=42, message_id=99, reply_markup={"inline_keyboard": []}
    )


def test_send_tracked_records_menu_message_ids_only_for_keyboards():
    # Arrange: one message with buttons, one plain — only the first should
    # be remembered as a menu to collapse later.
    fake_tg = MagicMock()
    fake_tg.send.side_effect = [{"message_id": 1}, {"message_id": 2}]
    fake_self = SimpleNamespace(tg=fake_tg, cfg=SimpleNamespace(chat_id=42), message_sid={})
    session = Session(sid=7)
    # Act
    Bot._send_tracked(fake_self, session, "menu", keyboard=[[{"text": "x", "callback_data": "y"}]])
    Bot._send_tracked(fake_self, session, "plain", keyboard=None)
    # Assert
    assert session.menu_message_ids == [1]
    assert fake_self.message_sid == {1: 7, 2: 7}


def test_collapse_menus_clears_every_tracked_message():
    # Arrange
    fake_tg = MagicMock()
    fake_self = SimpleNamespace(tg=fake_tg, cfg=SimpleNamespace(chat_id=42))
    session = Session(sid=7, menu_message_ids=[1, 2, 3])
    # Act
    Bot._collapse_menus(fake_self, session)
    # Assert
    assert fake_tg.clear_keyboard.call_args_list == [call(42, 1), call(42, 2), call(42, 3)]


def test_collapse_menus_on_empty_thread_is_a_no_op():
    # Arrange: boundary — a session that never sent a keyboard message
    fake_tg = MagicMock()
    fake_self = SimpleNamespace(tg=fake_tg, cfg=SimpleNamespace(chat_id=42))
    session = Session(sid=7)
    # Act
    Bot._collapse_menus(fake_self, session)
    # Assert
    fake_tg.clear_keyboard.assert_not_called()


def test_telegram_edit_keyboard_none_leaves_reply_markup_untouched():
    # Arrange: `None` means "don't mention reply_markup" — used by callers
    # (skip/cancel/confirm) that clear buttons via a separate _collapse_menus
    # call instead.
    tg = Telegram("token")
    with patch.object(tg, "call") as mock_call:
        # Act
        result = tg.edit(42, 99, "hi", keyboard=None)
    # Assert
    mock_call.assert_called_once_with(
        "editMessageText", chat_id=42, message_id=99, text="hi", parse_mode="HTML"
    )
    assert result == 99


def test_telegram_edit_keyboard_empty_list_clears_reply_markup():
    # Arrange: boundary — `[]` must still be sent (unlike `send`'s truthy
    # check), since it's the only way `_update_menu` can explicitly strip
    # buttons from an anchor message on a terminal edit.
    tg = Telegram("token")
    with patch.object(tg, "call") as mock_call:
        # Act
        result = tg.edit(42, 99, "hi", keyboard=[])
    # Assert
    mock_call.assert_called_once_with(
        "editMessageText",
        chat_id=42,
        message_id=99,
        text="hi",
        parse_mode="HTML",
        reply_markup={"inline_keyboard": []},
    )
    assert result == 99


def test_telegram_edit_falls_back_to_send_and_returns_new_message_id():
    # Arrange: error condition — Telegram rejects the edit (e.g. message too
    # old); edit() must fall back to send() and hand back *that* message's id
    # so callers can re-anchor.
    tg = Telegram("token")
    kb = [[{"text": "x", "callback_data": "y"}]]
    with patch.object(tg, "call", side_effect=RuntimeError("boom")), patch.object(
        tg, "send", return_value={"message_id": 123}
    ) as mock_send:
        # Act
        result = tg.edit(42, 99, "hi", keyboard=kb)
    # Assert
    mock_send.assert_called_once_with(42, "hi", kb)
    assert result == 123


def test_telegram_edit_not_modified_is_a_no_op_not_a_new_message():
    # Arrange: Telegram rejects an edit whose text+keyboard exactly match what's
    # already there (e.g. a redundant re-draft) — this must be treated as a
    # harmless no-op, not fall back to send() and duplicate the message.
    tg = Telegram("token")
    with patch.object(
        tg, "call", side_effect=RuntimeError("telegram editMessageText failed: message is not modified")
    ), patch.object(tg, "send") as mock_send:
        # Act
        result = tg.edit(42, 99, "hi", keyboard=None)
    # Assert
    mock_send.assert_not_called()
    assert result == 99


# -- bot._update_menu (anchor-message editing) ----------------------------------


def test_update_menu_sends_first_message_and_sets_anchor():
    # Arrange: no anchor yet — must behave like _send_tracked
    fake_tg = MagicMock()
    fake_tg.send.return_value = {"message_id": 5}
    fake_self = SimpleNamespace(tg=fake_tg, cfg=SimpleNamespace(chat_id=42), message_sid={})
    session = Session(sid=7)
    kb = [[{"text": "x", "callback_data": "y"}]]
    # Act
    Bot._update_menu(fake_self, session, "hi", kb)
    # Assert
    fake_tg.send.assert_called_once_with(42, "hi", kb)
    fake_tg.edit.assert_not_called()
    assert session.anchor_message_id == 5
    assert session.menu_message_ids == [5]
    assert fake_self.message_sid == {5: 7}


def test_update_menu_edits_anchor_in_place_on_later_calls():
    # Arrange: anchor already set from an earlier step
    fake_tg = MagicMock()
    fake_tg.edit.return_value = 5  # edit succeeded, same message id
    fake_self = SimpleNamespace(tg=fake_tg, cfg=SimpleNamespace(chat_id=42), message_sid={5: 7})
    session = Session(sid=7, anchor_message_id=5, menu_message_ids=[5])
    kb = [[{"text": "a", "callback_data": "b"}]]
    # Act
    Bot._update_menu(fake_self, session, "next step", kb)
    # Assert
    fake_tg.edit.assert_called_once_with(42, 5, "next step", kb)
    fake_tg.send.assert_not_called()
    assert session.anchor_message_id == 5
    assert session.menu_message_ids == [5]  # not duplicated


def test_update_menu_rebinds_anchor_when_edit_falls_back_to_a_new_message():
    # Arrange: Telegram.edit already resolved the API-level fallback and
    # returned the id of the message that now actually holds the text —
    # _update_menu must follow it, not keep pointing at the dead one.
    fake_tg = MagicMock()
    fake_tg.edit.return_value = 9
    fake_self = SimpleNamespace(tg=fake_tg, cfg=SimpleNamespace(chat_id=42), message_sid={5: 7})
    session = Session(sid=7, anchor_message_id=5, menu_message_ids=[5])
    kb = [[{"text": "a", "callback_data": "b"}]]
    # Act
    Bot._update_menu(fake_self, session, "next step", kb)
    # Assert
    assert session.anchor_message_id == 9
    assert fake_self.message_sid == {5: 7, 9: 7}
    assert session.menu_message_ids == [5, 9]


# -- bot.send_preview / remove_example (<br> example-sentence separator) ------


def test_send_preview_renders_br_as_a_line_break_not_raw_tag():
    # Arrange: drafted example sentences use a literal "<br>" tag (Anki fields
    # are HTML) — the Telegram preview must show it as a line break, not
    # HTML-escape it into visible "&lt;br&gt;"/"<br>" text.
    fmt = DeckFormat("Deutsch", "Basic", ["Front", "Back"], [])
    session = Session(sid=7, deck_format=fmt, draft={"Front": "Hallo.<br>Wie geht's?", "Back": "Hi."})
    fake_self = SimpleNamespace(_update_menu=MagicMock())
    # Act
    Bot.send_preview(fake_self, session)
    # Assert
    text = fake_self._update_menu.call_args[0][1]
    assert "Hallo.\nWie geht's?" in text
    assert "<br>" not in text
    assert "&lt;br&gt;" not in text


def test_send_preview_detects_existing_example_via_br_not_newline():
    # Arrange: regression — has_example used to check for "\n", but drafts
    # never contain a literal newline, only "<br>", so an existing example
    # went undetected and "Add example" stayed offered instead of "Remove".
    fmt = DeckFormat("Deutsch", "Basic", ["Front", "Back"], [])
    session = Session(sid=7, deck_format=fmt, draft={"Front": "Hallo.<br>Wie geht's?", "Back": "Hi.<br>Como estas."})
    fake_self = SimpleNamespace(_update_menu=MagicMock())
    # Act
    Bot.send_preview(fake_self, session)
    # Assert
    keyboard = fake_self._update_menu.call_args[0][2]
    button_texts = [b["text"] for row in keyboard for b in row]
    assert "➖ Remove example sentence" in button_texts
    assert "➕ Add example sentence" not in button_texts
    assert session.example_cache == session.draft
    assert session.example_cache_base == {"Front": "Hallo.", "Back": "Hi."}


def test_send_preview_no_example_offers_add_button():
    # Arrange: boundary — a draft with no "<br>" anywhere must not be
    # mistaken for having an example.
    fmt = DeckFormat("Deutsch", "Basic", ["Front", "Back"], [])
    session = Session(sid=7, deck_format=fmt, draft={"Front": "Hallo.", "Back": "Hi."})
    fake_self = SimpleNamespace(_update_menu=MagicMock())
    # Act
    Bot.send_preview(fake_self, session)
    # Assert
    keyboard = fake_self._update_menu.call_args[0][2]
    button_texts = [b["text"] for row in keyboard for b in row]
    assert "➕ Add example sentence" in button_texts
    assert session.example_cache is None


def test_remove_example_strips_br_suffix_from_every_field():
    # Arrange: regression — remove_example used to split on "\n", which never
    # matches, so the example sentence never actually got removed.
    session = Session(
        sid=7,
        deck_format=DeckFormat("Deutsch", "Basic", ["Front", "Back"], []),
        draft={"Front": "Hallo.<br>Wie geht's?", "Back": "Hi.<br>Como estas."},
    )
    fake_self = SimpleNamespace(send_preview=MagicMock())
    # Act
    Bot.remove_example(fake_self, session)
    # Assert
    assert session.draft == {"Front": "Hallo.", "Back": "Hi."}
    fake_self.send_preview.assert_called_once_with(session)


# -- bot._friendly_ai_error ----------------------------------------------------


def test_friendly_ai_error_detects_rate_limit_and_quota():
    # Arrange / Act / Assert
    assert "rate-limited" in _friendly_ai_error(RuntimeError("HTTP 429: too many requests"))
    assert "rate-limited" in _friendly_ai_error(RuntimeError("You exceeded your current quota"))


def test_friendly_ai_error_generic_fallback():
    # Arrange / Act
    result = _friendly_ai_error(RuntimeError("connection refused"))
    # Assert
    assert result == "AI backend didn't respond. Check the bot's logs for details."


def test_ai_config_from_env_gemini_fallback_defaults_and_overrides():
    # Arrange / Act
    default_cfg = _env_config(AI_PROVIDER="gemini")
    override_cfg = _env_config(AI_PROVIDER="gemini", GEMINI_FALLBACK_MODEL="gemini-2.0-flash")
    # Assert
    assert default_cfg.fallback_model == "gemini-flash-lite-latest"
    assert override_cfg.fallback_model == "gemini-2.0-flash"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("all checks passed")
