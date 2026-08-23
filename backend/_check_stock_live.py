"""Live test: stock tools end-to-end."""
import asyncio
import sys
import traceback


async def main() -> None:
    from StAgent.tools.data.stock_quote import StockQuoteTool
    from StAgent.tools.data.stock_chart import StockChartTool

    class Ctx:  # minimal ToolContext stand-in
        user_id = "test"
        session_id = "test"
        file_service = None

    ctx = Ctx()

    # 1. realtime quote
    q = StockQuoteTool()
    try:
        r = await asyncio.wait_for(
            q.execute({"symbol": "600519", "period": "realtime"}, ctx), timeout=60
        )
        print("[realtime]", {k: r.get(k) for k in ("success", "error", "name", "current_price")})
    except Exception:
        print("[realtime] EXC:")
        traceback.print_exc()

    # 2. historical
    try:
        r = await asyncio.wait_for(
            q.execute({"symbol": "600519", "period": "daily"}, ctx), timeout=60
        )
        print("[daily]", {k: r.get(k) for k in ("success", "error", "count")})
        if r.get("records"):
            print("[daily] first record keys:", list(r["records"][0].keys()))
    except Exception:
        print("[daily] EXC:")
        traceback.print_exc()

    # 3. chart
    c = StockChartTool()
    try:
        r = await asyncio.wait_for(
            c.execute({"symbol": "600519", "days": 30}, ctx), timeout=120
        )
        print("[chart]", {k: r.get(k) for k in ("success", "error", "output_path", "file_size_bytes")})
    except Exception:
        print("[chart] EXC:")
        traceback.print_exc()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
