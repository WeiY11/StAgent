$ErrorActionPreference = 'Stop'
Set-Location D:\StAgent\backend
$env:DATABASE_URL = 'postgresql+asyncpg://StAgent:test@127.0.0.1:15432/StAgent'
try {
    uv run pytest tests/integration/test_postgres_smoke.py -q
    Write-Output "SMOKE_EXIT=$LASTEXITCODE"
} finally {
    Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
}
