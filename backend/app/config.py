from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LangSmith (read by the langsmith SDK directly from the environment in later phases)
    langsmith_api_key: str = ""
    langsmith_tracing: bool = False
    langsmith_project: str = "crtc-copilot-dev"

    # LLM / embeddings (used from Phase 3-4)
    google_api_key: str = ""
    llm_model: str = ""
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384

    # Database
    database_url: str = "postgresql+asyncpg://crtc:crtc@localhost:5433/crtc"

    # CRTC HTTP client / case list (Phase 2)
    crtc_list_url: str = "https://applications.crtc.gc.ca/portail-portal/eng/listes-lists/18/data"
    crtc_list_ttl_minutes: int = Field(default=60, ge=1)
    # The full list is ~18 MB today (it grows every year). Default leaves ~2x headroom.
    crtc_max_response_bytes: int = Field(default=40 * 1024 * 1024, ge=1024)
    crtc_connect_timeout_s: float = Field(default=10.0, gt=0)
    crtc_read_timeout_s: float = Field(default=60.0, gt=0)
    crtc_max_redirects: int = Field(default=3, ge=0, le=10)
    crtc_max_retries: int = Field(default=3, ge=0, le=6)
    crtc_min_request_interval_s: float = Field(default=1.0, ge=0)
    crtc_user_agent: str = "crtc-copilot/0.1 (personal research tool; polite client; 1 req/s)"

    # Knowledge-base sync (Phase 3)
    data_dir: Path = BACKEND_DIR / "data"  # git-ignored; originals live in data_dir/originals
    crtc_max_document_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    kb_max_zip_entries: int = Field(default=500, ge=1)
    kb_max_zip_uncompressed_bytes: int = Field(default=300 * 1024 * 1024, ge=1024)
    kb_max_zip_ratio: float = Field(default=100.0, gt=1)
    kb_parse_timeout_s: float = Field(default=90.0, gt=0)
    kb_parse_memory_mb: int = Field(default=1536, ge=64)
    # Skip even the listing request if this case was synced this recently (seconds).
    kb_recheck_min_interval_s: float = Field(default=300.0, ge=0)
    # Don't re-ask a host that blocked a document sooner than this (seconds).
    kb_blocked_retry_s: float = Field(default=24 * 3600.0, ge=0)
    kb_embed_batch_size: int = Field(default=32, ge=1)
    kb_sync_rate_limit: int = Field(default=6, ge=1)  # POST /cases/{id}/sync per user per window
    kb_sync_rate_window_s: float = Field(default=600.0, gt=0)
    kb_max_concurrent_syncs: int = Field(default=2, ge=1)

    # Auth
    google_client_id: str
    session_secret: str = Field(min_length=32)
    allowed_emails: str = ""
    frontend_origin: str = "http://localhost:3000"

    @property
    def allowed_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.allowed_emails.split(",") if e.strip()}

    @property
    def cookie_secure(self) -> bool:
        return self.frontend_origin.startswith("https://")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
