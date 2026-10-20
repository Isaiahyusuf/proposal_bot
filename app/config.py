from functools import lru_cache

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
