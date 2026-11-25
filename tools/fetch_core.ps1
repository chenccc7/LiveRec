# 拉取上游核心代码（DouyinLiveRecorder，MIT）
# 用法：在项目根目录执行  powershell -ExecutionPolicy Bypass -File tools\fetch_core.ps1
param(
    [string]$Ref = "main"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$target = Join-Path $root "core"

if (Test-Path (Join-Path $target "main.py")) {
    Write-Host "core/ 已存在，跳过。要重新拉取请先删除 core/ 目录。" -ForegroundColor Yellow
    exit 0
}

Write-Host "正在拉取上游代码到 core/ ..." -ForegroundColor Cyan
# schannel 吊销检查在部分 Windows 上会报 CRYPT_E_NO_REVOCATION_CHECK，这里关掉
git -c http.schannelCheckRevoke=false clone --depth 1 --branch $Ref `
    https://github.com/ihmily/DouyinLiveRecorder.git $target

if (Test-Path (Join-Path $target "main.py")) {
    Write-Host "core/ 拉取完成。" -ForegroundColor Green
    Write-Host "提示：抖音/B站/虎牙/斗鱼的解析都走 liverec 内置实现，core/ 只在少数兜底路径用到。" -ForegroundColor DarkGray
} else {
    Write-Host "拉取失败，请检查网络或代理。" -ForegroundColor Red
    exit 1
}
