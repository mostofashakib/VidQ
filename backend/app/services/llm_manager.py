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


def _get_media_type(image_b64: str) -> str:
    """Detect media type from base64 string prefix."""
    # Clean up input
    img_data = image_b64.strip()
    if "," in img_data:
        img_data = img_data.split(",")[1]

    # /9j/ -> image/jpeg, iVBOR -> image/png, UklGR -> image/webp, R0lGO -> image/gif
    mtype = "image/png"  # Default
    if img_data.startswith("/9j/"):
        mtype = "image/jpeg"
    elif img_data.startswith("iVBOR"):
        mtype = "image/png"
    elif img_data.startswith("UklGR"):
        mtype = "image/webp"
    
    logger.debug(f"Detected image media type: {mtype} (prefix: {img_data[:10]}...)")
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

    async def call_text(self, prompt: str) -> dict:
        payload = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",              # important
            "options": {"temperature": 0}, # more reliable for JSON
        }
        logger.debug(f"Submitting payload to Ollama: {self.model_name}")
        data = await self._post(payload)
        content = data["message"]["content"].strip()
        logger.debug(f"Ollama raw response (first 200 chars): {content[:200]}")
        try:
            return json.loads(content)
        except Exception as e:
            logger.error(f"Failed to parse JSON from Ollama response: {content}")
            raise e

    async def call_vision(self, prompt: str, image_b64: str) -> dict:
        # image_b64 must be raw base64 only, not data:image/...;base64,...
        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [image_b64],
                }
            ],
            "stream": False,
            "format": "json",              # important
            "options": {"temperature": 0}, # more reliable for JSON
        }
        logger.debug(f"Submitting vision payload to Ollama: {self.model_name}")
        try:
            data = await self._post(payload)
            content = data["message"]["content"].strip()
            logger.debug(f"Ollama vision raw response (first 200 chars): {content[:200]}")
            return json.loads(content)
        except Exception as e:
            # Fallback: Many local models (like qwen3.5 text version) don't support vision
            # and will error out if 'images' is present. We retry without the image.
            logger.warning(f"Ollama vision call failed ({e}), falling back to text-only mode...")
            payload["messages"][0].pop("images")
            try:
                data = await self._post(payload)
                content = data["message"]["content"].strip()
                logger.info("Ollama fallback text-only successful.")
                return json.loads(content)
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
                        {"type": "image_url", "image_url": {"url": f"data:{_get_media_type(image_b64)};base64,{image_b64}"}},
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
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
        return _parse_json(response.content[0].text.strip())


class FallbackLLMManager:
    def __init__(self, providers: list[LLMProvider], default: str | None = "Anthropic"):
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

    async def _run(self, method_name: str, *args) -> dict:
        last_error = None
        for original_idx, provider in self._ordered_providers():
            provider_name = provider.__class__.__name__
            method = getattr(provider, method_name)
            for attempt in range(1):
                try:
                    if attempt == 0:
                        logger.info(f"[{method_name}] Attempting with {provider_name}")
                    else:
                        logger.warning(f"[{method_name}] Retrying {provider_name} (attempt 2)...")
                    result = await method(*args)
                    if self._working_provider_index != original_idx:
                        logger.info(f"Promoting {provider_name} as preferred provider.")
                        self._working_provider_index = original_idx
                    return result
                except Exception as e:
                    logger.warning(f"[{method_name}] Attempt {attempt + 1} failed for {provider_name}: {e}")
                    last_error = e
            logger.error(f"[{method_name}] Both attempts failed for {provider_name}. Trying next...")
        raise Exception(f"All LLM providers failed. Last error: {last_error}")

    async def execute_text(self, prompt: str) -> dict:
        """Text-only call through the fallback chain."""
        return await self._run("call_text", prompt)

    async def execute(self, prompt: str, image_b64: str) -> dict:
        """Vision call through the fallback chain (backwards compat entry point)."""
        return await self._run("call_vision", prompt, image_b64)
