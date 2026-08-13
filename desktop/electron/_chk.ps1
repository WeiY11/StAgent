$c = Join-Path $env:LOCALAPPDATA 'electron\Cache'
if (Test-Path $c) {
  Get-ChildItem $c -Recurse -File | ForEach-Object {
    '{0}  {1} MB  {2}' -f $_.Name, [math]::Round($_.Length/1MB,1), $_.LastWriteTime
  }
} else {
  'no electron cache dir yet'
}
$d = Join-Path $env:LOCALAPPDATA 'electron\Cache'
Get-ChildItem (Join-Path $env:LOCALAPPDATA 'electron') -Recurse -File -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -like '*.tmp' -or $_.Name -like '*download*' } |
  ForEach-Object { 'PARTIAL: {0}  {1} MB' -f $_.FullName, [math]::Round($_.Length/1MB,1) }
