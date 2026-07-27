"""Stock Quote Tool — fetch real-time and historical market data.

Supports Chinese A-share (SH/SZ/BJ), HK, and US markets via akshare.
Data includes OHLCV (open, high, low, close, volume) and basic info.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from leagent.tools.base import BaseTool, ToolCapability, ToolCategory, ToolContext
from leagent.tools.data._stock_common import call_ak

logger = structlog.get_logger(__name__)


class StockQuoteTool(BaseTool):
    """Fetch stock quote data — real-time snapshot or historical OHLCV.

    Retrieves market data for individual stocks including price, volume,
    and fundamental info. Supports A-share (沪深京), Hong Kong, and US markets.
    """

    name = "stock_quote"
    description = (
        "Fetch stock market data: real-time quotes or historical OHLCV (open/high/low/close/volume). "
        "Supports Chinese A-share (codes like 600519, 000001, 301234), HK stocks, and US stocks. "
        "Use period='daily'/'weekly'/'monthly' for historical data, or 'realtime' for current snapshot. "
        "Returns structured price data suitable for technical analysis or charting."
    )
    category = ToolCategory.DATA
    version = "1.0.0"
    timeout_sec = 60
    is_read_only = True
    is_concurrency_safe = True
    capabilities = (ToolCapability.NETWORK,)
    aliases = ["get_stock", "stock_data", "stock_price"]
    search_hint = (
        "stock quote price market data A-share 股票 行情 价格 K线 "
        "沪深 港股 美股 OHLCV candlestick historical realtime"
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["symbol"],
            "additionalProperties": False,
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": (
                        "Stock code. A-share: 6-digit (e.g. '600519' for 贵州茅台, '000001' for 平安银行). "
                        "HK: 5-digit with prefix (e.g. '09988' for 阿里巴巴-W). "
                        "US: ticker symbol (e.g. 'AAPL', 'TSLA')."
                    ),
                },
                "market": {
                    "type": "string",
                    "enum": ["cn", "hk", "us"],
                    "default": "cn",
                    "description": "Market: cn=A-share (default), hk=Hong Kong, us=US.",
                },
                "period": {
                    "type": "string",
                    "enum": ["realtime", "daily", "weekly", "monthly"],
                    "default": "daily",
                    "description": "Data period. 'realtime' for current snapshot, others for historical.",
                },
                "start_date": {
                    "type": "string",
                    "description": "Start date for historical data (YYYYMMDD). Default: 3 months ago.",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date for historical data (YYYYMMDD). Default: today.",
                },
                "adjust": {
                    "type": "string",
                    "enum": ["qfq", "hfq", "none"],
                    "default": "qfq",
                    "description": "Price adjustment: qfq=前复权 (default), hfq=后复权, none=不复权.",
                },
            },
        }

    def get_activity_description(self, params: dict[str, Any] | None = None) -> str | None:
        symbol = (params or {}).get("symbol", "")
        return f"Fetching stock data for {symbol}" if symbol else "Fetching stock data"

    async def execute(self, params: dict[str, Any], context: ToolContext) -> Any:
        symbol = str(params["symbol"]).strip()
        market = params.get("market", "cn")
        period = params.get("period", "daily")
        adjust = params.get("adjust", "qfq")
        start_date = params.get("start_date", "")
        end_date = params.get("end_date", "")

        if not symbol:
            return {"success": False, "error": "symbol is required"}

        try:
            import akshare as ak
        except ImportError:
            return {
                "success": False,
                "error": "akshare not installed. Run: uv pip install akshare",
            }

        try:
            if period == "realtime":
                try:
                    data = await self._fetch_realtime(ak, symbol, market)
                except Exception as spot_exc:  # noqa: BLE001 — degrade to latest daily bar
                    logger.warning(
                        "stock_quote_realtime_fallback", symbol=symbol, error=str(spot_exc)
                    )
                    hist = await self._fetch_historical(
                        ak, symbol, market, "daily", "qfq", "", ""
                    )
                    records = hist.get("records") or []
                    if not records:
                        raise
                    last = records[-1]
                    data = {
                        "note": "realtime source unavailable; returning latest daily bar",
                        "date": last.get("date"),
                        "current_price": last.get("close"),
                        "open": last.get("open"),
                        "high": last.get("high"),
                        "low": last.get("low"),
                        "volume": last.get("volume"),
                        "change_pct": last.get("change_pct"),
                    }
            else:
                data = await self._fetch_historical(
                    ak, symbol, market, period, adjust, start_date, end_date
                )
            return {"success": True, "symbol": symbol, "market": market, "period": period, **data}
        except Exception as e:
            logger.warning("stock_quote_failed", symbol=symbol, error=str(e))
            return {"success": False, "error": str(e), "symbol": symbol}

    async def _fetch_realtime(self, ak: Any, symbol: str, market: str) -> dict[str, Any]:
        """Fetch real-time snapshot."""

        def _get() -> dict[str, Any]:
            if market == "cn":
                df = call_ak(ak.stock_zh_a_spot_em)
                row = df[df["代码"] == symbol]
                if row.empty:
                    raise ValueError(f"Stock {symbol} not found in A-share market")
                r = row.iloc[0]
                return {
                    "name": str(r.get("名称", "")),
                    "current_price": float(r.get("最新价", 0)),
                    "change_pct": float(r.get("涨跌幅", 0)),
                    "change_amount": float(r.get("涨跌额", 0)),
                    "volume": float(r.get("成交量", 0)),
                    "turnover": float(r.get("成交额", 0)),
                    "amplitude": float(r.get("振幅", 0)),
                    "high": float(r.get("最高", 0)),
                    "low": float(r.get("最低", 0)),
                    "open": float(r.get("今开", 0)),
                    "prev_close": float(r.get("昨收", 0)),
                    "volume_ratio": float(r.get("量比", 0)),
                    "turnover_rate": float(r.get("换手率", 0)),
                    "pe_ratio": float(r.get("市盈率-动态", 0)) if r.get("市盈率-动态") else None,
                    "pb_ratio": float(r.get("市净率", 0)) if r.get("市净率") else None,
                    "market_cap": float(r.get("总市值", 0)),
                    "circulating_cap": float(r.get("流通市值", 0)),
                }
            if market == "hk":
                df = call_ak(ak.stock_hk_spot_em)
                row = df[df["代码"] == symbol]
                if row.empty:
                    raise ValueError(f"Stock {symbol} not found in HK market")
                r = row.iloc[0]
                return {
                    "name": str(r.get("名称", "")),
                    "current_price": float(r.get("最新价", 0)),
                    "change_pct": float(r.get("涨跌幅", 0)),
                    "volume": float(r.get("成交量", 0)),
                    "turnover": float(r.get("成交额", 0)),
                    "high": float(r.get("最高", 0)),
                    "low": float(r.get("最低", 0)),
                    "open": float(r.get("今开", 0)),
                    "prev_close": float(r.get("昨收", 0)),
                }
            # us
            df = call_ak(ak.stock_us_spot_em)
            row = df[df["代码"].str.upper() == symbol.upper()]
            if row.empty:
                # Try matching by name contains
                row = df[df["名称"].str.contains(symbol, case=False, na=False)]
            if row.empty:
                raise ValueError(f"Stock {symbol} not found in US market")
            r = row.iloc[0]
            return {
                "name": str(r.get("名称", "")),
                "current_price": float(r.get("最新价", 0)),
                "change_pct": float(r.get("涨跌幅", 0)),
                "volume": float(r.get("成交量", 0)),
                "turnover": float(r.get("成交额", 0)),
                "high": float(r.get("最高", 0)),
                "low": float(r.get("最低", 0)),
                "open": float(r.get("今开", 0)),
                "prev_close": float(r.get("昨收", 0)),
            }

        return await asyncio.to_thread(_get)

    async def _fetch_historical(
        self,
        ak: Any,
        symbol: str,
        market: str,
        period: str,
        adjust: str,
        start_date: str,
        end_date: str,
    ) -> dict[str, Any]:
        """Fetch historical OHLCV data."""
        import datetime

        if not start_date:
            start_date = (datetime.date.today() - datetime.timedelta(days=90)).strftime("%Y%m%d")
        if not end_date:
            end_date = datetime.date.today().strftime("%Y%m%d")

        def _get() -> dict[str, Any]:
            if market == "cn":
                df = call_ak(
                    ak.stock_zh_a_hist,
                    symbol=symbol,
                    period=period,
                    start_date=start_date,
                    end_date=end_date,
                    adjust=adjust if adjust != "none" else "",
                )
            elif market == "hk":
                df = call_ak(
                    ak.stock_hk_hist,
                    symbol=symbol,
                    period=period,
                    start_date=start_date,
                    end_date=end_date,
                    adjust=adjust if adjust != "none" else "",
                )
            else:  # us
                df = call_ak(
                    ak.stock_us_hist,
                    symbol=symbol,
                    period=period,
                    start_date=start_date,
                    end_date=end_date,
                    adjust=adjust if adjust != "none" else "",
                )

            if df.empty:
                return {"count": 0, "records": [], "note": "No data returned for the given parameters"}

            # Normalize column names
            col_map = {
                "日期": "date", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume",
                "成交额": "turnover", "振幅": "amplitude",
                "涨跌幅": "change_pct", "涨跌额": "change_amount",
                "换手率": "turnover_rate",
            }
            df = df.rename(columns=col_map)

            # Convert date to string
            if "date" in df.columns:
                df["date"] = df["date"].astype(str)

            records = df.to_dict(orient="records")
            # Limit to last 100 records to avoid oversized responses
            if len(records) > 100:
                records = records[-100:]
                truncated = True
            else:
                truncated = False

            return {
                "count": len(records),
                "total_available": len(df),
                "truncated": truncated,
                "start_date": start_date,
                "end_date": end_date,
                "adjust": adjust,
                "records": records,
            }

        return await asyncio.to_thread(_get)
