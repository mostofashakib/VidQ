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



@pytest.mark.asyncio
async def test_vision_calls_turn_thinking_off_too():
    provider = OllamaProvider(model_name="gemma4:26b", host="http://ollama.local")
    sent = {}

    async def fake_post(payload):
        sent.update(payload)
        return {"message": {"content": '{"play_selector": null}'}}

    provider._post = fake_post

    assert await provider.call_vision("find the player", "aGVsbG8=") == {"play_selector": None}
    assert sent["think"] is False


# ── Thinking only where a call asks for it ────────────────────────────────────

from types import SimpleNamespace  # noqa: E402

from app.services.llm_manager import AnthropicProvider, FallbackLLMManager, OpenAIProvider  # noqa: E402


def ollama_capturing(sent: list, content: str = '{"ok": true}') -> OllamaProvider:
    provider = OllamaProvider(model_name="gemma4:26b", host="http://ollama.local")

    async def fake_post(payload):
        sent.append(payload)
        return {"message": {"content": content}}

    provider._post = fake_post
    return provider


@pytest.mark.asyncio
async def test_calls_that_ask_for_reasoning_turn_thinking_on():
    sent = []
    provider = ollama_capturing(sent)

    await provider.call_text("judge", think=True)
    await provider.call_vision("pick a click target", "aGVsbG8=", think=True)

    assert [p["think"] for p in sent] == [True, True]


@pytest.mark.asyncio
async def test_translation_never_thinks():
    # Thinking models think by default when the flag is unset, which costs
    # seconds per chunk and changes nothing in a translation.
    sent = []

    assert await ollama_capturing(sent, "Bonjour").call_translate_text("Translate: Hello") == "Bonjour"
    assert sent[0]["think"] is False


@pytest.mark.asyncio
async def test_the_manager_passes_the_thinking_choice_to_the_provider():
    sent = []
    manager = FallbackLLMManager([ollama_capturing(sent)])

    await manager.execute_text("plan")
    await manager.execute("click", "aGVsbG8=", think=True)

    assert [p["think"] for p in sent] == [False, True]


class FakeAnthropicMessages:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        blocks = [SimpleNamespace(type="text", text='{"ok": true}')]
        if "thinking" in kwargs:
            blocks.insert(0, SimpleNamespace(type="thinking", thinking="let me see"))
        return SimpleNamespace(content=blocks)


@pytest.mark.asyncio
async def test_anthropic_thinks_on_request_and_reads_the_answer_after_its_reasoning():
    provider = AnthropicProvider(api_key="test", model="claude-test")
    provider.client = SimpleNamespace(messages=FakeAnthropicMessages())

    assert await provider.call_text("plan") == {"ok": True}
    assert await provider.call_vision("click", "aGVsbG8=", think=True) == {"ok": True}

    plain, reasoning = provider.client.messages.calls
    assert "thinking" not in plain
    assert reasoning["thinking"]["type"] == "enabled"
    assert reasoning["max_tokens"] > reasoning["thinking"]["budget_tokens"]


@pytest.mark.asyncio
async def test_providers_without_a_thinking_switch_accept_the_flag():
    class FakeCompletions:
        async def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))])

    provider = OpenAIProvider(api_key="test", model="gpt-test")
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))

    assert await provider.call_text("plan", think=True) == {"ok": True}
