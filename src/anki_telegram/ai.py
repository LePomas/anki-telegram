"""AI calls, dispatched to whichever provider is configured.

Default is the Claude Code CLI (`claude -p`), authenticated by your Claude
subscription — no API key needed. Optionally, set AI_PROVIDER to route
through a free-tier HTTP API instead (Gemini, OpenRouter, or a local Ollama)
using stdlib urllib — no extra SDK dependency."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)
_gemini_lock = threading.Lock()  # ponytail: global lock, per-model locks if throughput matters

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class AIConfig:
    provider: str  # "claude" | "gemini" | "openrouter" | "ollama" | "agy"
    model: str
    api_key: str = ""
    claude_bin: str = "claude"
    agy_bin: str = "agy"
    ollama_host: str = "http://localhost:11434"
    fallback_model: str = ""  # gemini only: used when the primary model hits a 429


def call_ai(cfg: AIConfig, system: str, user: str, cwd: Path | None = None) -> str:
    if cfg.provider == "claude":
        return _call_claude_cli(cfg.model, system, user, cfg.claude_bin, cwd)
    if cfg.provider == "gemini":
        return _call_gemini(cfg.model, system, user, cfg.api_key, cfg.fallback_model)
    if cfg.provider == "openrouter":
        return _call_openrouter(cfg.model, system, user, cfg.api_key)
    if cfg.provider == "ollama":
        return _call_ollama(cfg.model, system, user, cfg.ollama_host, cfg.api_key)
    if cfg.provider == "agy":
        return _call_agy_cli(cfg.model, system, user, cfg.agy_bin, cwd)
    raise ValueError(f"unknown AI_PROVIDER: {cfg.provider!r}")


def _call_claude_cli(
    model: str, system: str, user: str, claude_bin: str, cwd: Path | None
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


def _call_agy_cli(model: str, system: str, user: str, agy_bin: str, cwd: Path | None) -> str:
    # agy has no --system-prompt / --output-format flags (unlike claude), so fold
    # the system prompt into the single print-mode prompt and take stdout as-is.
    # -p/--print takes the prompt as its own value, so it must come last —
    # anything after it is swallowed as the prompt string, not a separate flag.
    cmd = [agy_bin, "--model", model, "-p", f"{system}\n\n{user}"]
    last_err = ""
    for attempt in (1, 2):
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180, cwd=cwd
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
        last_err = (proc.stderr or proc.stdout)[-300:]
        if attempt == 1:
            log.warning("agy CLI exit %s, retrying: %s", proc.returncode, last_err)
            time.sleep(3)
    raise RuntimeError(f"agy CLI failed: {last_err}")


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


def _post_json(url: str, body: dict, headers: dict, timeout: int = 60) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", **headers},
    )
    last_err = ""
    delay = 3.0
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            last_err = f"HTTP {exc.code}: {exc.read()[:300].decode(errors='replace')}"
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        pass
        except (urllib.error.URLError, TimeoutError) as exc:
            last_err = str(exc)
        if attempt == 1:
            log.warning("request to %s failed, retrying in %.1fs: %s", url, delay, last_err)
            time.sleep(delay)
    raise RuntimeError(f"request to {url} failed: {last_err}")


def _gemini_url(model: str, api_key: str) -> str:
    return (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        f"?key={api_key}"
    )


def _call_gemini(model: str, system: str, user: str, api_key: str, fallback_model: str = "") -> str:
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
    }
    with _gemini_lock:
        try:
            data = _post_json(_gemini_url(model, api_key), body, {})
        except RuntimeError as exc:
            if fallback_model and fallback_model != model and "HTTP 429" in str(exc):
                log.warning(
                    "gemini model %s rate-limited, falling back to %s", model, fallback_model
                )
                data = _post_json(_gemini_url(fallback_model, api_key), body, {})
            else:
                raise
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"unexpected Gemini response: {data}") from exc


def _call_openrouter(model: str, system: str, user: str, api_key: str) -> str:
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    data = _post_json(
        "https://openrouter.ai/api/v1/chat/completions",
        body,
        {"authorization": f"Bearer {api_key}"},
    )
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"unexpected OpenRouter response: {data}") from exc


def _call_ollama(model: str, system: str, user: str, host: str, api_key: str = "") -> str:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
    }
    # local Ollama needs no key; a remote instance (or Ollama Cloud) behind
    # auth does, via the same Bearer scheme as everyone else.
    headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
    data = _post_json(f"{host.rstrip('/')}/api/chat", body, headers, timeout=120)
    try:
        return data["message"]["content"]
    except KeyError as exc:
        raise RuntimeError(f"unexpected Ollama response: {data}") from exc


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


def analyze_word(cfg: AIConfig, text: str, cwd: Path | None = None) -> dict:
    raw = call_ai(cfg, ANALYZE_SYSTEM, text, cwd)
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

Keep every field monolingual: a German field holds ONLY German, a translation
field holds ONLY the translation. Never append the other language, or mix
languages within one field.

Only add an example sentence if the word is ambiguous, has multiple common
senses, or could otherwise be confused with something else — most words don't
need one. When you do add one, put it in BOTH the German field (as a German
example) and the translation field (that same example translated), each
appended after the headword/translation.

The German field is read aloud by text-to-speech verbatim, so keep it plainly
speakable: ordinary sentence punctuation only (periods, commas) — no quotation
marks, bullets, middots ("·"), slashes, or other decorative separators a TTS
engine would mispronounce or read as literal symbols.

Leave audio/sound fields and ID/timestamp-like fields empty ("").
Reply with ONLY a JSON object mapping every field name to its value."""


def draft_fields(
    cfg: AIConfig,
    word_display: str,
    field_names: list[str],
    examples: list[dict[str, str]],
    deck: str,
    cwd: Path | None = None,
) -> dict[str, str]:
    user = (
        f"Deck: {deck}\n"
        f"Fields (in order): {json.dumps(field_names, ensure_ascii=False)}\n"
        f"Example notes:\n{json.dumps(examples, ensure_ascii=False, indent=1)}\n\n"
        f"Create a note for: {word_display}"
    )
    raw = call_ai(cfg, DRAFT_SYSTEM, user, cwd)
    drafted = extract_json(raw)
    return {name: str(drafted.get(name, "")) for name in field_names}
