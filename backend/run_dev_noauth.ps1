# Temporary dev helper: run backend with auth disabled for local browser verification.
$env:StAgent_HOST = '127.0.0.1'
$env:StAgent_SECURITY_ENFORCE_AUTH = 'false'
$env:StAgent_SECURITY_RATE_LIMIT_ENABLED = 'false'
Set-Location $PSScriptRoot
uv run uvicorn StAgent.main:app --host 127.0.0.1 --port 7860
