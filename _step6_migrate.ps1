$ErrorActionPreference = 'Stop'
Set-Location D:\LeAgent\backend
$env:DATABASE_URL = 'postgresql+asyncpg://leagent:test@127.0.0.1:15432/leagent'
Write-Output '=== ALEMBIC UPGRADE HEAD ==='
uv run alembic upgrade head
Write-Output '=== PYTEST POSTGRES SMOKE ==='
uv run pytest tests/integration/test_postgres_smoke.py -q -p no:cacheprovider
Write-Output "SMOKE_EXIT=$LASTEXITCODE"
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
