import abc
import json
import logging
import httpx
from openai import AsyncOpenAI
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

def _parse_json(text: str) -> dict:
    """Extract the first JSON object from an LLM response string."""
    start = text.find('{')
    end = text.rfind('}') + 1
    if start == -1 or end == 0:
        raise ValueError(f"No valid JSON found in LLM response: {text[:200]}")
    return json.loads(text[start:end])


def _strip_data_uri(image_b64: str) -> str:
    """Strip the data:image/...;base64, prefix if present, returning raw base64."""
    img = image_b64.strip()
    if "," in img:
        img = img.split(",", 1)[1]
    return img


def _get_media_type(image_b64: str) -> str:
    """Detect media type from base64 string by inspecting magic bytes."""
    img = _strip_data_uri(image_b64)

    # Magic byte signatures (base64-encoded first bytes):
    # JPEG  -> /9j/
    # PNG   -> iVBORw0KGgo
    # WebP  -> UklGR
    if img.startswith("/9j/"):
        mtype = "image/jpeg"
    elif img.startswith("iVBOR"):
        mtype = "image/png"
    elif img.startswith("UklGR"):
        mtype = "image/webp"
    else:
        mtype = "image/png"  # Safe default

    logger.debug(f"Detected image media type: {mtype} (prefix: {img[:12]}...)")
    return mtype


class LLMProvider(abc.ABC):
    @abc.abstractmethod
    async def call_text(self, prompt: str) -> dict:
        """Text-only call — no image. Used for HTML-based navigation analysis."""
        pass

    @abc.abstractmethod
    async def call_vision(self, prompt: str, image_b64: str) -> dict:
        """Vision call — prompt + screenshot. Used for metadata extraction."""
        pass

    # Convenience alias kept for backwards compat
    async def extract_metadata(self, prompt: str, image_b64: str) -> dict:
        return await self.call_vision(prompt, image_b64)


class OllamaProvider(LLMProvider):
    def __init__(
        self,
        model_name: str = "qwen3.5:latest",
        host: str = "http://127.0.0.1:11434",
    ):
        self.model_name = model_name
        self.host = host.rstrip("/")

    async def _post(self, payload: dict) -> dict:
        url = f"{self.host}/api/chat"
        async with httpx.AsyncClient(timeout=180) as client:
            res = await client.post(url, json=payload)
            try:
                res.raise_for_status()
            except httpx.HTTPStatusError as e:
                body = res.text[:1000]
                raise RuntimeError(
                    f"Ollama error {res.status_code}: {body}"
                ) from e
            return res.json()

    @staticmethod
    def _parse(content: str) -> dict:
        """
        Robust parser for Ollama output.
        qwen3.x emits <think>…</think> reasoning blocks before the JSON —
        strip those first, then fall back to the generic _parse_json extractor.
        """
        import re as _re
        # Strip <think>…</think> blocks (qwen3 chain-of-thought output)
        content = _re.sub(r'<think>.*?</think>', '', content, flags=_re.DOTALL).strip()
        # Try direct parse first (fast path)
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass
        # Extract first {...} block
        return _parse_json(content)

    async def call_text(self, prompt: str) -> dict:
        payload = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }
        logger.info(f"Ollama call_text → model={self.model_name}")
        data = await self._post(payload)
        content = data["message"]["content"].strip()
        logger.debug(f"Ollama raw (first 300): {content[:300]}")
        try:
            return self._parse(content)
        except Exception as e:
            logger.error(f"Ollama JSON parse failed. Raw: {content[:500]}")
            raise e

    async def call_vision(self, prompt: str, image_b64: str) -> dict:
        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [_strip_data_uri(image_b64)],
                }
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }
        logger.info(f"Ollama call_vision → model={self.model_name}")
        try:
            data = await self._post(payload)
            content = data["message"]["content"].strip()
            logger.debug(f"Ollama vision raw (first 300): {content[:300]}")
            return self._parse(content)
        except Exception as e:
            # qwen3.5 is text-only — retry without the image
            logger.warning(f"Ollama vision failed ({e}), retrying text-only…")
            payload["messages"][0].pop("images", None)
            try:
                data = await self._post(payload)
                content = data["message"]["content"].strip()
                logger.info("Ollama text-only fallback succeeded.")
                return self._parse(content)
            except Exception as e2:
                logger.error(f"Ollama text-only fallback also failed: {e2}")
                raise e2


