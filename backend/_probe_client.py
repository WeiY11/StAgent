import time
from fastapi.testclient import TestClient
from leagent.main import create_app, mount_frontend_spa_if_configured
from leagent.config.settings import get_settings
get_settings()
app = create_app()
mount_frontend_spa_if_configured(app)
for i in range(3):
    t0 = time.perf_counter()
    with TestClient(app, raise_server_exceptions=False) as c:
        t1 = time.perf_counter()
        r = c.get("/api/v1/coding-projects/templates")
        t2 = time.perf_counter()
    t3 = time.perf_counter()
    print(f"iter{i}: enter(lifespan startup)={t1-t0:.2f}s  request={t2-t1:.2f}s  exit(shutdown)={t3-t2:.2f}s  status={r.status_code}")
