"""Offline test: verify stock_chart date-type fix with synthetic data."""
import asyncio
import datetime
import sys

import numpy as np
import pandas as pd


async def main() -> None:
    from StAgent.tools.data.stock_chart import StockChartTool

    tool = StockChartTool()

    # Synthetic OHLCV frame mimicking akshare output (date as datetime.date,
    # the exact shape that triggered the original TypeError).
    n = 60
    base = datetime.date(2026, 4, 1)
    rng = np.random.default_rng(42)
    closes = 1500 + np.cumsum(rng.normal(0, 12, n))
    df = pd.DataFrame({
        "date": [base + datetime.timedelta(days=i) for i in range(n)],
        "open": closes + rng.normal(0, 5, n),
        "close": closes,
        "high": closes + abs(rng.normal(6, 4, n)),
        "low": closes - abs(rng.normal(6, 4, n)),
        "volume": abs(rng.normal(3e6, 6e5, n)),
    })
    # Apply the same normalization the fixed _fetch_data now performs
    df["date"] = df["date"].astype(str)

    result = tool._render_chart(
        df, "600519", "cn",
        ["ma", "volume", "macd", "rsi", "kdj", "boll"],
        [5, 10, 20], "", "default",
        "d:/StAgent/backend/_test_chart_output.png",
    )
    print("render result:", {k: result.get(k) for k in ("success", "output_path", "file_size_bytes", "period", "data_points")})

    # Also verify the previously-crashing branch: date column as datetime.date
    df2 = df.copy()
    df2["date"] = pd.to_datetime(df2["date"]).dt.date
    result2 = tool._render_chart(
        df2, "600519", "cn", ["ma", "volume"], [5, 10], "", "dark",
        "d:/StAgent/backend/_test_chart_output2.png",
    )
    print("render result (date objects):", {k: result2.get(k) for k in ("success", "period")})


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
