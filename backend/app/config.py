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
