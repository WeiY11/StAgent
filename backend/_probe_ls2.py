import asyncio, time
from StAgent.main import create_app, lifespan
app = create_app()
async def main():
    for i in range(2):
        t0 = time.perf_counter()
        async with lifespan(app):
            t1 = time.perf_counter()
            print(f"lifespan iter{i} startup: {t1-t0:.2f}s")
        print(f"lifespan iter{i} shutdown: {time.perf_counter()-t1:.2f}s")
asyncio.run(main())
