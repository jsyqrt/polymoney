"""
Configuration loader for PolyMoney.

Reads YAML configuration files and provides typed access to parameters.
Supports layered configuration: YAML defaults → CLI overrides.

Usage:
    config = load_config("config/strategy_defaults.yaml")
    strategy_params = config.get_strategy_params()
    sim_config = config.to_simulation_config()
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from polymoney.core.logging import get_logger

logger = get_logger("config")

# Default config file path (relative to project root)
DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config" / "strategy_defaults.yaml"


class StrategyConfig:
    """
    Unified configuration object for strategy and simulation parameters.
    
    Loads from YAML and provides typed access to all parameter groups.
    Supports merging with CLI overrides.
    """

    def __init__(self, data: Optional[Dict[str, Any]] = None):
        self._data = data or {}

    @classmethod
    def from_yaml(cls, path: str | Path) -> "StrategyConfig":
        """
        Load configuration from a YAML file.
        
        Args:
            path: Path to the YAML configuration file.
        
        Returns:
            StrategyConfig instance with loaded parameters.
        
        Raises:
            FileNotFoundError: If the config file does not exist.
            yaml.YAMLError: If the file is not valid YAML.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            raise ValueError(f"Config file must contain a YAML mapping, got {type(data)}")

        logger.info(f"Loaded config from {path}")
        return cls(data)

    @classmethod
    def from_defaults(cls) -> "StrategyConfig":
        """Load from the default config file, or return empty config if not found."""
        if DEFAULT_CONFIG_PATH.exists():
            return cls.from_yaml(DEFAULT_CONFIG_PATH)
        logger.warning(f"Default config not found at {DEFAULT_CONFIG_PATH}, using built-in defaults")
        return cls({})

    # -------------------------------------------------------------------------
    # Section accessors
    # -------------------------------------------------------------------------

    def _section(self, name: str) -> Dict[str, Any]:
        """Get a configuration section by name."""
        return self._data.get(name, {})

    @property
    def strategy(self) -> Dict[str, Any]:
        return self._section("strategy")

    @property
    def risk(self) -> Dict[str, Any]:
        return self._section("risk")

    @property
    def trend_detection(self) -> Dict[str, Any]:
        return self._section("trend_detection")

    @property
    def urgency_pricing(self) -> Dict[str, Any]:
        return self._section("urgency_pricing")

    @property
    def simulation(self) -> Dict[str, Any]:
        return self._section("simulation")

    @property
    def fund_management(self) -> Dict[str, Any]:
        return self._section("fund_management")

    # -------------------------------------------------------------------------
    # Typed parameter accessors
    # -------------------------------------------------------------------------

    def get_strategy_params(self, **overrides) -> Dict[str, Any]:
        """
        Build strategy_params dict for PositionArbitrageStrategy.
        
        Merges YAML config with optional keyword overrides.
        Override keys take precedence over YAML values.
        
        Returns:
            Dict suitable for passing to PositionArbitrageStrategy(params=...).
        """
        s = self.strategy
        r = self.risk
        t = self.trend_detection
        u = self.urgency_pricing

        params = {
            # Strategy core
            "target_cost": s.get("target_cost", 0.96),
            "batch_ratio": s.get("batch_ratio", 0.001),
            "order_timeout": s.get("order_timeout", 60),
            "phase1_end": s.get("phase1_end", 300),
            "phase2_end": s.get("phase2_end", 600),
            "low_prob_threshold": s.get("low_prob_threshold", 0.05),
            "max_orders_per_tick": s.get("max_orders_per_tick", 2),
            # Risk control
            "ecr_threshold": r.get("ecr_threshold", 1.05),
            "balance_threshold": r.get("balance_threshold", 0.70),
            "enable_ecr_stoploss": r.get("enable_ecr_stoploss", True),
            "enable_rebalancing": r.get("enable_rebalancing", True),
            "rebalancing_cooldown": r.get("rebalancing_cooldown", 30),
            "severe_imbalance_threshold": r.get("severe_imbalance_threshold", 0.50),
            "market_order_size_cap": r.get("market_order_size_cap", 0.10),
            "max_skew_threshold": r.get("max_skew_threshold", 0.85),
            "trend_patience_threshold": r.get("trend_patience_threshold", 0.65),
            # Trend detection
            "enable_trend_detection": t.get("enabled", True),
            "momentum_window_seconds": t.get("momentum_window_seconds", 30.0),
            "max_momentum_threshold": t.get("max_momentum_threshold", 0.10),
            "trend_stop_threshold": t.get("trend_stop_threshold", 0.70),
            # Urgency pricing
            "enable_urgency_pricing": u.get("enabled", True),
            "urgency_weight": u.get("urgency_weight", 1.5),
            "urgency_price_factor": u.get("urgency_price_factor", 0.5),
            "market_duration": u.get("market_duration", 900),
        }

        # Pass through explicit batch_size only if set in YAML
        # (otherwise auto-derived from position_size × batch_ratio)
        if "batch_size" in s:
            params["batch_size"] = s["batch_size"]

        # Apply overrides
        for key, value in overrides.items():
            if value is not None:
                params[key] = value

        return params

    def get_position_size(self, override: Optional[float] = None) -> float:
        """Get position size, with optional override."""
        if override is not None:
            return override
        return self.strategy.get("position_size", 100.0)

    def get_coins(self, override: Optional[List[str]] = None) -> List[str]:
        """Get list of coins to monitor."""
        if override is not None:
            return override
        return self.simulation.get("coins", ["btc", "eth", "sol"])

    def to_simulation_config(self, **overrides):
        """
        Build a SimulationConfig from YAML + overrides.
        
        Import is done lazily to avoid circular imports.
        
        Args:
            **overrides: CLI argument overrides. Keys should match
                         SimulationConfig field names. None values are ignored.
        
        Returns:
            SimulationConfig instance.
        """
        from polymoney.simulation.live_runner import SimulationConfig

        s = self.strategy
        r = self.risk
        t = self.trend_detection
        u = self.urgency_pricing
        sim = self.simulation
        fm = self.fund_management

        # Build kwargs from YAML
        kwargs = {
            "coins": sim.get("coins", ["btc", "eth", "sol"]),
            "duration_seconds": sim.get("duration_seconds", 0),
            "output_dir": Path(sim.get("output_dir", "simulation_results")),
            "target_cost": s.get("target_cost", 0.96),
            "batch_ratio": s.get("batch_ratio", 0.001),
            "position_size": s.get("position_size", 100.0),
            "ecr_threshold": r.get("ecr_threshold", 1.05),
            "enable_ecr_stoploss": r.get("enable_ecr_stoploss", True),
            "enable_rebalancing": r.get("enable_rebalancing", True),
            "enable_trend_detection": t.get("enabled", True),
            "enable_urgency_pricing": u.get("enabled", True),
            "market_scan_interval": sim.get("market_scan_interval", 60.0),
            "metrics_output_interval": sim.get("metrics_output_interval", 1.0),
            "order_timeout": sim.get("order_timeout", 30.0),
            "stale_order_threshold": sim.get("stale_order_threshold", 0.20),
            "min_trading_time": sim.get("min_trading_time", 300.0),
            "min_price_threshold": sim.get("min_price_threshold", 0.05),
            "max_entry_skew": sim.get("max_entry_skew", 0.75),
            "max_concurrent_markets": fm.get("max_concurrent_markets", 6),
            "max_total_exposure": fm.get("max_total_exposure", 500.0),
            "redemption_delay": fm.get("redemption_delay", 60.0),
        }

        # Apply overrides (None values are skipped)
        for key, value in overrides.items():
            if value is not None and key in kwargs:
                kwargs[key] = value

        return SimulationConfig(**kwargs)

    def __repr__(self) -> str:
        sections = list(self._data.keys())
        return f"StrategyConfig(sections={sections})"


def load_config(path: Optional[str | Path] = None) -> StrategyConfig:
    """
    Load configuration from a YAML file.
    
    Args:
        path: Path to config file. If None, loads from default location.
    
    Returns:
        StrategyConfig instance.
    """
    if path:
        return StrategyConfig.from_yaml(path)
    return StrategyConfig.from_defaults()
