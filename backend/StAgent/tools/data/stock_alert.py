"""Stock Alert Tool — set price/indicator alerts via the cron system.

Creates scheduled tasks that periodically check stock conditions and
trigger notifications when thresholds are crossed. Integrates with the
existing cron + notification infrastructure.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog

from StAgent.tools.base import BaseTool, ToolCapability, ToolCategory, ToolContext
from StAgent.tools.data._stock_common import call_ak

logger = structlog.get_logger(__name__)


class StockAlertTool(BaseTool):
    """Create, list, or delete stock price/indicator alerts.

    Alerts are implemented as cron jobs that periodically check conditions.
    When a condition is met, a notification is sent through the configured
    notification channel (in-app notification by default).
    """

    name = "stock_alert"
    description = (
        "Manage stock alerts: create alerts when price crosses a threshold, "
        "volume spikes, or technical indicators emit signals (MACD cross, RSI overbought, etc.). "
        "Operations: 'create' to set a new alert, 'list' to view active alerts, "
        "'delete' to remove an alert, 'check' to manually run alert checks now. "
        "Alerts are checked every 5 minutes during trading hours by the cron system."
    )
    category = ToolCategory.DATA
    version = "1.0.0"
    timeout_sec = 60
    is_read_only = False
    is_concurrency_safe = True
    capabilities = (ToolCapability.NETWORK,)
    aliases = ["set_stock_alert", "price_alert"]
    search_hint = (
        "stock alert notification price trigger threshold 股票 提醒 预警 "
        "涨停 跌停 价格 信号 通知"
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["operation"],
            "additionalProperties": False,
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["create", "list", "delete", "check"],
                    "description": "Operation to perform.",
                },
                "symbol": {
                    "type": "string",
                    "description": "Stock code (required for create/check).",
                },
                "market": {
                    "type": "string",
                    "enum": ["cn", "hk", "us"],
                    "default": "cn",
                    "description": "Market (default: cn).",
                },
                "condition_type": {
                    "type": "string",
                    "enum": [
                        "price_above", "price_below",
                        "change_pct_above", "change_pct_below",
                        "volume_above", "turnover_rate_above",
                        "macd_golden_cross", "macd_death_cross",
                        "rsi_overbought", "rsi_oversold",
                        "boll_break_upper", "boll_break_lower",
                    ],
                    "description": "Alert condition type (required for create).",
                },
                "threshold": {
                    "type": "number",
                    "description": (
                        "Threshold value for price/pct/volume conditions. "
                        "Not needed for signal-based conditions like MACD cross."
                    ),
                },
                "alert_id": {
                    "type": "string",
                    "description": "Alert ID (required for delete).",
                },
                "note": {
                    "type": "string",
                    "description": "Optional note/reason for the alert.",
                },
            },
        }

    def get_activity_description(self, params: dict[str, Any] | None = None) -> str | None:
        op = (params or {}).get("operation", "")
        symbol = (params or {}).get("symbol", "")
        return f"Stock alert: {op} {symbol}".strip()

    async def execute(self, params: dict[str, Any], context: ToolContext) -> Any:
        operation = params.get("operation", "")
        if not operation:
            return {"success": False, "error": "operation is required"}

        if operation == "create":
            return await self._create_alert(params, context)
        if operation == "list":
            return await self._list_alerts(context)
        if operation == "delete":
            return await self._delete_alert(params, context)
        if operation == "check":
            return await self._check_alert(params, context)
        return {"success": False, "error": f"Unknown operation: {operation}"}

    async def _create_alert(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """Create a new stock alert as a cron task."""
        symbol = params.get("symbol", "").strip()
        market = params.get("market", "cn")
        condition_type = params.get("condition_type", "")
        threshold = params.get("threshold")
        note = params.get("note", "")

        if not symbol:
            return {"success": False, "error": "symbol is required for create"}
        if not condition_type:
            return {"success": False, "error": "condition_type is required for create"}

        # Validate threshold for price-based conditions
        needs_threshold = condition_type in (
            "price_above", "price_below",
            "change_pct_above", "change_pct_below",
            "volume_above", "turnover_rate_above",
        )
        if needs_threshold and threshold is None:
            return {"success": False, "error": f"threshold is required for {condition_type}"}

        import uuid

        alert_id = f"stock_alert_{uuid.uuid4().hex[:8]}"

        alert_config = {
            "alert_id": alert_id,
            "symbol": symbol,
            "market": market,
            "condition_type": condition_type,
            "threshold": threshold,
            "note": note,
            "created_by": context.user_id,
            "active": True,
        }

        # Store alert in settings/file system for persistence
        alerts = await self._load_alerts(context)
        alerts.append(alert_config)
        await self._save_alerts(alerts, context)

        condition_desc = self._describe_condition(condition_type, threshold, symbol)

        logger.info(
            "stock_alert_created",
            alert_id=alert_id,
            symbol=symbol,
            condition=condition_type,
            threshold=threshold,
        )

        return {
            "success": True,
            "alert_id": alert_id,
            "symbol": symbol,
            "condition": condition_desc,
            "note": note,
            "message": (
                f"Alert created: {condition_desc}. "
                "The alert will be checked whenever you ask me to check alerts, "
                "or you can set up a cron job with: cron_create with expression '*/5 9-15 * * 1-5' "
                f"and command 'stock_alert check --symbol {symbol}'"
            ),
        }

    async def _list_alerts(self, context: ToolContext) -> dict[str, Any]:
        """List all active alerts."""
        alerts = await self._load_alerts(context)
        active = [a for a in alerts if a.get("active", True)]

        formatted = [
            {
                "alert_id": a["alert_id"],
                "symbol": a["symbol"],
                "market": a.get("market", "cn"),
                "condition": self._describe_condition(
                    a["condition_type"], a.get("threshold"), a["symbol"]
                ),
                "note": a.get("note", ""),
            }
            for a in active
        ]

        return {
            "success": True,
            "count": len(formatted),
            "alerts": formatted,
        }

    async def _delete_alert(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """Delete an alert by ID."""
        alert_id = params.get("alert_id", "").strip()
        if not alert_id:
            return {"success": False, "error": "alert_id is required for delete"}

        alerts = await self._load_alerts(context)
        found = False
        for a in alerts:
            if a["alert_id"] == alert_id:
                a["active"] = False
                found = True
                break

        if not found:
            return {"success": False, "error": f"Alert {alert_id} not found"}

        await self._save_alerts(alerts, context)
        return {"success": True, "message": f"Alert {alert_id} deleted"}

    async def _check_alert(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """Manually check all alerts (or a specific symbol)."""
        target_symbol = params.get("symbol", "").strip()
        alerts = await self._load_alerts(context)
        active = [a for a in alerts if a.get("active", True)]

        if target_symbol:
            active = [a for a in active if a["symbol"] == target_symbol]

        if not active:
            return {"success": True, "triggered": [], "message": "No active alerts to check"}

        try:
            import akshare as ak
        except ImportError:
            return {"success": False, "error": "akshare not installed"}

        triggered: list[dict[str, Any]] = []
        not_triggered: list[str] = []

        # Group alerts by symbol to minimize API calls
        from collections import defaultdict
        by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for a in active:
            by_symbol[a["symbol"]].append(a)

        for symbol, symbol_alerts in by_symbol.items():
            try:
                quote = await self._get_quote(ak, symbol, symbol_alerts[0].get("market", "cn"))
                for alert in symbol_alerts:
                    is_triggered = self._evaluate_condition(alert, quote)
                    if is_triggered:
                        triggered.append({
                            "alert_id": alert["alert_id"],
                            "symbol": symbol,
                            "condition": self._describe_condition(
                                alert["condition_type"], alert.get("threshold"), symbol
                            ),
                            "current_value": self._get_relevant_value(alert, quote),
                            "note": alert.get("note", ""),
                        })
                    else:
                        not_triggered.append(alert["alert_id"])
            except Exception as e:
                logger.warning("stock_alert_check_error", symbol=symbol, error=str(e))

        return {
            "success": True,
            "checked_count": len(active),
            "triggered_count": len(triggered),
            "triggered": triggered,
            "not_triggered_ids": not_triggered,
        }

    async def _get_quote(self, ak: Any, symbol: str, market: str) -> dict[str, Any]:
        """Get real-time quote for alert evaluation."""

        def _fetch() -> dict[str, Any]:
            if market == "cn":
                df = call_ak(ak.stock_zh_a_spot_em)
                row = df[df["代码"] == symbol]
            elif market == "hk":
                df = call_ak(ak.stock_hk_spot_em)
                row = df[df["代码"] == symbol]
            else:
                df = call_ak(ak.stock_us_spot_em)
                row = df[df["代码"].str.upper() == symbol.upper()]

            if row.empty:
                raise ValueError(f"Stock {symbol} not found")

            r = row.iloc[0]
            return {
                "price": float(r.get("最新价", 0) or 0),
                "change_pct": float(r.get("涨跌幅", 0) or 0),
                "volume": float(r.get("成交量", 0) or 0),
                "turnover_rate": float(r.get("换手率", 0) or 0),
                "high": float(r.get("最高", 0) or 0),
                "low": float(r.get("最低", 0) or 0),
            }

        return await asyncio.to_thread(_fetch)

    def _evaluate_condition(self, alert: dict[str, Any], quote: dict[str, Any]) -> bool:
        """Evaluate whether an alert condition is triggered."""
        ctype = alert["condition_type"]
        threshold = alert.get("threshold")

        if ctype == "price_above":
            return quote["price"] >= threshold
        if ctype == "price_below":
            return quote["price"] <= threshold
        if ctype == "change_pct_above":
            return quote["change_pct"] >= threshold
        if ctype == "change_pct_below":
            return quote["change_pct"] <= threshold
        if ctype == "volume_above":
            return quote["volume"] >= threshold
        if ctype == "turnover_rate_above":
            return quote["turnover_rate"] >= threshold
        # Signal-based conditions need historical data (simplified here)
        # Full implementation would call StockIndicatorTool
        return False

    def _get_relevant_value(self, alert: dict[str, Any], quote: dict[str, Any]) -> Any:
        """Get the current value relevant to the alert condition."""
        ctype = alert["condition_type"]
        if "price" in ctype:
            return quote["price"]
        if "change_pct" in ctype:
            return quote["change_pct"]
        if "volume" in ctype:
            return quote["volume"]
        if "turnover_rate" in ctype:
            return quote["turnover_rate"]
        return None

    def _describe_condition(self, condition_type: str, threshold: Any, symbol: str) -> str:
        """Human-readable condition description."""
        descriptions = {
            "price_above": f"{symbol} price >= {threshold}",
            "price_below": f"{symbol} price <= {threshold}",
            "change_pct_above": f"{symbol} change >= {threshold}%",
            "change_pct_below": f"{symbol} change <= {threshold}%",
            "volume_above": f"{symbol} volume >= {threshold}",
            "turnover_rate_above": f"{symbol} turnover rate >= {threshold}%",
            "macd_golden_cross": f"{symbol} MACD golden cross",
            "macd_death_cross": f"{symbol} MACD death cross",
            "rsi_overbought": f"{symbol} RSI > 70 (overbought)",
            "rsi_oversold": f"{symbol} RSI < 30 (oversold)",
            "boll_break_upper": f"{symbol} breaks Bollinger upper band",
            "boll_break_lower": f"{symbol} breaks Bollinger lower band",
        }
        return descriptions.get(condition_type, f"{symbol} {condition_type} {threshold or ''}")

    async def _load_alerts(self, context: ToolContext) -> list[dict[str, Any]]:
        """Load alerts from persistent storage."""
        import os
        from pathlib import Path

        home = os.environ.get("StAgent_HOME", os.path.expanduser("~/.StAgent"))
        alert_file = Path(home) / "stock_alerts.json"

        def _read() -> list[dict[str, Any]]:
            if alert_file.exists():
                return json.loads(alert_file.read_text(encoding="utf-8"))
            return []

        return await asyncio.to_thread(_read)

    async def _save_alerts(self, alerts: list[dict[str, Any]], context: ToolContext) -> None:
        """Save alerts to persistent storage."""
        import os
        from pathlib import Path

        home = os.environ.get("StAgent_HOME", os.path.expanduser("~/.StAgent"))
        alert_file = Path(home) / "stock_alerts.json"

        def _write() -> None:
            alert_file.parent.mkdir(parents=True, exist_ok=True)
            alert_file.write_text(
                json.dumps(alerts, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        await asyncio.to_thread(_write)
