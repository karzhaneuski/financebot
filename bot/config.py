from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    BOT_TOKEN: str
    ANTHROPIC_API_KEY: str
    DATABASE_URL: str
    REDIS_URL: str
    EXCHANGE_API_KEY: str
    DEV_TOKEN: str | None = None
    DEV_USER_ID: int = 0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()
