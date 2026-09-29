from functools import lru_cache
from typing import Literal
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str

    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30
    refresh_token_session_expire_hours: int = Field(default=12, ge=1)
    refresh_reuse_grace_seconds: float = Field(default=30, gt=0)
    auth_max_sessions_per_user: int = Field(default=5, ge=1)

    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    cookie_secure: bool = False
    cookie_domain: str | None = None

    trust_proxy_headers: bool = False
    trusted_proxy_hops: int = Field(default=1, ge=1)

    sse_ticket_expire_seconds: int = 30
    sse_revalidation_interval_seconds: int = 30

    api_key_header_name: str = "X-API-Key"
    api_key: str

    storage_path: str = "./storage"

    frontend_url: str
    backend_public_url: str

    cors_allowed_origins: str

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]

    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from_email: str

    mediamtx_api_url: str
    mediamtx_public_url: str
    mediamtx_api_user: str
    mediamtx_api_password: str

    ai_vision_api_url: str
    ai_vision_api_key: str

    mediamtx_jwt_private_key_b64: str
    mediamtx_stream_token_expire_seconds: int 

    camera_resync_concurrency_limit: int = 10
    camera_manual_verify_rate_limit: int = 1
    camera_manual_verify_rate_window_seconds: float = 30.0
    camera_verify_poll_interval_seconds: float = 2.0
    camera_verify_max_duration_seconds: float = 60.0
    blacklist_email_alert_cooldown_seconds: int = 900
    contact_max_per_user: int = 50
    presence_max_connections_per_user: int = 5
    presence_sweep_interval_seconds: int = 60
    presence_broadcast_debounce_seconds: float = Field(default=0.3, ge=0)
    channel_max_connections_per_user: int = 5
    channel_queue_size: int = 100
    user_avatar_max_size_bytes: int = 2_097_152  # 2MB
    auth_refresh_cookie_path: str = "/api/auth"

    @model_validator(mode="after")
    def check_cookie_security(self) -> "Settings":
        if self.cookie_samesite == "none" and not self.cookie_secure:
            raise ValueError(
                "cookie_samesite='none' requires cookie_secure=True: "
                "browsers reject SameSite=None cookies that are not Secure"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()