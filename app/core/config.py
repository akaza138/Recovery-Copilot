from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "development"

    database_url: str = "postgresql+psycopg2://recovery:change-me@localhost:5432/recovery_copilot"

    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""

    anthropic_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
