# 解压 cava-master.zip 到 src/
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$zip = Join-Path $root 'cava-master.zip'
$src = Join-Path $root 'src'

if (!(Test-Path $zip)) {
    throw "找不到 $zip"
}

if (Test-Path $src) {
    Write-Host "src/ 已存在，跳过解压。"
    exit 0
}

Write-Host "正在解压 $zip ..."
Expand-Archive -Path $zip -DestinationPath $root -Force

$extracted = Join-Path $root 'cava-master'
if (Test-Path $extracted) {
    Rename-Item -Path $extracted -NewName 'src'
    Write-Host "完成：$src"
} else {
    Write-Host "解压完成，但未找到 cava-master 目录，请手动检查。"
}
