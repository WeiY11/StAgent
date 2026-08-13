$env:LEAGENT_SKIP_PLAYWRIGHT_INSTALL = '1'
& (Join-Path $PSScriptRoot 'start.ps1') all -Quiet