class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str):
        self.client = AsyncOpenAI(api_key=api_key)

    async def call_text(self, prompt: str) -> dict:
        response = await self.client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000,
            temperature=0.2,
        )
        return _parse_json(response.choices[0].message.content.strip())

    async def call_vision(self, prompt: str, image_b64: str) -> dict:
        response = await self.client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{_get_media_type(image_b64)};base64,{_strip_data_uri(image_b64)}"}},
                    ],
                }
            ],
            max_tokens=1000,
            temperature=0.2,
        )
        return _parse_json(response.choices[0].message.content.strip())


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str):
        self.client = AsyncAnthropic(api_key=api_key)

    async def call_text(self, prompt: str) -> dict:
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        return _parse_json(response.content[0].text.strip())

    async def call_vision(self, prompt: str, image_b64: str) -> dict:
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": _get_media_type(image_b64),
                                "data": _strip_data_uri(image_b64),
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
        return _parse_json(response.content[0].text.strip())


class FallbackLLMManager:
    def __init__(self, providers: list[LLMProvider], default: str | None = None):
        """
        :param providers: List of available LLM providers.
        :param default: Optional name of the provider to use exclusively (e.g., 'Ollama', 'OpenAI').
        """
        self._all_providers = providers
        self._working_provider_index: int | None = None

        if default:
            # Filter providers to matches (case-insensitive)
            target = default.lower()
            matching = [p for p in providers if target in p.__class__.__name__.lower()]
            if matching:
                logger.info(f"LLM Manager locked to default provider: {matching[0].__class__.__name__}")
                self.providers = matching
            else:
                logger.warning(f"Default provider '{default}' not found. Falling back to all available: {[p.__class__.__name__ for p in providers]}")
                self.providers = providers
        else:
            self.providers = providers

    def _ordered_providers(self) -> list[tuple[int, LLMProvider]]:
        """Return providers ordered so the last known-good one is tried first."""
        if self._working_provider_index is not None:
            idx = self._working_provider_index
            ordered = [(idx, self.providers[idx])] + [
                (i, p) for i, p in enumerate(self.providers) if i != idx
            ]
            return ordered
        return list(enumerate(self.providers))

    @staticmethod
    def _provider_label(provider: "LLMProvider") -> str:
        name = provider.__class__.__name__
        if hasattr(provider, "model_name"):
            return f"{name}({provider.model_name})"
        return name

    async def _run(self, method_name: str, *args) -> dict:
        last_error = None
        for original_idx, provider in self._ordered_providers():
            provider_label = self._provider_label(provider)
            method = getattr(provider, method_name)
            for attempt in range(1):
                try:
                    if attempt == 0:
                        logger.info(f"[{method_name}] Using {provider_label}")
                    else:
                        logger.warning(f"[{method_name}] Retrying {provider_label} (attempt 2)...")
                    result = await method(*args)
                    if self._working_provider_index != original_idx:
                        logger.info(f"Promoting {provider_label} as preferred provider.")
                        self._working_provider_index = original_idx
                    return result
                except Exception as e:
                    logger.warning(f"[{method_name}] Attempt {attempt + 1} failed for {provider_label}: {e}")
                    last_error = e
            logger.error(f"[{method_name}] Both attempts failed for {provider_label}. Trying next...")
        raise Exception(f"All LLM providers failed. Last error: {last_error}")

    async def execute_text(self, prompt: str) -> dict:
        """Text-only call through the fallback chain."""
        return await self._run("call_text", prompt)

    async def execute(self, prompt: str, image_b64: str) -> dict:
        """Vision call through the fallback chain (backwards compat entry point)."""
        return await self._run("call_vision", prompt, image_b64)
