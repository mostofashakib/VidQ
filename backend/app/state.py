"""
Process-lifetime application state.

Keeping the LLM manager as a true singleton ensures the provider memory
(_working_provider_index) persists across all requests instead of being
recreated on every Depends() call.
"""
from app.config import get_settings
from app.services.llm_manager import (
    OllamaProvider, OpenAIProvider, AnthropicProvider, OpenRouterProvider, FallbackLLMManager
)

_settings = get_settings()

_providers = [OllamaProvider(model_name=_settings.ollama_model, host=_settings.ollama_host)]
if _settings.openai_api_key:
    _providers.append(OpenAIProvider(api_key=_settings.openai_api_key))
if _settings.anthropic_api_key:
    _providers.append(AnthropicProvider(api_key=_settings.anthropic_api_key))
if _settings.openrouter_api_key:
    _providers.append(OpenRouterProvider(api_key=_settings.openrouter_api_key, model=_settings.openrouter_model))

# LLM_PROVIDER="" → auto-fallback chain across all configured providers
# LLM_PROVIDER="ollama"/"openai"/"anthropic"/"openrouter" → lock to that provider
llm_manager = FallbackLLMManager(_providers, default=_settings.llm_provider or None)
