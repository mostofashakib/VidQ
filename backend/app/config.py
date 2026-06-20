import os
from pathlib import Path
from dotenv import load_dotenv

# Load env file once globally
env_path = Path(__file__).parent.parent / '.env'
load_dotenv(dotenv_path=env_path)

class Settings:
    def __init__(self):
        self.app_password: str = os.getenv("APP_PASSWORD", "")
        self.database_url: str = os.getenv("DATABASE_URL", "")
        self.cors_origins: str = os.getenv("CORS_ORIGINS", "")
        self.openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
        self.anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
        self.openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
        self.openrouter_model: str = os.getenv("OPENROUTER_MODEL", "google/gemma-4-31b-it:free")
        self.auth_enabled: bool = os.getenv("AUTH_ENABLED", "False").lower() in ("true", "1", "yes")
        # LLM provider selection: "ollama" | "openai" | "anthropic" | "openrouter" | "" (auto-fallback chain)
        self.llm_provider: str = os.getenv("LLM_PROVIDER", "ollama").lower().strip()
        self.ollama_host: str = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
        self.ollama_model: str = os.getenv("OLLAMA_MODEL", "gemma4:26b")

        # Transcription adapter for the Translate feature
        # Options: faster_whisper (default, local) | openai_whisper
        self.transcription_provider: str = os.getenv("TRANSCRIPTION_PROVIDER", "faster_whisper")
        self.transcription_model: str = os.getenv("TRANSCRIPTION_MODEL", "large-v3-turbo")
        # Local directory where faster-whisper stores downloaded model weights.
        # Model is downloaded once on first use; every subsequent restart loads from disk.
        default_model_dir = str(Path(__file__).parent.parent / "models" / "whisper")
        self.whisper_model_dir: str = os.getenv("WHISPER_MODEL_DIR", default_model_dir)

        # Determine the temp storage directory path
        default_storage = str(Path(__file__).parent.parent / "temp_storage")
        self.temp_storage_dir: str = os.getenv("TEMP_STORAGE_DIR", default_storage)

        # Base URL for generating self-referencing URLs (e.g. temp_storage links)
        self.base_url: str = os.getenv("BASE_URL", "http://localhost:8000")

        # Browser settings — BROWSER_HEADLESS=false to see the browser window (useful for debugging)
        self.browser_headless: bool = os.getenv("BROWSER_HEADLESS", "true").lower() in ("true", "1", "yes")
        # Persistent browser profile: cookies/localStorage saved here and reloaded each session
        # so the browser looks like a returning human visitor rather than a fresh bot.
        default_profile = str(Path(__file__).parent / "browser_profile")
        self.browser_profile_dir: str = os.getenv("BROWSER_PROFILE_DIR", default_profile)

        # Proxy pool for Cloudflare evasion. Comma-separated list of proxy URLs.
        # Format: http://user:pass@host:port  or  socks5://host:port
        # Leave empty to run without a proxy. On CF detection the pipeline rotates
        # to a fresh proxy + clears cookies so the site sees a new IP each attempt.
        raw_proxies = os.getenv("PROXY_URLS", "")
        self.proxy_urls: list[str] = [p.strip() for p in raw_proxies.split(",") if p.strip()]

        if not self.database_url:
            raise ValueError("Missing required environment variable: DATABASE_URL")
settings = Settings()

def get_settings() -> Settings:
    return settings
