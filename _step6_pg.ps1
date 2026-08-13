$ErrorActionPreference = 'Stop'
$PostgresContainer = 'leagent-postgres-contract'
$existing = docker ps -a --filter "name=^/$PostgresContainer$" --format '{{.Names}}'
if ($existing) { docker rm -f $PostgresContainer | Out-Null }

docker run -d --name $PostgresContainer `
  -e POSTGRES_USER=leagent `
  -e POSTGRES_PASSWORD=test `
  -e POSTGRES_DB=leagent `
  -p 127.0.0.1:15432:5432 `
  --health-cmd 'pg_isready -U leagent' `
  --health-interval 2s `
  --health-timeout 2s `
  --health-retries 30 `
  postgres:17-alpine

$postgresReady = $false
1..40 | ForEach-Object {
  if (-not $postgresReady) {
    $status = docker inspect --format '{{.State.Health.Status}}' $PostgresContainer 2>$null
    if ($status -eq 'healthy') { $postgresReady = $true } else { Start-Sleep -Seconds 1 }
  }
}
if (-not $postgresReady) { throw 'PostgreSQL container did not become ready' }
Write-Output 'POSTGRES_HEALTHY'
