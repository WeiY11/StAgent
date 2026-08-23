import asyncio, time
from StAgent.config.settings import get_settings
from StAgent.services.service_manager import ServiceManager

async def main():
    s = get_settings()
    sm = ServiceManager(s)
    steps = ["_start_database","_start_event","_start_cache","_start_chat","_start_canvas","_start_coding_projects","_start_variable","_start_llm","_start_file_service","_start_session_manager","_start_agent_memory","_start_rules","_start_workflow_service","_start_cron","_start_task_manager","_start_file_processing","_start_channels"]
    from StAgent.runtime.deployment_topology import validate_deployment_topology
    validate_deployment_topology(database_backend=s.database.backend, workers=s.settings.workers if hasattr(s,'settings') else s.workers)
    for name in steps:
        fn = getattr(sm, name)
        t0 = time.perf_counter()
        await fn()
        dt = time.perf_counter()-t0
        if dt > 0.15:
            print(f"{name}: {dt:.2f}s")
    print("done")

asyncio.run(main())
