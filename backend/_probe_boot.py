import asyncio, time
from leagent.main import _bootstrap_tool_system

async def main():
    for i in range(3):
        t0 = time.perf_counter()
        await _bootstrap_tool_system()
        print(f"bootstrap iter{i}: {time.perf_counter()-t0:.2f}s")

asyncio.run(main())
