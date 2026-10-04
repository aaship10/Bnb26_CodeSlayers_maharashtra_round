"""Process configuration, read from environment variables (and .env if present).

Nothing in here is correctness-critical shared state: every replica reads the
same environment, and all mutable state lives in Postgres.
"""
from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://fairdrop:fairdrop@localhost:5432/fairdrop"
    test_database_url: str = "postgresql+psycopg://fairdrop:fairdrop@localhost:5432/fairdrop_test"
    db_pool_size: int = 20
    db_max_overflow: int = 20

    app_env: Literal["dev", "prod"] = "dev"
    auth_mode: Literal["dev", "jwt"] = "dev"
    admin_token: str = "dev-admin-token"
    # Signs dev-login tokens (AUTH_MODE=dev only).
    dev_auth_secret: str = "dev-auth-secret-change-me"
    # Comma-separated dotted paths of modules exposing `setup(app)`, loaded at
    # startup. This is how Member B plugs in middleware, the real auth
    # dependency and the entry gate without editing core code.
    plugins: str = ""

    beacon_provider: Literal["mock", "drand"] = "mock"

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = ""


    @field_validator("database_url", "test_database_url")
    @classmethod
    def _use_psycopg3(cls, v: str) -> str:
        # Accept plain libpq URLs (as Neon/Heroku hand them out) and pin the
        # SQLAlchemy dialect to psycopg 3 instead of the default psycopg2.
        for prefix in ("postgresql://", "postgres://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix):]
        return v

    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def sync_url(url: str) -> str:
    """Plain libpq URL for psycopg.connect() (scripts, COPY, tests)."""
    return url.replace("postgresql+psycopg://", "postgresql://", 1)
