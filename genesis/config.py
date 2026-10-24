from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration.

    Every value is operator-controlled; nothing here is derived from user input.
    """

    model_config = SettingsConfigDict(
        env_prefix="GENESIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Identity and location
    env: str = "development"
    agent_name: str = "genesis"
    agent_seed: str = ""
    agent_port: int = 8000
    agent_endpoint: str = ""
    agent_mailbox: bool = True
    agent_handle: str = "genesis"
    database_path: str = "./genesis.db"
    log_level: str = "INFO"

    # Kill switches
    builds_enabled: bool = True
    payments_enabled: bool = False
    payment_rail: str = "stripe"
    stripe_secret_key: str = ""
    payment_recipient: str = ""
    payment_amount: str = "5.00"
    payment_currency: str = "USD"

    # Gateway
    gateway_url: str = "http://localhost:8080"
    gateway_secret: str = "replace-me"
    gateway_timeout_s: float = 30.0

    # Platform credentials
    agentverse_api_key: str = ""
    agentverse_base_url: str = "https://agentverse.ai"
    asi1_api_key: str = ""
    asi1_base_url: str = "https://api.asi1.ai/v1"
    asi1_model: str = "asi1"
    asi1_fast_model: str = "asi1-mini"
    tavily_api_key: str = ""
    tavily_base_url: str = "https://api.tavily.com"

    # Detection and planning
    discovery_timeout_s: int = Field(default=120, ge=0, le=600)
    match_threshold: float = Field(default=0.72, ge=0, le=1)
    borderline_threshold: float = Field(default=0.40, ge=0, le=1)
    planner_repairs: int = Field(default=3, ge=0, le=5)

    # Limits and quotas
    max_concurrent_builds: int = Field(default=2, ge=1, le=20)
    max_live_agents: int = Field(default=50, ge=1, le=500)
    sender_builds_per_day: int = Field(default=3, ge=0)
    sender_runs_per_hour: int = Field(default=20, ge=0)
    per_agent_daily_requests: int = Field(default=1000, ge=1)
    per_run_llm_tokens: int = Field(default=4000, ge=256, le=32000)
    llm_max_steps: int = Field(default=3, ge=1, le=3)

    # Session and delegation timing
    session_input_ttl_s: int = Field(default=900, ge=60)
    session_total_ttl_s: int = Field(default=3600, ge=300)
    delegate_timeout_s: float = Field(default=45.0, ge=5, le=120)
    probe_timeout_s: float = Field(default=60.0, ge=10, le=180)
    progress_interval_s: float = Field(default=4.0, ge=1, le=30)
    agent_ttl_days: int = Field(default=30, ge=1)

    # Interval agents
    interval_chat_delivery_confirmed: bool = False
    interval_max_failures: int = Field(default=3, ge=1, le=10)

    # Discovery / retention
    dedupe_window: int = Field(default=500, ge=50)
    build_resume_after_s: int = Field(default=300, ge=60)

    @property
    def is_production(self) -> bool:
        return self.env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
