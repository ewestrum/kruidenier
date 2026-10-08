from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from the environment (.env on the NAS)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    tz: str = "Europe/Amsterdam"
    database_url: str = "postgresql+psycopg://kruidenier:kruidenier@localhost:5432/kruidenier"
    secret_key: str = ""
    fernet_key: str = ""
    base_url: str = "http://localhost:8000"
    ha_webhook_url: str = ""
    ntfy_url: str = ""

    ah_client_id: str = "appie-ios"
    ah_client_version: str = "9.28"
    ah_min_request_interval: float = 1.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
