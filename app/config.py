from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # .env.deployment is written by scripts/deploy.py and holds the vault address and start block.
    model_config = SettingsConfigDict(env_file=(".env", ".env.deployment"), extra="ignore")

    database_url: str = "postgresql+psycopg://stakelens:stakelens@localhost:55432/stakelens"

    rpc_url: str = "http://127.0.0.1:8545"
    # Comma-separated backup nodes, tried in order when the primary fails.
    rpc_fallback_urls: str = ""
    chain_id: int = 31337
    token_address: str = ""
    vault_address: str = ""
    start_block: int = 0

    confirmations: int = 3
    poll_seconds: float = 1.0
    batch_size: int = 100
    max_reorg_depth: int = 64
    log_range_size: int = 2000
    reconcile_seconds: float = 60.0
    indexer_metrics_port: int = 9101

    tx_poll_seconds: float = 2.0
    tx_stuck_seconds: float = 60.0
    tx_max_fee_gwei: int = 100
    tx_metrics_port: int = 9102

    aggregate_cache_seconds: float = 1.0

    siwe_domain: str = "localhost:8010"
    siwe_uri: str = "http://localhost:8010"
    challenge_ttl_seconds: int = 300
    session_ttl_seconds: int = 3600

    anthropic_api_key: str = ""
    assistant_model: str = "claude-opus-5-5"
    assistant_hourly_limit: int = 20

    @property
    def rpc_urls(self) -> list[str]:
        fallbacks = [url.strip() for url in self.rpc_fallback_urls.split(",") if url.strip()]
        return [self.rpc_url, *fallbacks]


@lru_cache
def get_settings() -> Settings:
    return Settings()
