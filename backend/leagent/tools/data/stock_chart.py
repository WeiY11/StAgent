"""Stock Chart Tool — generate professional K-line and indicator charts.

Produces candlestick charts with overlaid technical indicators using
matplotlib. Outputs a PNG/SVG image file.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import structlog

from leagent.tools.base import BaseTool, ToolCapability, ToolCategory, ToolContext
from leagent.tools.data._stock_common import call_ak

logger = structlog.get_logger(__name__)


class StockChartTool(BaseTool):
    """Generate professional stock charts with candlestick and indicators.

    Creates K-line (candlestick) charts overlaid with technical indicators
    such as MA, MACD, RSI, Bollinger Bands, and volume bars. The output is
    a high-quality image file suitable for analysis or sharing.
    """

    name = "stock_chart"
    description = (
        "Generate professional stock charts: candlestick (K-line) with technical indicators. "
        "Overlays MA, EMA, BOLL on price panel; MACD, RSI, KDJ on sub-panels; volume bars. "
        "Provide a stock symbol and desired indicators; returns a chart image file path. "
        "Supports daily/weekly/monthly timeframes and customizable date ranges."
    )
    category = ToolCategory.DATA
    version = "1.0.0"
    timeout_sec = 90
    is_read_only = False
    is_concurrency_safe = True
    capabilities = (ToolCapability.NETWORK,)
    aliases = ["kline_chart", "candlestick_chart", "stock_visualization"]
    search_hint = (
        "stock chart candlestick K-line visualization plot 股票 图表 K线 "
        "蜡烛图 走势图 技术分析图 可视化"
    )
    output_path_params = ("output_path",)

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["symbol"],
            "additionalProperties": False,
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Stock code (e.g. '600519').",
                },
                "market": {
                    "type": "string",
                    "enum": ["cn", "hk", "us"],
                    "default": "cn",
                    "description": "Market (default: cn).",
                },
                "period": {
                    "type": "string",
                    "enum": ["daily", "weekly", "monthly"],
                    "default": "daily",
                    "description": "Chart timeframe.",
                },
                "days": {
                    "type": "integer",
                    "default": 60,
                    "description": "Number of trading days to display (default: 60).",
                },
                "indicators": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["ma", "ema", "boll", "macd", "rsi", "kdj", "volume"],
                    },
                    "default": ["ma", "volume", "macd"],
                    "description": "Indicators to overlay (default: ma + volume + macd).",
                },
                "ma_periods": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "default": [5, 10, 20, 60],
                    "description": "MA periods to show.",
                },
                "title": {
                    "type": "string",
                    "description": "Chart title (auto-generated if omitted).",
                },
                "style": {
                    "type": "string",
                    "enum": ["default", "dark", "light"],
                    "default": "default",
                    "description": "Chart color style.",
                },
                "output_path": {
                    "type": "string",
                    "description": "Output file path for the chart image. Auto-generated if omitted.",
                },
            },
        }

    def get_activity_description(self, params: dict[str, Any] | None = None) -> str | None:
        symbol = (params or {}).get("symbol", "")
        return f"Generating stock chart for {symbol}"

    async def execute(self, params: dict[str, Any], context: ToolContext) -> Any:
        symbol = str(params.get("symbol", "")).strip()
        market = params.get("market", "cn")
        period = params.get("period", "daily")
        days = params.get("days", 60)
        indicators = params.get("indicators", ["ma", "volume", "macd"])
        ma_periods = params.get("ma_periods", [5, 10, 20, 60])
        title = params.get("title", "")
        style = params.get("style", "default")
        output_path = params.get("output_path", "")

        if not symbol:
            return {"success": False, "error": "symbol is required"}

        try:
            import akshare as ak
        except ImportError:
            return {"success": False, "error": "akshare not installed"}

        try:
            # Fetch data
            df = await self._fetch_data(ak, symbol, market, period, days)
            if df.empty:
                return {"success": False, "error": f"No data for {symbol}"}

            # Determine output path
            if not output_path:
                import tempfile
                output_path = str(Path(tempfile.gettempdir()) / f"stock_{symbol}_{period}.png")

            # Generate chart in thread (matplotlib is not thread-safe but we isolate it)
            result = await asyncio.to_thread(
                self._render_chart, df, symbol, market, indicators, ma_periods, title, style, output_path
            )
            return result

        except Exception as e:
            logger.warning("stock_chart_failed", symbol=symbol, error=str(e))
            return {"success": False, "error": str(e)}

    async def _fetch_data(self, ak: Any, symbol: str, market: str, period: str, days: int) -> Any:
        """Fetch historical data for charting."""
        import datetime

        import pandas as pd  # noqa: TC002 — runtime lazy import

        # Fetch extra days to ensure we have enough after weekends/holidays
        fetch_days = int(days * 1.8)
        end_date = datetime.date.today().strftime("%Y%m%d")
        start_date = (datetime.date.today() - datetime.timedelta(days=fetch_days)).strftime("%Y%m%d")

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
                # akshare returns datetime.date objects — normalize to str
                df["date"] = df["date"].astype(str)
            return df.tail(days)

        return await asyncio.to_thread(_get)

    def _render_chart(
        self,
        df: Any,
        symbol: str,
        market: str,
        indicators: list[str],
        ma_periods: list[int],
        title: str,
        style: str,
        output_path: str,
    ) -> dict[str, Any]:
        """Render the chart using matplotlib."""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
        import pandas as pd  # noqa: TC002 — runtime lazy import

        # Style configuration
        styles = {
            "default": {
                "bg": "#FFFFFF", "grid": "#E5E5E5", "text": "#333333",
                "up": "#EF4444", "down": "#22C55E",  # Chinese convention: red=up, green=down
                "ma_colors": ["#F59E0B", "#3B82F6", "#8B5CF6", "#EC4899"],
            },
            "dark": {
                "bg": "#1A1A2E", "grid": "#333355", "text": "#E0E0E0",
                "up": "#FF6B6B", "down": "#51CF66",
                "ma_colors": ["#FFD93D", "#6BCB77", "#4D96FF", "#FF6B6B"],
            },
            "light": {
                "bg": "#FAFAFA", "grid": "#EEEEEE", "text": "#555555",
                "up": "#DC2626", "down": "#16A34A",
                "ma_colors": ["#EA580C", "#2563EB", "#7C3AED", "#DB2777"],
            },
        }
        s = styles.get(style, styles["default"])

        # Determine subplot layout
        sub_indicators = [i for i in indicators if i in ("macd", "rsi", "kdj")]
        has_volume = "volume" in indicators
        n_subplots = 1 + (1 if has_volume else 0) + len(sub_indicators)

        # Height ratios
        ratios = [4]
        if has_volume:
            ratios.append(1.2)
        ratios.extend([1.5] * len(sub_indicators))

        fig, axes = plt.subplots(
            n_subplots, 1, figsize=(14, 3 + 2.5 * n_subplots),
            gridspec_kw={"height_ratios": ratios, "hspace": 0.05},
            sharex=True,
        )
        if n_subplots == 1:
            axes = [axes]

        fig.patch.set_facecolor(s["bg"])
        for ax in axes:
            ax.set_facecolor(s["bg"])
            ax.grid(True, alpha=0.3, color=s["grid"])
            ax.tick_params(colors=s["text"])

        # Prepare data
        dates = pd.to_datetime(df["date"])
        opens = df["open"].values.astype(float)
        closes = df["close"].values.astype(float)
        highs = df["high"].values.astype(float)
        lows = df["low"].values.astype(float)
        volumes = df["volume"].values.astype(float) if "volume" in df.columns else None

        x = np.arange(len(dates))

        # --- Main price panel (candlestick) ---
        ax_price = axes[0]
        for i in range(len(x)):
            color = s["up"] if closes[i] >= opens[i] else s["down"]
            # Body
            body_bottom = min(opens[i], closes[i])
            body_height = abs(closes[i] - opens[i])
            ax_price.bar(x[i], body_height, bottom=body_bottom, width=0.6,
                         color=color, edgecolor=color, linewidth=0.5)
            # Wicks
            ax_price.vlines(x[i], lows[i], highs[i], color=color, linewidth=0.8)

        # Overlay MA/EMA/BOLL on price panel
        if "ma" in indicators:
            close_series = pd.Series(closes)
            for idx, p in enumerate(ma_periods):
                if p < len(closes):
                    ma = close_series.rolling(window=p).mean()
                    color = s["ma_colors"][idx % len(s["ma_colors"])]
                    ax_price.plot(x, ma.values, color=color, linewidth=1.2,
                                  label=f"MA{p}", alpha=0.9)

        if "ema" in indicators:
            close_series = pd.Series(closes)
            for idx, p in enumerate(ma_periods):
                if p < len(closes):
                    ema = close_series.ewm(span=p, adjust=False).mean()
                    color = s["ma_colors"][idx % len(s["ma_colors"])]
                    ax_price.plot(x, ema.values, color=color, linewidth=1.2,
                                  label=f"EMA{p}", linestyle="--", alpha=0.8)

        if "boll" in indicators:
            close_series = pd.Series(closes)
            mid = close_series.rolling(window=20).mean()
            std = close_series.rolling(window=20).std()
            upper = mid + 2 * std
            lower = mid - 2 * std
            ax_price.plot(x, upper.values, color="#9333EA", linewidth=1, alpha=0.7, label="BOLL Upper")
            ax_price.plot(x, mid.values, color="#9333EA", linewidth=1, alpha=0.5, linestyle="--")
            ax_price.plot(x, lower.values, color="#9333EA", linewidth=1, alpha=0.7, label="BOLL Lower")
            ax_price.fill_between(x, upper.values, lower.values, alpha=0.05, color="#9333EA")

        chart_title = title or f"{symbol} {'A-Share' if market == 'cn' else market.upper()} Chart"
        ax_price.set_title(chart_title, fontsize=14, color=s["text"], pad=10)
        ax_price.legend(loc="upper left", fontsize=8, framealpha=0.7)
        ax_price.set_ylabel("Price", color=s["text"])

        current_ax = 1

        # --- Volume panel ---
        if has_volume and volumes is not None:
            ax_vol = axes[current_ax]
            colors = [s["up"] if closes[i] >= opens[i] else s["down"] for i in range(len(x))]
            ax_vol.bar(x, volumes, color=colors, alpha=0.7, width=0.6)
            ax_vol.set_ylabel("Volume", color=s["text"], fontsize=9)
            current_ax += 1

        # --- MACD panel ---
        if "macd" in sub_indicators:
            ax_macd = axes[current_ax]
            close_series = pd.Series(closes)
            ema12 = close_series.ewm(span=12, adjust=False).mean()
            ema26 = close_series.ewm(span=26, adjust=False).mean()
            dif = ema12 - ema26
            dea = dif.ewm(span=9, adjust=False).mean()
            macd_hist = 2 * (dif - dea)

            ax_macd.plot(x, dif.values, color="#3B82F6", linewidth=1.2, label="DIF")
            ax_macd.plot(x, dea.values, color="#F59E0B", linewidth=1.2, label="DEA")
            macd_colors = [s["up"] if v >= 0 else s["down"] for v in macd_hist.values]
            ax_macd.bar(x, macd_hist.values, color=macd_colors, alpha=0.7, width=0.6)
            ax_macd.axhline(0, color=s["grid"], linewidth=0.5)
            ax_macd.set_ylabel("MACD", color=s["text"], fontsize=9)
            ax_macd.legend(loc="upper left", fontsize=8, framealpha=0.7)
            current_ax += 1

        # --- RSI panel ---
        if "rsi" in sub_indicators:
            ax_rsi = axes[current_ax]
            close_series = pd.Series(closes)
            delta = close_series.diff()
            gain = delta.clip(lower=0).rolling(window=14).mean()
            loss = (-delta.clip(upper=0)).rolling(window=14).mean()
            rs = gain / loss.replace(0, float("nan"))
            rsi = 100 - (100 / (1 + rs))

            ax_rsi.plot(x, rsi.values, color="#8B5CF6", linewidth=1.2, label="RSI(14)")
            ax_rsi.axhline(70, color=s["up"], linewidth=0.8, linestyle="--", alpha=0.6)
            ax_rsi.axhline(30, color=s["down"], linewidth=0.8, linestyle="--", alpha=0.6)
            ax_rsi.fill_between(x, 30, 70, alpha=0.05, color="#8B5CF6")
            ax_rsi.set_ylim(0, 100)
            ax_rsi.set_ylabel("RSI", color=s["text"], fontsize=9)
            ax_rsi.legend(loc="upper left", fontsize=8, framealpha=0.7)
            current_ax += 1

        # --- KDJ panel ---
        if "kdj" in sub_indicators:
            ax_kdj = axes[current_ax]
            low_series = pd.Series(lows)
            high_series = pd.Series(highs)
            close_series = pd.Series(closes)
            low_min = low_series.rolling(window=9).min()
            high_max = high_series.rolling(window=9).max()
            rsv = (close_series - low_min) / (high_max - low_min) * 100
            rsv = rsv.fillna(50)
            k = rsv.ewm(com=2, adjust=False).mean()
            d = k.ewm(com=2, adjust=False).mean()
            j = 3 * k - 2 * d

            ax_kdj.plot(x, k.values, color="#3B82F6", linewidth=1.2, label="K")
            ax_kdj.plot(x, d.values, color="#F59E0B", linewidth=1.2, label="D")
            ax_kdj.plot(x, j.values, color="#8B5CF6", linewidth=1, label="J", alpha=0.7)
            ax_kdj.axhline(80, color=s["up"], linewidth=0.8, linestyle="--", alpha=0.5)
            ax_kdj.axhline(20, color=s["down"], linewidth=0.8, linestyle="--", alpha=0.5)
            ax_kdj.set_ylabel("KDJ", color=s["text"], fontsize=9)
            ax_kdj.legend(loc="upper left", fontsize=8, framealpha=0.7)
            current_ax += 1

        # X-axis date labels on bottom panel
        tick_positions = np.linspace(0, len(x) - 1, min(8, len(x))).astype(int)
        axes[-1].set_xticks(tick_positions)
        axes[-1].set_xticklabels(
            [dates.iloc[i].strftime("%m-%d") for i in tick_positions],
            rotation=30, fontsize=8, color=s["text"],
        )

        plt.tight_layout()
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output_path, dpi=150, bbox_inches="tight", facecolor=s["bg"])
        plt.close(fig)

        file_size = Path(output_path).stat().st_size
        logger.info("stock_chart_generated", symbol=symbol, output=output_path, size=file_size)

        return {
            "success": True,
            "output_path": output_path,
            "file_size_bytes": file_size,
            "symbol": symbol,
            "period": f"{df['date'].iloc[0]} ~ {df['date'].iloc[-1]}" if "date" in df.columns else "",
            "data_points": len(df),
            "indicators": indicators,
        }
