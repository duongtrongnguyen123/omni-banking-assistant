import os
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
    # Offline-demo survival switch — see docs/offline-demo.md. When 1, the
    # backend silently disables every outbound network dependency (LLM
    # providers, embedding model downloads, schedule ticker) so the pitch
    # laptop can demo without wifi. The frontend still talks to localhost
    # unchanged.
    offline_demo: bool = False

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
    s = Settings()
    # OMNI_OFFLINE_DEMO=1 is the canonical env switch — promote it onto
    # the settings object even if the user didn't set the typed field.
    # Also fan out to the OMNI_SKIP_EMBED_BACKFILL / schedule-tick env
    # vars the startup code already honours, so a single switch is
    # enough.
    if os.environ.get("OMNI_OFFLINE_DEMO") in ("1", "true", "True"):
        s.offline_demo = True
        os.environ.setdefault("OMNI_SKIP_EMBED_BACKFILL", "1")
        os.environ.setdefault("OMNI_DISABLE_SCHEDULE_TICK", "1")
    return s
