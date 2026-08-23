"""Quick env check for stock tools."""
import importlib
import sys

print("python:", sys.executable)
for mod in ("akshare", "pandas", "matplotlib", "mplfinance"):
    try:
        m = importlib.import_module(mod)
        print(f"{mod}: OK", getattr(m, "__version__", "?"))
    except Exception as e:
        print(f"{mod}: FAIL -> {type(e).__name__}: {e}")

# Check tool registration path
try:
    from StAgent.tools.registry import ToolRegistry
    reg = ToolRegistry()
    n = reg.discover_all(categories=["data"])
    names = [t for t in reg.list_tools()]
    stock = [t for t in names if "stock" in str(t).lower()]
    print("data tools discovered:", n)
    print("stock tools:", stock)
except Exception as e:
    print("discovery FAIL:", type(e).__name__, e)
