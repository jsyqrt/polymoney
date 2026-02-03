"""
Configuration management for Polymoney.

Supports loading from environment variables, .env files, and YAML config files.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PolymarketConfig(BaseSettings):
    """Polymarket API configuration."""

    host: str = Field(default="https://clob.polymarket.com", description="CLOB API host")
    chain_id: int = Field(default=137, description="Polygon chain ID")
    private_key: Optional[str] = Field(default=None, description="Wallet private key")
    funder: Optional[str] = Field(default=None, description="Proxy/funder address")
    signature_type: int = Field(default=0, description="Signature type (0=EOA, 1=Magic, 2=Browser)")

    model_config = SettingsConfigDict(env_prefix="POLYMARKET_")


class DatabaseConfig(BaseSettings):
    """Database configuration."""

    url: str = Field(default="sqlite+aiosqlite:///polymoney.db", description="Database URL")
    echo: bool = Field(default=False, description="Echo SQL queries")

    model_config = SettingsConfigDict(env_prefix="DATABASE_")


class ApiConfig(BaseSettings):
    """API server configuration."""

    host: str = Field(default="0.0.0.0", description="API server host")
    port: int = Field(default=8000, description="API server port")
    api_key: Optional[str] = Field(default=None, description="Optional API key for authentication")
    cors_origins: List[str] = Field(default=["*"], description="CORS allowed origins")

    model_config = SettingsConfigDict(env_prefix="API_")


class TradingConfig(BaseSettings):
    """Trading configuration."""

    mode: str = Field(default="paper", description="Trading mode (paper/live)")
    max_position_size: float = Field(default=1000.0, description="Max position size per strategy")
    order_rate_limit: int = Field(default=10, description="Max orders per second")
    reconnect_delay: float = Field(default=1.0, description="WebSocket reconnect delay (seconds)")
    reconnect_max_delay: float = Field(default=60.0, description="Max reconnect delay (seconds)")

    model_config = SettingsConfigDict(env_prefix="TRADING_")


class DataConfig(BaseSettings):
    """Data storage configuration."""

    candlestick_intervals: List[str] = Field(
        default=["1m", "5m", "15m", "1h", "4h", "1d"],
        description="Candlestick aggregation intervals",
    )
    retention_days: int = Field(default=30, description="Data retention period in days")

    model_config = SettingsConfigDict(env_prefix="DATA_")


class Config(BaseSettings):
    """Main application configuration."""

    log_level: str = Field(default="INFO", description="Logging level")
    log_file: Optional[str] = Field(default=None, description="Log file path")

    polymarket: PolymarketConfig = Field(default_factory=PolymarketConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    data: DataConfig = Field(default_factory=DataConfig)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )


def load_config(config_file: Optional[str] = None) -> Config:
    """
    Load configuration from environment and optionally a YAML file.

    Args:
        config_file: Optional path to YAML config file

    Returns:
        Config instance with loaded values
    """
    config_dict: Dict[str, Any] = {}

    # Load from YAML file if provided
    if config_file:
        config_path = Path(config_file)
        if config_path.exists():
            with open(config_path) as f:
                config_dict = yaml.safe_load(f) or {}

    # Environment variables take precedence over YAML
    return Config(**config_dict)


def get_default_config_path() -> Optional[Path]:
    """Get the default config file path if it exists."""
    candidates = [
        Path("config.yaml"),
        Path("config.yml"),
        Path("polymoney.yaml"),
        Path("polymoney.yml"),
        Path.home() / ".polymoney" / "config.yaml",
    ]

    for path in candidates:
        if path.exists():
            return path

    return None
