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
        self.auth_enabled: bool = os.getenv("AUTH_ENABLED", "False").lower() in ("true", "1", "yes")
        
        # Determine the temp storage directory path
        default_storage = str(Path(__file__).parent.parent / "temp_storage")
        self.temp_storage_dir: str = os.getenv("TEMP_STORAGE_DIR", default_storage)

        if not self.database_url:
            raise ValueError("Missing required environment variable: DATABASE_URL")
settings = Settings()

def get_settings() -> Settings:
    return settings
