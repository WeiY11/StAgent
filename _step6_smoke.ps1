$ErrorActionPreference = 'Stop'
Set-Location D:\LeAgent\backend
$env:DATABASE_URL = 'postgresql+asyncpg://leagent:test@127.0.0.1:15432/leagent'
try {
    uv run pytest tests/integration/test_postgres_smoke.py -q
    Write-Output "SMOKE_EXIT=$LASTEXITCODE"
} finally {
    Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
}
