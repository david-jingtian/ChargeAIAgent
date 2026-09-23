from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHARGE_", extra="ignore")
    database_url: str = "postgresql://charge:charge@localhost:5432/charge"
    tool_url: str = "http://localhost:8001"
    poll_seconds: float = Field(default=0.2, gt=0)
    request_timeout_seconds: float = Field(default=5.0, gt=0)
    max_failures: int = Field(default=5, gt=0)
    backoff_base_seconds: float = Field(default=0.5, gt=0)
    backoff_cap_seconds: float = Field(default=8.0, gt=0)
    tool_failure_rate: float = Field(default=0.25, ge=0, le=1)
    tool_fail_first: int = Field(default=0, ge=0)
    tool_retry_after_seconds: int = Field(default=1, ge=0)
    tool_seed: int = 7
    tool_response_delay_seconds: float = Field(default=0, ge=0)
    pause_at: str = ""
    pause_step: str = "charge"


settings = Settings()
