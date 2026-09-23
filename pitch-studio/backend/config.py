from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PITCH_STUDIO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PITCH_STUDIO_ROOT / "data"
FILES_DIR = DATA_DIR / "files"
REPO_ROOT = PITCH_STUDIO_ROOT.parent
DEFAULT_XLSX = REPO_ROOT / "One Company - One Story (2).xlsx"
if not DEFAULT_XLSX.exists():
    DEFAULT_XLSX = REPO_ROOT / "One Company - One Story (1).xlsx"
if not DEFAULT_XLSX.exists():
    DEFAULT_XLSX = REPO_ROOT / "One Company - One Story.xlsx"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(REPO_ROOT / ".env"), str(PITCH_STUDIO_ROOT / ".env")),
        extra="ignore",
    )

    database_url: str = f"sqlite:///{DATA_DIR / 'app.db'}"
    secret_key: str = "dev-secret-change-me"
    openrouter_api_key: str = ""
    openrouter_model: str = "anthropic/claude-opus-4.6"
    openrouter_interpret_model: str = "google/gemini-2.5-flash"
    openrouter_slide_model: str = "anthropic/claude-haiku-4.5"
    openrouter_stt_model: str = "openai/whisper-large-v3"
    openrouter_embedding_model: str = "openai/text-embedding-3-small"
    openrouter_verbosity: str = "medium"
    admin_email: str = "admin@example.com"
    admin_password: str = "changeme"
    spaces_endpoint: str = ""
    spaces_region: str = ""
    spaces_key: str = ""
    spaces_secret: str = ""
    spaces_bucket: str = ""
    runpod_api_key: str = ""
    runpod_endpoint_id: str = ""
    apify_token: str = ""
    apify_youtube_actor: str = "dami_studio/youtube-video-downloader"
    apify_youtube_quality: str = "4320"
    apify_youtube_max_mb: int = 5000
    cookie_name: str = "pitch_session"
    cookie_max_age: int = 60 * 60 * 24 * 7
    cookie_secure: bool = False
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def uses_spaces(self) -> bool:
        return bool(
            self.spaces_endpoint
            and self.spaces_region
            and self.spaces_key
            and self.spaces_secret
            and self.spaces_bucket
        )


settings = Settings()
DATA_DIR.mkdir(parents=True, exist_ok=True)
FILES_DIR.mkdir(parents=True, exist_ok=True)
