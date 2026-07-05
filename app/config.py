from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    supabase_url: str
    supabase_anon_key: str
    supabase_service_key: str
    anthropic_api_key: str
    environment: str = "development"

    class Config:
        env_file = ".env"


settings = Settings()