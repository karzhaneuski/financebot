from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    BOT_TOKEN: str
    # LLM backend for receipt/screenshot vision and /search parsing:
    # "gemini" (default) or "anthropic". Only the selected provider's key is
    # required.
    LLM_PROVIDER: str = "gemini"
    GEMINI_API_KEY: str | None = None
    GEMINI_MODEL: str = "gemini-3.8-flash"
    ANTHROPIC_API_KEY: str | None = None
    DATABASE_URL: str
    REDIS_URL: str
    EXCHANGE_API_KEY: str
    # Local development only: with DEV_MODE=true the API also accepts
    # "Bearer <DEV_TOKEN>" as DEV_USER_ID. Never set these in production.
    DEV_MODE: bool = False
    DEV_TOKEN: str | None = None
    DEV_USER_ID: int = 0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()
