$ErrorActionPreference = 'Stop'
Set-Location D:\StAgent\backend
$env:DATABASE_URL = 'postgresql+asyncpg://StAgent:test@127.0.0.1:15432/StAgent'
Write-Output '=== ALEMBIC UPGRADE HEAD ==='
uv run alembic upgrade head
Write-Output '=== PYTEST POSTGRES SMOKE ==='
uv run pytest tests/integration/test_postgres_smoke.py -q -p no:cacheprovider
Write-Output "SMOKE_EXIT=$LASTEXITCODE"
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
