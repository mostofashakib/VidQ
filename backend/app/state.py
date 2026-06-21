"""
Process-lifetime application state.

Keeping managers as true singletons ensures provider memory
(_working_provider_index) persists across requests instead of being
recreated on every Depends() call.
"""
from app.config import get_settings
from app.services.llm_manager import (
    OllamaProvider, OpenAIProvider, AnthropicProvider, OpenRouterProvider, FallbackLLMManager
)
from app.services.transcription import (
    TranscriptionAdapter, FasterWhisperAdapter, WhisperOpenAIAdapter
)

_settings = get_settings()

_providers = [OllamaProvider(model_name=_settings.ollama_model, host=_settings.ollama_host)]
if _settings.openai_api_key:
    _providers.append(OpenAIProvider(api_key=_settings.openai_api_key, model=_settings.openai_model))
if _settings.anthropic_api_key:
    _providers.append(AnthropicProvider(api_key=_settings.anthropic_api_key, model=_settings.claude_model))
if _settings.openrouter_api_key:
    _providers.append(OpenRouterProvider(api_key=_settings.openrouter_api_key, model=_settings.openrouter_model))

# LLM_PROVIDER="" → auto-fallback chain across all configured providers
# LLM_PROVIDER="ollama"/"openai"/"anthropic"/"openrouter" → lock to that provider
llm_manager = FallbackLLMManager(_providers, default=_settings.llm_provider or None)

# Translate can use a separate provider lock while sharing the configured provider pool.
translate_llm_manager = FallbackLLMManager(
    _providers,
    default=_settings.translate_llm_provider or None,
)


def _build_transcription_adapter() -> TranscriptionAdapter:
    provider = _settings.transcription_provider.lower()
    if provider == "openai_whisper":
        return WhisperOpenAIAdapter(api_key=_settings.openai_api_key, model=_settings.transcription_model)
    return FasterWhisperAdapter(model=_settings.transcription_model, model_dir=_settings.whisper_model_dir)


# Transcription adapter for the Translate feature
# Controlled by TRANSCRIPTION_PROVIDER (faster_whisper | openai_whisper)
transcription_adapter: TranscriptionAdapter = _build_transcription_adapter()
