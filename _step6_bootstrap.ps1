$ErrorActionPreference = 'Stop'

# Pristine DB: drop and recreate the public schema so create_all + migrations
# run from truly empty state (a prior failed standalone-alembic left partial tables).
docker exec StAgent-postgres-contract psql -U StAgent -d StAgent -v ON_ERROR_STOP=1 -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
Write-Output 'SCHEMA_RESET_OK'

Set-Location D:\StAgent\backend
$env:DATABASE_URL = 'postgresql+asyncpg://StAgent:test@127.0.0.1:15432/StAgent'
uv run python _step6_pg_bootstrap.py
Write-Output "BOOTSTRAP_EXIT=$LASTEXITCODE"
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
