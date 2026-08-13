import asyncio, time
from leagent.config.settings import get_settings
from leagent.services.service_manager import ServiceManager

async def main():
    s = get_settings()
    t0 = time.perf_counter()
    sm = ServiceManager(s)
    await sm.start_all()
    t1 = time.perf_counter()
    print(f"start_all: {t1-t0:.2f}s")
    from leagent.main import _run_db_migrations, _bootstrap_tool_system
    try:
        await _run_db_migrations()
    except Exception as e:
        print("migr err", e)
    t2 = time.perf_counter()
    print(f"_run_db_migrations: {t2-t1:.2f}s")
    await _bootstrap_tool_system()
    t3 = time.perf_counter()
    print(f"_bootstrap_tool_system: {t3-t2:.2f}s")
    await sm.stop_all()
    t4 = time.perf_counter()
    print(f"stop_all: {t4-t3:.2f}s")

asyncio.run(main())
