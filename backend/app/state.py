"""
Process-lifetime application state.

Keeping the LLM manager as a true singleton ensures the provider memory
(_working_provider_index) persists across all requests instead of being
recreated on every Depends() call.
"""
from app.config import get_settings
from app.services.llm_manager import (
    OllamaProvider, OpenAIProvider, AnthropicProvider, FallbackLLMManager
)

_settings = get_settings()

_providers = [OllamaProvider()]
if _settings.openai_api_key:
    _providers.append(OpenAIProvider(api_key=_settings.openai_api_key))
if _settings.anthropic_api_key:
    _providers.append(AnthropicProvider(api_key=_settings.anthropic_api_key))

llm_manager = FallbackLLMManager(_providers)
