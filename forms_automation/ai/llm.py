"""One structured-output call to the local Ollama.

The reply is forced to match a JSON schema (Ollama's `format`), so callers get
a dict with known keys - or an LLMError, never free text. Used for head
creation (head_creator.py) and classification (jev.py).
"""

from __future__ import annotations

import json

import httpx

from forms_automation.ai import cover_letter
from forms_automation.ai.cover_letter import OLLAMA

DEFAULT_MODEL = cover_letter.DEFAULT_MODEL


class LLMError(Exception):
    pass


def chat_json(system: str, user: str, schema: dict, model: str = DEFAULT_MODEL,
              temperature: float = 0.0, timeout: float = 180) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        "format": schema,
        "options": {"temperature": temperature, "num_ctx": 8192},
    }
    try:
        r = httpx.post(f"{OLLAMA}/api/chat", json=body, timeout=timeout)
        r.raise_for_status()
        reply = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise LLMError(f"Ollama request failed: {exc}") from exc
    if "error" in reply:
        raise LLMError(f"Ollama error: {reply['error']}")
    content = reply.get("message", {}).get("content", "")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise LLMError(f"model didn't return JSON: {content[:200]!r}") from exc
    if not isinstance(data, dict):
        raise LLMError("model returned JSON that isn't an object")
    return data


def available(model: str = DEFAULT_MODEL) -> bool:
    """True when Ollama is running and has the model."""
    try:
        cover_letter.check_ollama(model)
    except cover_letter.CoverLetterError:
        return False
    return True
