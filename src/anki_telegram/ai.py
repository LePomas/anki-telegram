"""AI calls via the Claude Code CLI (`claude -p`), authenticated by your
Claude subscription — no Anthropic API key needed."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def call_claude(
    model: str,
    system: str,
    user: str,
    claude_bin: str = "claude",
    cwd: Path | None = None,
) -> str:
    # cwd points away from any project dir so the CLI doesn't pull in a
    # CLAUDE.md or local settings as context.
    cmd = [
        claude_bin,
        "-p",
        "--output-format", "json",
        "--model", model,
        "--system-prompt", system,
        "--max-turns", "1",
        user,
    ]
    last_err = ""
    for attempt in (1, 2):
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180, cwd=cwd
        )
        if proc.returncode == 0:
            data = json.loads(proc.stdout)
            if data.get("is_error"):
                raise RuntimeError(f"claude CLI error: {data.get('result', '')[:300]}")
            return data["result"]
        last_err = _extract_error_message(proc.stdout) or (proc.stderr or proc.stdout)[-300:]
        if attempt == 1:
            log.warning("claude CLI exit %s, retrying: %s", proc.returncode, last_err)
            time.sleep(3)
    raise RuntimeError(f"claude CLI failed: {last_err}")


def _extract_error_message(stdout: str) -> str:
    """Pull a human-readable reason out of the CLI's JSON stdout, if present."""
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return ""
    for key in ("result", "error", "message"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val[:300]
    return ""


def extract_json(text: str) -> dict:
    """Parse the first JSON object out of a model response."""
    match = _JSON_RE.search(text)
    if not match:
        raise ValueError(f"no JSON in model response: {text[:200]}")
    return json.loads(match.group(0))


ANALYZE_SYSTEM = """\
You are a German vocabulary assistant for an Anki flashcard workflow.
The user submits a German word or short phrase, possibly inflected or misspelled.
Reply with ONLY a JSON object:
{
  "display": "canonical dictionary form (nouns as 'der/die/das Wort', verbs as infinitive, keep phrases as-is)",
  "lemma": "bare lemma without article",
  "pos": "noun|verb|adjective|adverb|phrase|other",
  "search_terms": ["lemma plus up to 6 common inflected/conjugated forms, including the user's original spelling"]
}"""


def analyze_word(
    model: str, text: str, claude_bin: str = "claude", cwd: Path | None = None
) -> dict:
    raw = call_claude(model, ANALYZE_SYSTEM, text, claude_bin, cwd)
    result = extract_json(raw)
    terms = [t for t in result.get("search_terms", []) if t.strip()]
    if text not in terms:
        terms.append(text)
    result["search_terms"] = terms
    result.setdefault("display", text)
    result.setdefault("lemma", text)
    return result


DRAFT_SYSTEM = """\
You create Anki flashcards for a German learner. You are given the target deck's
field names and a few existing notes from that deck as examples.
Write a new note for the requested word that mirrors the examples EXACTLY:
same language per field (if examples translate into Spanish, use Latin American
Mexican Spanish — never peninsular Spanish), same formatting, same level of detail.
German nouns get their article (der/die/das); verbs are given as infinitive.
Leave audio/sound fields and ID/timestamp-like fields empty ("").
Reply with ONLY a JSON object mapping every field name to its value."""


def draft_fields(
    model: str,
    word_display: str,
    field_names: list[str],
    examples: list[dict[str, str]],
    deck: str,
    claude_bin: str = "claude",
    cwd: Path | None = None,
) -> dict[str, str]:
    user = (
        f"Deck: {deck}\n"
        f"Fields (in order): {json.dumps(field_names, ensure_ascii=False)}\n"
        f"Example notes:\n{json.dumps(examples, ensure_ascii=False, indent=1)}\n\n"
        f"Create a note for: {word_display}"
    )
    raw = call_claude(model, DRAFT_SYSTEM, user, claude_bin, cwd)
    drafted = extract_json(raw)
    return {name: str(drafted.get(name, "")) for name in field_names}
