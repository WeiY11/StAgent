try {
  $h = Invoke-WebRequest -UseBasicParsing http://localhost:7860/health -TimeoutSec 5
  'HEALTH ' + $h.StatusCode + ' ' + $h.Content
} catch {
  'backend-not-ready: ' + $_.Exception.Message
}
$ecount = (Get-Process electron -ErrorAction SilentlyContinue | Measure-Object).Count
'ELECTRON_PROCESSES=' + $ecount
