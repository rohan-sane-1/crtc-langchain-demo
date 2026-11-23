from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
