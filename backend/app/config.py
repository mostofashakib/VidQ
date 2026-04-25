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
        
        # Determine the temp storage directory path
        default_storage = str(Path(__file__).parent.parent / "temp_storage")
        self.temp_storage_dir: str = os.getenv("TEMP_STORAGE_DIR", default_storage)

        # Base URL for generating self-referencing URLs (e.g. temp_storage links)
        self.base_url: str = os.getenv("BASE_URL", "http://localhost:8000")

        if not self.database_url:
            raise ValueError("Missing required environment variable: DATABASE_URL")
settings = Settings()

def get_settings() -> Settings:
    return settings
