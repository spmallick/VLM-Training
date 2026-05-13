from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    app_name: str = "Receipt-to-Expense Agent"
    app_host: str = "127.0.0.1"
    app_port: int = 8000

    data_dir: Path = ROOT_DIR / "app" / "data"
    uploads_dir: Path = ROOT_DIR / "app" / "data" / "uploads"
    database_path: Path = ROOT_DIR / "app" / "data" / "expense_agent.db"
    currency_rates_path: Path = ROOT_DIR / "app" / "data" / "currency_rates.json"

    hf_api_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "HUGGINGFACEHUB_API_TOKEN",
            "HF_API_TOKEN",
            "HF_TOKEN",
            "HUGGING_FACE_HUB_TOKEN",
        ),
    )
    hf_model: str = Field(
        default="Qwen/Qwen3-VL-8B-Instruct:novita",
        validation_alias=AliasChoices("HF_MODEL", "HUGGINGFACE_MODEL"),
    )
    hf_receipt_model: str = Field(
        default="",
        validation_alias=AliasChoices("HF_RECEIPT_MODEL", "HUGGINGFACE_RECEIPT_MODEL"),
    )
    hf_policy_model: str = Field(
        default="",
        validation_alias=AliasChoices("HF_POLICY_MODEL", "HUGGINGFACE_POLICY_MODEL"),
    )
    hf_navigation_model: str = Field(
        default="",
        validation_alias=AliasChoices("HF_NAVIGATION_MODEL", "HF_PORTAL_MODEL", "HUGGINGFACE_NAVIGATION_MODEL"),
    )
    hf_router_url: str = "https://router.huggingface.co/v1/chat/completions"
    hf_timeout_seconds: int = 90
    require_qwen3_vl: bool = Field(
        default=False,
        validation_alias=AliasChoices("REQUIRE_QWEN3_VL", "QWEN3_VL_ONLY"),
    )
    browser_headless: bool = Field(
        default=False,
        validation_alias=AliasChoices("BROWSER_HEADLESS", "PLAYWRIGHT_HEADLESS", "HEADLESS_BROWSER"),
    )

    demo_fill_delay_ms: int = 850
    demo_currency: str = "USD"

    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def receipt_model(self) -> str:
        return self.hf_receipt_model or self.hf_model

    @property
    def policy_model(self) -> str:
        return self.hf_policy_model or self.receipt_model

    @property
    def navigation_model(self) -> str:
        return self.hf_navigation_model or self.hf_model


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    return settings
