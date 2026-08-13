$ErrorActionPreference = 'Stop'
Set-Location D:\LeAgent
# Production image build + container health proof (Step 6b).
docker build --target production -f deploy\Dockerfile -t leagent:test .
Write-Output "BUILD_EXIT=$LASTEXITCODE"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$existing = docker ps -a --filter "name=^/leagent-smoke$" --format '{{.Names}}'
if ($existing) { docker rm -f leagent-smoke | Out-Null }

docker run -d --name leagent-smoke -p 127.0.0.1:18000:8000 `
  -e LEAGENT_SECRET_KEY=test-secret-key-with-at-least-32-bytes `
  -e LEAGENT_SECURITY_ENFORCE_AUTH=false `
  leagent:test
try {
  $ready = $false
  1..30 | ForEach-Object {
    if (-not $ready) {
      try {
        Invoke-RestMethod http://127.0.0.1:18000/health | Out-Null
        Invoke-RestMethod http://127.0.0.1:18000/api/v1/health/ready | Out-Null
        $ready = $true
        Write-Output 'CONTAINER_READY'
      } catch {
        Start-Sleep -Seconds 2
      }
    }
  }
  if (-not $ready) { throw 'LeAgent container did not become ready' }
  docker exec leagent-smoke python -c "import leagent, asyncpg; print(leagent.__version__)"
} finally {
  docker logs leagent-smoke 2>&1 | Select-Object -Last 30
  docker rm -f leagent-smoke | Out-Null
}
