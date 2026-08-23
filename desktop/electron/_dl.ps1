$env:ELECTRON_MIRROR = 'https://registry.npmmirror.com/-/binary/electron/'
$env:ELECTRON_CUSTOM_DIR = '{{ version }}'
Set-Location 'd:\StAgent\desktop\electron'
'Downloading Electron binary via npmmirror...'
& node 'node_modules\electron\install.js'
'INSTALL_EXITCODE=' + $LASTEXITCODE
if (Test-Path 'node_modules\electron\dist\electron.exe') { 'ELECTRON_BINARY_OK' } else { 'ELECTRON_BINARY_MISSING' }
