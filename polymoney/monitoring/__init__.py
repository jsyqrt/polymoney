"""
Monitoring and observability for trading operations.

Provides:
- AlertManager: Webhook-based alerts (Discord, Telegram, etc.)
- TradeLogger: Structured trade logging to JSONL files
"""

from polymoney.monitoring.alerts import AlertManager

__all__ = ["AlertManager"]
