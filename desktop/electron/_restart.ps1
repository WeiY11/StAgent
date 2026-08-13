$ErrorActionPreference = 'SilentlyContinue'

# Only target node/electron download processes, never powershell running our scripts
Get-CimInstance Win32_Process | Where-Object {
  $_.Name -in @('node.exe','electron.exe') -and $_.CommandLine -and (
    $_.CommandLine -like '*electron*install.js*' -or
    $_.CommandLine -like '*node_modules\electron*' -or
    $_.CommandLine -like '*electron .*'
  )
} | ForEach-Object {
  'KILL {0} {1} {2}' -f $_.ProcessId, $_.Name, ($_.CommandLine.Substring(0,[Math]::Min(80,$_.CommandLine.Length)))
  Stop-Process -Id $_.ProcessId -Force
}

Start-Sleep -Seconds 2
'CLEANUP DONE'
