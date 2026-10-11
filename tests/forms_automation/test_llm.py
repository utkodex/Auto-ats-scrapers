from __future__ import annotations

import json

import httpx
import pytest

from forms_automation.ai import llm

URL = f"{llm.OLLAMA}/api/chat"
SCHEMA = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}


def reply(content: str) -> dict:
    return {"message": {"role": "assistant", "content": content}, "done": True}


def test_chat_json_sends_schema_and_parses(httpx_mock):
    httpx_mock.add_response(url=URL, method="POST", json=reply('{"ok": true}'))
    assert llm.chat_json("sys", "user", SCHEMA) == {"ok": True}
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["format"] == SCHEMA
    assert body["stream"] is False
    assert body["options"]["temperature"] == 0
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["model"] == llm.DEFAULT_MODEL


def test_chat_json_invalid_json_raises(httpx_mock):
    httpx_mock.add_response(url=URL, method="POST", json=reply("not json"))
    with pytest.raises(llm.LLMError):
        llm.chat_json("sys", "user", SCHEMA)


def test_chat_json_http_error_raises(httpx_mock):
    httpx_mock.add_response(url=URL, method="POST", status_code=500)
    with pytest.raises(llm.LLMError):
        llm.chat_json("sys", "user", SCHEMA)


def test_chat_json_ollama_error_field_raises(httpx_mock):
    httpx_mock.add_response(url=URL, method="POST", json={"error": "model not found"})
    with pytest.raises(llm.LLMError):
        llm.chat_json("sys", "user", SCHEMA)


def test_chat_json_connection_error_raises(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    with pytest.raises(llm.LLMError):
        llm.chat_json("sys", "user", SCHEMA)


def test_available_false_when_ollama_down(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    assert llm.available() is False
