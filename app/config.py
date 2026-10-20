from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./proposal_bot.db"  # dev only; use PostgreSQL in production
    secret_key: str
    encryption_key: str
    cookie_secure: bool = False
    dexscreener_base_url: str = "https://api.dexscreener.com"
    max_failed_logins: int = 5
    lockout_minutes: int = 15

    @field_validator("database_url")
    @classmethod
    def _use_psycopg3(cls, v: str) -> str:
        # Hosts such as Railway hand out postgres:// or postgresql:// URLs; we ship psycopg 3.
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix):]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
