# 重新打包 spectrum.exe
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $root
try {
    python -m PyInstaller --clean spectrum.spec
    Write-Host "完成: $root\dist\spectrum.exe"
} finally {
    Pop-Location
}
