from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://geo_api:geo_api@127.0.0.1:5432/geo_api"
    temp_root: Path = Path("/tmp/geo_api")
    upload_limit_bytes: int = 10 * 1024 * 1024
    body_limit_bytes: int = 11 * 1024 * 1024
    expanded_limit_bytes: int = 50 * 1024 * 1024
    max_archive_entries: int = 64
    max_features: int = 5_000
    max_coordinates: int = 100_000
    max_coordinates_per_feature: int = 20_000
    max_generated_coordinates_per_feature: int = 100_000
    max_generated_coordinates_file: int = 500_000
    max_generated_work: int = 2_000_000
    max_feature_output_bytes: int = 2 * 1024 * 1024
    max_filename_bytes: int = 255
    max_child_output_bytes: int = 64 * 1024 * 1024
    upload_timeout_seconds: int = 30
    child_timeout_seconds: int = 60
    request_timeout_seconds: int = 120
    publication_timeout_seconds: int = 20
    transaction_timeout_seconds: int = 15
    commit_reconciliation_seconds: int = 5


@lru_cache
def get_settings() -> Settings:
    return Settings()
