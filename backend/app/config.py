from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gemini_api_key: str = ""
    gemini_model: str = "gemini-1.5-flash"
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    demo_user_id: str = "u_an"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    banking_data_dir: str = ""

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def data_dir(self) -> Path:
        if not self.banking_data_dir:
            return Path(__file__).parent / "data"
        path = Path(self.banking_data_dir).expanduser()
        if path.is_absolute():
            return path
        return (Path(__file__).parent.parent / path).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
