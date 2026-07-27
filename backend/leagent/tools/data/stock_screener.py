"""Stock Screener Tool — filter stocks by conditions.

Screens the entire A-share market (or a subset) using criteria like
price change, volume, market cap, PE ratio, turnover rate, etc.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from leagent.tools.base import BaseTool, ToolCapability, ToolCategory, ToolContext
from leagent.tools.data._stock_common import call_ak

logger = structlog.get_logger(__name__)


class StockScreenerTool(BaseTool):
    """Screen stocks by user-defined conditions.

    Filters the market to find stocks matching multiple criteria such as
    change percentage, volume thresholds, market cap range, PE ratio, etc.
    Useful for finding trading opportunities that match specific strategies.
    """

    name = "stock_screener"
    description = (
        "Screen and filter stocks by conditions: change percentage, volume, market cap, "
        "PE ratio, PB ratio, turnover rate, amplitude, price range, etc. "
        "Returns a ranked list of stocks matching all specified criteria. "
        "Currently supports Chinese A-share market screening. "
        "Example: find stocks with >5% gain, volume >1M, and market cap >10B."
    )
    category = ToolCategory.DATA
    version = "1.0.0"
    timeout_sec = 90
    is_read_only = True
    is_concurrency_safe = True
    capabilities = (ToolCapability.NETWORK,)
    aliases = ["screen_stocks", "filter_stocks", "stock_filter"]
    search_hint = (
        "stock screen filter scan condition criteria 选股 筛选 条件 "
        "涨幅 跌幅 市值 市盈率 换手率 量比 成交量"
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": [],
            "additionalProperties": False,
            "properties": {
                "change_pct_min": {
                    "type": "number",
                    "description": "Minimum change percentage (e.g. 5 for >5% gain, -3 for loss > -3%).",
                },
                "change_pct_max": {
                    "type": "number",
                    "description": "Maximum change percentage.",
                },
                "volume_min": {
                    "type": "number",
                    "description": "Minimum volume in shares (e.g. 1000000 for >1M shares).",
                },
                "turnover_min": {
                    "type": "number",
                    "description": "Minimum turnover amount in CNY (e.g. 100000000 for >1亿).",
                },
                "market_cap_min": {
                    "type": "number",
                    "description": "Minimum total market cap in CNY (e.g. 10000000000 for >100亿).",
                },
                "market_cap_max": {
                    "type": "number",
                    "description": "Maximum total market cap in CNY.",
                },
                "pe_min": {
                    "type": "number",
                    "description": "Minimum PE ratio (dynamic).",
                },
                "pe_max": {
                    "type": "number",
                    "description": "Maximum PE ratio (dynamic). Use to filter overvalued stocks.",
                },
                "pb_min": {
                    "type": "number",
                    "description": "Minimum PB ratio.",
                },
                "pb_max": {
                    "type": "number",
                    "description": "Maximum PB ratio.",
                },
                "turnover_rate_min": {
                    "type": "number",
                    "description": "Minimum turnover rate percentage (e.g. 5 for >5%).",
                },
                "turnover_rate_max": {
                    "type": "number",
                    "description": "Maximum turnover rate percentage.",
                },
                "price_min": {
                    "type": "number",
                    "description": "Minimum current price.",
                },
                "price_max": {
                    "type": "number",
                    "description": "Maximum current price.",
                },
                "amplitude_min": {
                    "type": "number",
                    "description": "Minimum amplitude percentage.",
                },
                "volume_ratio_min": {
                    "type": "number",
                    "description": "Minimum volume ratio (量比).",
                },
                "name_contains": {
                    "type": "string",
                    "description": "Filter by stock name containing text (e.g. '银行', '新能源').",
                },
                "exclude_st": {
                    "type": "boolean",
                    "default": True,
                    "description": "Exclude ST/*ST stocks (default: true).",
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["change_pct", "volume", "turnover", "market_cap", "turnover_rate", "amplitude", "volume_ratio"],
                    "default": "change_pct",
                    "description": "Sort result by this field.",
                },
                "sort_order": {
                    "type": "string",
                    "enum": ["desc", "asc"],
                    "default": "desc",
                    "description": "Sort order (default: descending).",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "Maximum number of results (default: 20, max: 50).",
                },
            },
        }

    def get_activity_description(self, params: dict[str, Any] | None = None) -> str | None:
        return "Screening stocks by conditions"

    async def execute(self, params: dict[str, Any], context: ToolContext) -> Any:
        try:
            import akshare as ak
            import pandas as pd
        except ImportError as e:
            return {"success": False, "error": f"Missing dependency: {e}"}

        try:
            df = await asyncio.to_thread(call_ak, ak.stock_zh_a_spot_em)

            # Rename columns
            col_map = {
                "代码": "code", "名称": "name", "最新价": "price",
                "涨跌幅": "change_pct", "涨跌额": "change_amount",
                "成交量": "volume", "成交额": "turnover",
                "振幅": "amplitude", "最高": "high", "最低": "low",
                "今开": "open", "昨收": "prev_close",
                "量比": "volume_ratio", "换手率": "turnover_rate",
                "市盈率-动态": "pe", "市净率": "pb",
                "总市值": "market_cap", "流通市值": "circulating_cap",
            }
            df = df.rename(columns=col_map)

            # Convert numeric columns
            numeric_cols = ["price", "change_pct", "volume", "turnover", "amplitude",
                           "volume_ratio", "turnover_rate", "pe", "pb", "market_cap",
                           "circulating_cap"]
            for col in numeric_cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            # Apply filters
            mask = pd.Series(True, index=df.index)

            # Exclude ST stocks
            if params.get("exclude_st", True):
                mask &= ~df["name"].str.contains(r"ST|\*ST", na=False, regex=True)

            # Price filters
            if params.get("price_min") is not None:
                mask &= df["price"] >= params["price_min"]
            if params.get("price_max") is not None:
                mask &= df["price"] <= params["price_max"]

            # Change pct filters
            if params.get("change_pct_min") is not None:
                mask &= df["change_pct"] >= params["change_pct_min"]
            if params.get("change_pct_max") is not None:
                mask &= df["change_pct"] <= params["change_pct_max"]

            # Volume filters
            if params.get("volume_min") is not None:
                mask &= df["volume"] >= params["volume_min"]
            if params.get("turnover_min") is not None:
                mask &= df["turnover"] >= params["turnover_min"]

            # Market cap filters
            if params.get("market_cap_min") is not None:
                mask &= df["market_cap"] >= params["market_cap_min"]
            if params.get("market_cap_max") is not None:
                mask &= df["market_cap"] <= params["market_cap_max"]

            # PE filters
            if params.get("pe_min") is not None:
                mask &= df["pe"] >= params["pe_min"]
            if params.get("pe_max") is not None:
                mask &= df["pe"] <= params["pe_max"]

            # PB filters
            if params.get("pb_min") is not None:
                mask &= df["pb"] >= params["pb_min"]
            if params.get("pb_max") is not None:
                mask &= df["pb"] <= params["pb_max"]

            # Turnover rate filters
            if params.get("turnover_rate_min") is not None:
                mask &= df["turnover_rate"] >= params["turnover_rate_min"]
            if params.get("turnover_rate_max") is not None:
                mask &= df["turnover_rate"] <= params["turnover_rate_max"]

            # Amplitude filter
            if params.get("amplitude_min") is not None:
                mask &= df["amplitude"] >= params["amplitude_min"]

            # Volume ratio filter
            if params.get("volume_ratio_min") is not None:
                mask &= df["volume_ratio"] >= params["volume_ratio_min"]

            # Name filter
            if params.get("name_contains"):
                mask &= df["name"].str.contains(params["name_contains"], na=False)

            filtered = df[mask].copy()

            # Sort
            sort_by = params.get("sort_by", "change_pct")
            sort_order = params.get("sort_order", "desc")
            ascending = sort_order == "asc"
            if sort_by in filtered.columns:
                filtered = filtered.sort_values(sort_by, ascending=ascending, na_position="last")

            # Limit results
            limit = min(params.get("limit", 20), 50)
            filtered = filtered.head(limit)

            # Build output
            output_cols = ["code", "name", "price", "change_pct", "volume",
                          "turnover", "turnover_rate", "pe", "pb", "market_cap"]
            existing_cols = [c for c in output_cols if c in filtered.columns]
            records = filtered[existing_cols].to_dict(orient="records")

            # Round numeric values
            for rec in records:
                for k, v in rec.items():
                    if isinstance(v, float):
                        rec[k] = round(v, 2) if v == v else None

            return {
                "success": True,
                "total_market_stocks": len(df),
                "matched_count": int(mask.sum()),
                "returned_count": len(records),
                "sort_by": sort_by,
                "sort_order": sort_order,
                "results": records,
            }

        except Exception as e:
            logger.warning("stock_screener_failed", error=str(e))
            return {"success": False, "error": str(e)}
