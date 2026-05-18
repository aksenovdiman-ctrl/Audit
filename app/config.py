from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


class SettingsError(RuntimeError):
    """Raised when a required setting is missing."""


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SettingsError(f"Missing required environment variable: {name}")
    return value


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    public_base_url: str
    database_path: Path
    style_reference_image_url: str
    kie_api_key: str
    kie_api_base_url: str
    kie_file_upload_base_url: str
    kie_callback_token: str
    kie_reasoning_effort: str
    use_direct_attachment_urls: bool
    salesbot_api_key: str
    salesbot_api_base_url: str
    salesbot_webhook_token: str
    salesbot_ready_message: str
    salesbot_fail_message: str
    salesbot_need_more_message: str
    telegram_bot_token: str
    telegram_webhook_token: str
    brand_name: str
    session_min_images: int
    session_max_images: int
    http_timeout_seconds: float
    kie_poll_interval_seconds: float
    kie_poll_max_attempts: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            public_base_url=_require_env("PUBLIC_BASE_URL").rstrip("/"),
            database_path=Path(os.getenv("DATABASE_PATH", "./data/audit_bot.sqlite3")),
            style_reference_image_url=os.getenv("STYLE_REFERENCE_IMAGE_URL", "").strip(),
            kie_api_key=_require_env("KIE_API_KEY"),
            kie_api_base_url=os.getenv("KIE_API_BASE_URL", "https://api.kie.ai").rstrip("/"),
            kie_file_upload_base_url=os.getenv(
                "KIE_FILE_UPLOAD_BASE_URL", "https://kieai.redpandaai.co"
            ).rstrip("/"),
            kie_callback_token=_require_env("KIE_CALLBACK_TOKEN"),
            kie_reasoning_effort=os.getenv("KIE_REASONING_EFFORT", "high"),
            use_direct_attachment_urls=_get_bool("USE_DIRECT_ATTACHMENT_URLS", True),
            salesbot_api_key=_require_env("SALESBOT_API_KEY"),
            salesbot_api_base_url=os.getenv(
                "SALESBOT_API_BASE_URL", "https://chatter.salebot.ai/api"
            ).rstrip("/"),
            salesbot_webhook_token=_require_env("SALESBOT_WEBHOOK_TOKEN"),
            salesbot_ready_message=os.getenv("SALESBOT_READY_MESSAGE", "audit_ready"),
            salesbot_fail_message=os.getenv("SALESBOT_FAIL_MESSAGE", "audit_failed"),
            salesbot_need_more_message=os.getenv(
                "SALESBOT_NEED_MORE_MESSAGE", "audit_need_more_screens"
            ),
            telegram_bot_token=_require_env("TELEGRAM_BOT_TOKEN"),
            telegram_webhook_token=_require_env("TELEGRAM_WEBHOOK_TOKEN"),
            brand_name=os.getenv("BRAND_NAME", "audit_inst_bot"),
            session_min_images=int(os.getenv("SESSION_MIN_IMAGES", "2")),
            session_max_images=int(os.getenv("SESSION_MAX_IMAGES", "2")),
            http_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "30")),
            kie_poll_interval_seconds=float(os.getenv("KIE_POLL_INTERVAL_SECONDS", "8")),
            kie_poll_max_attempts=int(os.getenv("KIE_POLL_MAX_ATTEMPTS", "30")),
        )

    def kie_callback_url(self, job_id: int) -> str:
        return (
            f"{self.public_base_url}/kie/callback"
            f"?token={self.kie_callback_token}&job_id={job_id}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
