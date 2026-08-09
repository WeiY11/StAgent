# Temporary dev helper: run backend with auth disabled for local browser verification.
$env:LEAGENT_HOST = '127.0.0.1'
$env:LEAGENT_SECURITY_ENFORCE_AUTH = 'false'
$env:LEAGENT_SECURITY_RATE_LIMIT_ENABLED = 'false'
Set-Location $PSScriptRoot
uv run uvicorn leagent.main:app --host 127.0.0.1 --port 7860
