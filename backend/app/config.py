import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env into os.environ so untyped keys (the GROQ_API_KEY_N pool,
# OMNI_DEBUG flags, etc.) are visible to modules that read environ
# directly. pydantic-settings ONLY populates the typed fields below;
# anything else stays in the .env file and never reaches os.environ
# without this explicit load_dotenv call.
try:
    from dotenv import load_dotenv

    load_dotenv(
        Path(__file__).resolve().parent.parent / ".env", override=False,
    )
except ImportError:  # python-dotenv is optional; settings still load .env
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gemini_api_key: str = ""
    gemini_model: str = "gemini-1.5-flash"
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    # OpenRouter — third-tier fallback when Groq + Gemini exhaust their
    # daily quotas. The default model is a known-free multilingual one;
    # override via OPENROUTER_MODEL if it disappears (the free roster
    # rotates monthly). Pool keys live in OPENROUTER_API_KEY_1..N.
    openrouter_api_key: str = ""
    # Live probe (Jun 7): the 26b sibling was upstream-throttled by
    # Google AI Studio while the 31b returned VN replies cleanly. Pin
    # to 31b so the demo's first request doesn't bounce on a 429.
    openrouter_model: str = "google/gemma-4-31b-it:free"
    # OpenRouter convention: send HTTP-Referer + X-Title so the call
    # is attributed to our app in their leaderboard. Cosmetic, not
    # enforced — but recommended by their docs.
    openrouter_referer: str = "https://github.com/duongtrongnguyen123/omni-banking-assistant"
    openrouter_title: str = "Omni Banking Assistant"
    # Speech-to-text via Groq whisper-large-v3 (OpenAI-compatible audio API).
    groq_stt_model: str = "whisper-large-v3"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    openai_api_key: str = ""
    openai_stt_model: str = "gpt-4o-mini-transcribe"
    # auto: Groq if GROQ_API_KEY set, else OpenAI, else local faster-whisper.
    speech_stt_provider: str = "auto"
    demo_user_id: str = "u_an"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    banking_data_dir: str = ""
    # Offline-demo survival switch — see docs/offline-demo.md. When 1, the
    # backend silently disables every outbound network dependency (LLM
    # providers, embedding model downloads, schedule ticker) so the pitch
    # laptop can demo without wifi. The frontend still talks to localhost
    # unchanged.
    offline_demo: bool = False

    # --- Nguồn dữ liệu & cache (tùy chọn) ---
    # data_backend: "memory" (đọc JSON in-memory, mặc định) hoặc "postgres"
    # (đọc lịch sử giao dịch thật từ RDS qua db/postgres.py).
    data_backend: str = "memory"
    # database_url: chuỗi kết nối Postgres omni (RDS) — dùng khi data_backend=postgres,
    # và cho script seed/benchmark.
    database_url: str = ""
    # redis_url rỗng => cache tự tắt, app đọc thẳng nguồn chính.
    redis_url: str = ""
    # Bật cache cho đường đọc của app. Mặc định TẮT vì store demo là in-memory
    # (đã nhanh hơn round-trip Redis). Bật khi store thật sự là Postgres.
    cache_enabled: bool = False
    # TTL mặc định cho các key cache (giây). 300s = 5 phút như kế hoạch.
    cache_ttl_seconds: int = 300
    # Compliance retention: completed transaction records are retained for
    # 10 years for reporting, audit, dispute lookup, and competent-authority
    # requests. Demo code does not purge rows before this horizon.
    transaction_retention_years: int = 10

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
