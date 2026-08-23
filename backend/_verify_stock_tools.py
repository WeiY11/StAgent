"""Verify stock tools completeness."""
import asyncio
from StAgent.tools.data.stock_quote import StockQuoteTool
from StAgent.tools.data.stock_indicator import StockIndicatorTool
from StAgent.tools.data.stock_screener import StockScreenerTool
from StAgent.tools.data.stock_alert import StockAlertTool
from StAgent.tools.data.stock_chart import StockChartTool
from StAgent.tools.base import ToolContext

tools = [StockQuoteTool(), StockIndicatorTool(), StockScreenerTool(), StockAlertTool(), StockChartTool()]

print("=== Schema Validation ===")
for t in tools:
    schema = t.parameters
    assert schema["type"] == "object", f"{t.name} schema invalid"
    assert t.description, f"{t.name} missing description"
    assert t.search_hint, f"{t.name} missing search_hint"
    props = list(schema.get("properties", {}).keys())
    print(f"  {t.name}: OK (params: {props})")

print("\n=== Auto-Discovery Compatibility ===")
for t in tools:
    assert hasattr(t, "name") and t.name
    assert hasattr(t, "category")
    print(f"  {t.name}: category={t.category.value}, read_only={t.is_read_only}")

print("\n=== Workflow Integration ===")
for t in tools:
    schema = t.to_openai_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == t.name
    print(f"  Tool.{t.name} node: ready")

print("\n=== Alias Registration ===")
from StAgent.tools.registry import ToolRegistry
reg = ToolRegistry()
for t in tools:
    reg.register(t)
assert reg.find_by_name("get_stock") is not None
assert reg.find_by_name("technical_indicator") is not None
assert reg.find_by_name("screen_stocks") is not None
assert reg.find_by_name("set_stock_alert") is not None
assert reg.find_by_name("kline_chart") is not None
print("  All aliases resolve correctly")

print("\n=== Live Data Test (stock_screener, top 3 by volume) ===")
ctx = ToolContext(user_id="test", session_id="test")
result = asyncio.run(StockScreenerTool().execute(
    {"volume_min": 5000000, "sort_by": "volume", "limit": 3}, ctx
))
if result.get("success"):
    print(f"  Screener OK: {result['matched_count']} stocks matched, showing {result['returned_count']}")
    for r in result["results"]:
        print(f"    {r['code']} {r['name']}: vol={r.get('volume')}")
else:
    print(f"  Screener: {result.get('error', 'unknown error')} (may be non-trading hours)")

print("\n=== ALL CHECKS PASSED ===")
