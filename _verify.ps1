Write-Host '=== startup markers in monolith.log ==='
Select-String -Path .\logs\monolith.log -Pattern 'Uvicorn running|Application startup complete|Started server process|ERROR|Traceback|Address already in use' -ErrorAction SilentlyContinue |
    Select-Object -Last 15 | ForEach-Object { Write-Host $_.Line }

Write-Host ''
Write-Host '=== retry /health x3 ==='
for ($i = 1; $i -le 3; $i++) {
    try {
        $r = Invoke-WebRequest -Uri 'http://127.0.0.1:7860/health' -UseBasicParsing -TimeoutSec 30
        Write-Host ("  try#$i status=" + $r.StatusCode + " body=" + $r.Content)
        break
    } catch {
        Write-Host ("  try#$i error: " + $_.Exception.Message)
        Start-Sleep -Seconds 3
    }
}
