# Clean up leftover StAgent / Playwright-download processes without touching IDE node procs.
$killed = @()

# 1) Playwright download node processes (identified by command line)
Get-CimInstance Win32_Process -Filter "Name='node.exe'" | ForEach-Object {
    $cl = $_.CommandLine
    if ($cl -and ($cl -match 'oopDownloadBrowser' -or $cl -match 'playwright')) {
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $killed += "node $($_.ProcessId)" } catch {}
    }
}

# 2) uv.exe and StAgent python (command line contains StAgent / alembic / uvicorn / playwright)
foreach ($name in @('uv.exe','python.exe')) {
    Get-CimInstance Win32_Process -Filter "Name='$name'" | ForEach-Object {
        $cl = $_.CommandLine
        if ($cl -and ($cl -match 'StAgent' -or $cl -match 'alembic' -or $cl -match 'playwright' -or $cl -match 'uvicorn' -or $cl -match 'gunicorn')) {
            try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $killed += "$name $($_.ProcessId)" } catch {}
        }
    }
}

Write-Host ("Killed: " + ($killed -join ', '))
