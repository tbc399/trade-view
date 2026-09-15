from functools import lru_cache
from decimal import Decimal
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        extra="ignore",
        populate_by_name=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return init_settings, dotenv_settings, env_settings, file_secret_settings

    username: str = Field(default="", validation_alias="USERNAME")
    password_hash: SecretStr = Field(default=SecretStr(""), validation_alias="PASSWORD")
    account_id: str = Field(default="", validation_alias="TRADIER_ACCOUNT_ID")
    api_token: SecretStr = Field(default=SecretStr(""), validation_alias="TRADIER_API_TOKEN")
    base_url: str = Field(default="https://api.tradier.com/v1", validation_alias="TRADIER_BASE_URL")
    timeout_seconds: float = Field(default=10.0, validation_alias="TRADIER_TIMEOUT_SECONDS")
    benchmark_symbol: str = Field(default="SPY", validation_alias="BENCHMARK_SYMBOL")
    position_size_default_percent: Decimal = Field(
        default=Decimal("5"),
        validation_alias="POSITION_SIZE_DEFAULT_PERCENT",
    )

    @property
    def has_tradier_credentials(self) -> bool:
        return bool(self.account_id and self.api_token.get_secret_value())

    @property
    def has_tradier_token(self) -> bool:
        return bool(self.api_token.get_secret_value())

    @property
    def has_login_credentials(self) -> bool:
        return bool(self.username and self.password_hash.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    return Settings()
