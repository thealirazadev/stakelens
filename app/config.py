from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # .env.deployment is written by scripts/deploy.py and holds the vault address and start block.
    model_config = SettingsConfigDict(env_file=(".env", ".env.deployment"), extra="ignore")

    database_url: str = "postgresql+psycopg://stakelens:stakelens@localhost:55432/stakelens"

    rpc_url: str = "http://127.0.0.1:8545"
    chain_id: int = 31337
    vault_address: str = ""
    start_block: int = 0

    confirmations: int = 3
    poll_seconds: float = 1.0
    batch_size: int = 100
    max_reorg_depth: int = 64

    siwe_domain: str = "localhost:8010"
    siwe_uri: str = "http://localhost:8010"
    challenge_ttl_seconds: int = 300
    session_ttl_seconds: int = 3600

    anthropic_api_key: str = ""
    assistant_model: str = "claude-opus-5-5"
    assistant_hourly_limit: int = 20


@lru_cache
def get_settings() -> Settings:
    return Settings()
