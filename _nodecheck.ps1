$NodePath = 'C:\Program Files\nodejs\node.exe'
$expr = "const [M,m]=process.versions.node.split('.').map(Number); process.stdout.write(String(Number((M === 20 && m >= 19) || (M === 22 && m >= 12) || M > 22)))"
$out = & $NodePath -e $expr 2>$null
Write-Host "EXIT=$LASTEXITCODE OUT=[$out]"
