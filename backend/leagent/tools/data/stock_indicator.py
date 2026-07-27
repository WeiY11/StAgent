"""Stock Technical Indicator Tool — compute common indicators on price data.

Calculates MA, EMA, MACD, RSI, Bollinger Bands, KDJ, and more from OHLCV data.
All computation is local (pandas/numpy) — no external API calls needed after
the price data is already fetched.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from leagent.tools.base import BaseTool, ToolCapability, ToolCategory, ToolContext
from leagent.tools.data._stock_common import call_ak

logger = structlog.get_logger(__name__)


class StockIndicatorTool(BaseTool):
    """Calculate technical indicators for a given stock.

    Fetches historical data and computes one or more indicators such as
    MA, EMA, MACD, RSI, Bollinger Bands, KDJ, BOLL, ATR, OBV, etc.
    Returns numeric results suitable for analysis or charting.
    """

    name = "stock_indicator"
    description = (
        "Calculate stock technical indicators from historical price data. "
        "Supports: MA (moving average), EMA, MACD, RSI, BOLL (Bollinger Bands), "
        "KDJ, ATR, OBV, VWAP, WR (Williams %R). "
        "Provide a stock symbol and indicator name; returns computed values. "
        "Use with stock_chart for visualization."
    )
    category = ToolCategory.DATA
    version = "1.0.0"
    timeout_sec = 60
    is_read_only = True
    is_concurrency_safe = True
    capabilities = (ToolCapability.NETWORK,)
    aliases = ["technical_indicator", "stock_analysis"]
    search_hint = (
        "stock indicator technical analysis MA MACD RSI bollinger KDJ 技术指标 "
        "均线 布林带 相对强弱 移动平均线 成交量"
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["symbol", "indicators"],
            "additionalProperties": False,
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Stock code (e.g. '600519' for A-share).",
                },
                "market": {
                    "type": "string",
                    "enum": ["cn", "hk", "us"],
                    "default": "cn",
                    "description": "Market: cn (default), hk, us.",
                },
                "indicators": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["ma", "ema", "macd", "rsi", "boll", "kdj", "atr", "obv", "vwap", "wr"],
                    },
                    "description": "List of indicators to compute.",
                },
                "ma_periods": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "default": [5, 10, 20, 60],
                    "description": "MA/EMA periods (default: [5,10,20,60]).",
                },
                "period": {
                    "type": "string",
                    "enum": ["daily", "weekly", "monthly"],
                    "default": "daily",
                    "description": "Data frequency.",
                },
                "days": {
                    "type": "integer",
                    "default": 120,
                    "description": "Number of trading days of history to fetch (default: 120).",
                },
            },
        }

    def get_activity_description(self, params: dict[str, Any] | None = None) -> str | None:
        symbol = (params or {}).get("symbol", "")
        indicators = (params or {}).get("indicators", [])
        return f"Computing {','.join(indicators)} for {symbol}"

    async def execute(self, params: dict[str, Any], context: ToolContext) -> Any:
        symbol = str(params["symbol"]).strip()
        market = params.get("market", "cn")
        indicators = params.get("indicators", [])
        ma_periods = params.get("ma_periods", [5, 10, 20, 60])
        period = params.get("period", "daily")
        days = params.get("days", 120)

        if not symbol or not indicators:
            return {"success": False, "error": "symbol and indicators are required"}

        try:
            import akshare as ak
        except ImportError as e:
            return {"success": False, "error": f"Missing dependency: {e}"}

        try:
            df = await self._fetch_data(ak, symbol, market, period, days)
            if df.empty:
                return {"success": False, "error": f"No data for {symbol}"}

            results: dict[str, Any] = {"symbol": symbol, "market": market, "data_points": len(df)}
            indicator_data: dict[str, Any] = {}

            for ind in indicators:
                ind_lower = ind.lower()
                if ind_lower == "ma":
                    indicator_data["ma"] = self._calc_ma(df, ma_periods)
                elif ind_lower == "ema":
                    indicator_data["ema"] = self._calc_ema(df, ma_periods)
                elif ind_lower == "macd":
                    indicator_data["macd"] = self._calc_macd(df)
                elif ind_lower == "rsi":
                    indicator_data["rsi"] = self._calc_rsi(df)
                elif ind_lower == "boll":
                    indicator_data["boll"] = self._calc_boll(df)
                elif ind_lower == "kdj":
                    indicator_data["kdj"] = self._calc_kdj(df)
                elif ind_lower == "atr":
                    indicator_data["atr"] = self._calc_atr(df)
                elif ind_lower == "obv":
                    indicator_data["obv"] = self._calc_obv(df)
                elif ind_lower == "vwap":
                    indicator_data["vwap"] = self._calc_vwap(df)
                elif ind_lower == "wr":
                    indicator_data["wr"] = self._calc_wr(df)

            results["indicators"] = indicator_data
            results["success"] = True
            # Include last 20 dates for reference
            results["recent_dates"] = df["date"].tail(20).tolist()
            return results

        except Exception as e:
            logger.warning("stock_indicator_failed", symbol=symbol, error=str(e))
            return {"success": False, "error": str(e), "symbol": symbol}

    async def _fetch_data(self, ak: Any, symbol: str, market: str, period: str, days: int) -> Any:
        """Fetch historical OHLCV data for indicator computation."""
        import datetime

        import pandas as pd  # noqa: TC002 — runtime lazy import

        end_date = datetime.date.today().strftime("%Y%m%d")
        start_date = (datetime.date.today() - datetime.timedelta(days=int(days * 1.6))).strftime("%Y%m%d")

        def _get() -> pd.DataFrame:
            if market == "cn":
                df = call_ak(
                    ak.stock_zh_a_hist, symbol=symbol, period=period,
                    start_date=start_date, end_date=end_date, adjust="qfq",
                )
            elif market == "hk":
                df = call_ak(
                    ak.stock_hk_hist, symbol=symbol, period=period,
                    start_date=start_date, end_date=end_date, adjust="qfq",
                )
            else:
                df = call_ak(
                    ak.stock_us_hist, symbol=symbol, period=period,
                    start_date=start_date, end_date=end_date, adjust="qfq",
                )

            col_map = {
                "日期": "date", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume",
                "成交额": "turnover",
            }
            df = df.rename(columns=col_map)
            if "date" in df.columns:
                df["date"] = df["date"].astype(str)
            return df.tail(days)

        return await asyncio.to_thread(_get)

    def _calc_ma(self, df: Any, periods: list[int]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for p in periods:
            ma = df["close"].rolling(window=p).mean()
            result[f"MA{p}"] = {"current": round(float(ma.iloc[-1]), 3) if not ma.empty else None,
                                "values_last_10": [round(float(v), 3) for v in ma.tail(10).tolist() if v == v]}
        return result

    def _calc_ema(self, df: Any, periods: list[int]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for p in periods:
            ema = df["close"].ewm(span=p, adjust=False).mean()
            result[f"EMA{p}"] = {"current": round(float(ema.iloc[-1]), 3),
                                 "values_last_10": [round(float(v), 3) for v in ema.tail(10).tolist()]}
        return result

    def _calc_macd(self, df: Any) -> dict[str, Any]:
        close = df["close"]
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        macd_hist = 2 * (dif - dea)

        return {
            "DIF": round(float(dif.iloc[-1]), 4),
            "DEA": round(float(dea.iloc[-1]), 4),
            "MACD": round(float(macd_hist.iloc[-1]), 4),
            "signal": "golden_cross" if dif.iloc[-1] > dea.iloc[-1] and dif.iloc[-2] <= dea.iloc[-2]
                      else "death_cross" if dif.iloc[-1] < dea.iloc[-1] and dif.iloc[-2] >= dea.iloc[-2]
                      else "bullish" if dif.iloc[-1] > dea.iloc[-1] else "bearish",
            "DIF_last_10": [round(float(v), 4) for v in dif.tail(10).tolist()],
            "DEA_last_10": [round(float(v), 4) for v in dea.tail(10).tolist()],
            "MACD_last_10": [round(float(v), 4) for v in macd_hist.tail(10).tolist()],
        }

    def _calc_rsi(self, df: Any, periods: tuple[int, ...] = (6, 12, 24)) -> dict[str, Any]:
        result: dict[str, Any] = {}
        close = df["close"]
        delta = close.diff()
        for p in periods:
            gain = delta.clip(lower=0).rolling(window=p).mean()
            loss = (-delta.clip(upper=0)).rolling(window=p).mean()
            rs = gain / loss.replace(0, float("nan"))
            rsi = 100 - (100 / (1 + rs))
            current = float(rsi.iloc[-1]) if rsi.iloc[-1] == rsi.iloc[-1] else None
            result[f"RSI{p}"] = {
                "current": round(current, 2) if current is not None else None,
                "signal": "overbought" if current and current > 70
                          else "oversold" if current and current < 30
                          else "neutral",
            }
        return result

    def _calc_boll(self, df: Any, window: int = 20, num_std: float = 2.0) -> dict[str, Any]:
        close = df["close"]
        mid = close.rolling(window=window).mean()
        std = close.rolling(window=window).std()
        upper = mid + num_std * std
        lower = mid - num_std * std

        current_price = float(close.iloc[-1])
        upper_val = float(upper.iloc[-1])
        lower_val = float(lower.iloc[-1])
        mid_val = float(mid.iloc[-1])

        # Width percentage
        width = (upper_val - lower_val) / mid_val * 100 if mid_val else 0

        return {
            "upper": round(upper_val, 3),
            "middle": round(mid_val, 3),
            "lower": round(lower_val, 3),
            "width_pct": round(width, 2),
            "position": "above_upper" if current_price > upper_val
                        else "below_lower" if current_price < lower_val
                        else "upper_half" if current_price > mid_val
                        else "lower_half",
            "upper_last_10": [round(float(v), 3) for v in upper.tail(10).tolist() if v == v],
            "middle_last_10": [round(float(v), 3) for v in mid.tail(10).tolist() if v == v],
            "lower_last_10": [round(float(v), 3) for v in lower.tail(10).tolist() if v == v],
        }

    def _calc_kdj(self, df: Any, n: int = 9) -> dict[str, Any]:
        low_min = df["low"].rolling(window=n).min()
        high_max = df["high"].rolling(window=n).max()
        rsv = (df["close"] - low_min) / (high_max - low_min) * 100
        rsv = rsv.fillna(50)

        k = rsv.ewm(com=2, adjust=False).mean()
        d = k.ewm(com=2, adjust=False).mean()
        j = 3 * k - 2 * d

        return {
            "K": round(float(k.iloc[-1]), 2),
            "D": round(float(d.iloc[-1]), 2),
            "J": round(float(j.iloc[-1]), 2),
            "signal": "overbought" if k.iloc[-1] > 80 and d.iloc[-1] > 80
                      else "oversold" if k.iloc[-1] < 20 and d.iloc[-1] < 20
                      else "golden_cross" if k.iloc[-1] > d.iloc[-1] and k.iloc[-2] <= d.iloc[-2]
                      else "death_cross" if k.iloc[-1] < d.iloc[-1] and k.iloc[-2] >= d.iloc[-2]
                      else "neutral",
        }

    def _calc_atr(self, df: Any, period: int = 14) -> dict[str, Any]:
        import numpy as np

        high = df["high"]
        low = df["low"]
        close = df["close"]
        prev_close = close.shift(1)
        tr = np.maximum(high - low, np.maximum(abs(high - prev_close), abs(low - prev_close)))
        atr = tr.rolling(window=period).mean()

        return {
            "ATR": round(float(atr.iloc[-1]), 4),
            "ATR_pct": round(float(atr.iloc[-1]) / float(close.iloc[-1]) * 100, 2),
        }

    def _calc_obv(self, df: Any) -> dict[str, Any]:
        import numpy as np

        close = df["close"]
        volume = df["volume"]
        direction = np.sign(close.diff()).fillna(0)
        obv = (volume * direction).cumsum()

        return {
            "current": int(obv.iloc[-1]),
            "trend": "rising" if obv.iloc[-1] > obv.iloc[-5] else "falling",
            "last_10": [int(v) for v in obv.tail(10).tolist()],
        }

    def _calc_vwap(self, df: Any) -> dict[str, Any]:
        typical_price = (df["high"] + df["low"] + df["close"]) / 3
        cumulative_tp_vol = (typical_price * df["volume"]).cumsum()
        cumulative_vol = df["volume"].cumsum()
        vwap = cumulative_tp_vol / cumulative_vol

        return {
            "current": round(float(vwap.iloc[-1]), 3),
            "vs_close": "above" if vwap.iloc[-1] > df["close"].iloc[-1] else "below",
        }

    def _calc_wr(self, df: Any, periods: tuple[int, ...] = (6, 10)) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for p in periods:
            highest = df["high"].rolling(window=p).max()
            lowest = df["low"].rolling(window=p).min()
            wr = (highest - df["close"]) / (highest - lowest) * -100
            current = float(wr.iloc[-1]) if wr.iloc[-1] == wr.iloc[-1] else None
            result[f"WR{p}"] = {
                "current": round(current, 2) if current is not None else None,
                "signal": "overbought" if current and current > -20
                          else "oversold" if current and current < -80
                          else "neutral",
            }
        return result
