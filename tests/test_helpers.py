"""Checks for the pure logic: field heuristics, search escaping, JSON extraction."""

from anki_telegram import ai
from anki_telegram.anki_store import (
    escape_search,
    is_audio_field,
    is_id_field,
    main_field,
    strip_html,
)
from anki_telegram.bot import parse_callback_data


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


def test_extract_json():
    assert ai.extract_json('noise {"a": 1} noise') == {"a": 1}
    assert ai.extract_json('```json\n{"a": {"b": 2}}\n```') == {"a": {"b": 2}}


def test_parse_callback_data():
    assert parse_callback_data("opt:skip:3") == ("skip", 3, None)
    assert parse_callback_data("opt:create:42") == ("create", 42, None)
    assert parse_callback_data("deck:7:2") == ("deck_pick", 7, 2)
    try:
        parse_callback_data("bogus")
        assert False, "expected ValueError"
    except ValueError:
        pass


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("all checks passed")
