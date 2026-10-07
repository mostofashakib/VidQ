"""Tests for the Ollama provider's JSON text calls."""

import pytest

from app.services.llm_manager import OllamaProvider


@pytest.mark.asyncio
async def test_text_calls_turn_thinking_off_so_json_answers_are_not_starved():
    # Thinking models (e.g. gemma4) spend their whole budget reasoning in JSON
    # mode and return an empty answer, or run until the request times out.
    provider = OllamaProvider(model_name="gemma4:26b", host="http://ollama.local")
    sent = {}

    async def fake_post(payload):
        sent.update(payload)
        return {"message": {"content": '{"queries": ["a"]}'}}

    provider._post = fake_post

    assert await provider.call_text("plan") == {"queries": ["a"]}
    assert sent["think"] is False
    assert sent["format"] == "json"
