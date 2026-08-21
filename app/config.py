from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://ipo_app:ipo_dev_password@localhost:5432/ipo_research"
    sec_user_agent: str = "IPO Research Prototype your-email@example.com"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
