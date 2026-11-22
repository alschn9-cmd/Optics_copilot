from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    wp_url: str = "http://opticscopilot.local"
    wp_user: str = "admin"
    wp_app_password: str = ""

    llm_api_key: str = ""
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_primary_model: str = "deepseek/deepseek-chat"
    llm_fallback_model: str = "openai/gpt-4o-mini"
    embedding_model: str = "openai/text-embedding-3-small"
    # USD per 1M tokens. Override with LLM_PRICE_PER_MILLION as a JSON object.
    llm_price_per_million: dict[str, dict[str, float]] = Field(
        default_factory=lambda: {
            "deepseek/deepseek-chat": {"input": 0.27, "output": 1.10},
            "openai/gpt-4o-mini": {"input": 0.15, "output": 0.60},
            "google/gemini-3.1-flash-lite": {"input": 0.10, "output": 0.40},
            "openai/text-embedding-3-small": {"input": 0.02, "output": 0.0},
        }
    )

    telegram_bot_token: str = ""
    telegram_manager_chat_id: str = ""

    database_url: str = "sqlite:///./data/app.db"
    chroma_persist_dir: str = "./data/chroma"
    pipeline_mode: str = "manual"
    pipeline_interval_hours: int = 6
    admin_password: str = ""

    news_source_url: str = ""
    products_source_url: str = ""
    pdf_urls: str = ""

    @property
    def news_source_list(self) -> list[str]:
        return split_csv(self.news_source_url)

    @property
    def product_source_list(self) -> list[str]:
        return split_csv(self.products_source_url)

    @property
    def pdf_url_list(self) -> list[str]:
        return split_csv(self.pdf_urls)


@lru_cache
def get_settings() -> Settings:
    return Settings()
