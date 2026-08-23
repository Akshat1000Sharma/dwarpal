"""Application configuration, validated at import time.

A missing or malformed required setting must stop the process here rather than surface as a
request-time failure, so every field is validated the moment this module is first imported.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    APP_ENV: Literal["development", "testing", "production"] = "development"
    LOG_LEVEL: str = "INFO"
    SECRET_KEY: str = Field(min_length=16)
    PUBLIC_BASE_URL: str = "http://localhost:8000"

    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_NAME: str = "dwarpal"
    DB_USER: str = "dwarpal"
    DB_PASSWORD: str = Field(min_length=1)

    RAZORPAY_KEY_ID: str = Field(min_length=1)
    RAZORPAY_KEY_SECRET: str = Field(min_length=1)
    RAZORPAY_WEBHOOK_SECRET: str = Field(min_length=1)
    RAZORPAY_S2S_ENABLED: bool = False

    GEMINI_API_KEY: str = Field(min_length=1)
    GEMINI_MODEL: str = "gemini-2.5-flash"
    SEMANTIC_TIMEOUT_SECONDS: float = 12.0

    META_ACCESS_TOKEN: str = ""
    META_PHONE_NUMBER_ID: str = ""
    META_VERIFY_TOKEN: str = Field(min_length=1)
    META_APP_SECRET: str = Field(min_length=1)
    META_APP_ID: str = ""
    META_GRAPH_VERSION: str = "v23.0"
    ESCALATION_HUMAN_WHATSAPP: str = ""
    ESCALATION_DEADLINE_SECONDS: int = 900

    MERCHANT_KEY_ID: str = "dwarpal-merchant-01"
    MERCHANT_SIGNING_KEY_DIR: str = "./secrets/merchant_keys"
    MERCHANT_ID: str = "dwarpal-demo-merchant"
    MERCHANT_NAME: str = "Dwarpal Demo Store"
    MERCHANT_WEBSITE: str = "https://dwarpal.example"

    TRUST_REGISTRY_PATH: str = "./config/trust_registry.json"
    CATALOG_SEED_PATH: str = "./config/catalog_seed.json"
    POLICY_TERMS_PATH: str = "./config/policy_terms.md"

    CREDENTIAL_CLOCK_SKEW_SECONDS: int = 60
    UNVERIFIED_CEILING_MINOR: int = 50000
    BUDGET_RESERVATION_TTL_SECONDS: int = 300
    INVENTORY_HOLD_TTL_SECONDS: int = 600
    INVENTORY_HOLD_QUOTA_PER_AGENT: int = 5
    VELOCITY_WINDOW_SECONDS: int = 3600
    STRUCTURING_WINDOW_SECONDS: int = 900

    @field_validator("RAZORPAY_KEY_ID")
    @classmethod
    def _must_be_test_mode(cls, value: str) -> str:
        # Spec section 9 requires test mode throughout. Refusing a live key at startup makes that
        # structural rather than a convention someone can forget.
        if not value.startswith("rzp_test_"):
            raise ValueError(
                "RAZORPAY_KEY_ID must be a test-mode key beginning 'rzp_test_'. "
                "Dwarpal refuses to run against live Razorpay credentials."
            )
        return value

    @field_validator("LOG_LEVEL")
    @classmethod
    def _known_level(cls, value: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}")
        return upper

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    @property
    def maintenance_database_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/postgres"
        )

    def resolve(self, relative: str) -> Path:
        path = Path(relative)
        return path if path.is_absolute() else (BACKEND_ROOT / path).resolve()


class ConfigurationError(RuntimeError):
    """Raised when configuration is missing or malformed."""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        lines = [
            f"  {'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        ]
        raise ConfigurationError(
            "Dwarpal configuration is invalid. Fix backend/.env and start again:\n"
            + "\n".join(lines)
        ) from exc


try:
    settings = get_settings()
except ConfigurationError as exc:  # pragma: no cover - exercised by running the process
    print(str(exc), file=sys.stderr)
    raise
