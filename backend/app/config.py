from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 生产默认指向 docker-compose 中的 PostgreSQL；本地单测可用 sqlite:///./test.db 覆盖
    database_url: str = "postgresql+psycopg://batch:batch@db:5432/batch"
    cors_origins: str = "*"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="BATCH_", extra="ignore")


settings = Settings()
