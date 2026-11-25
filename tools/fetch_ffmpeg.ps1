# 拉取内嵌用的 ffmpeg（Windows 静态构建）
# 用法：在项目根目录执行  powershell -ExecutionPolicy Bypass -File tools\fetch_ffmpeg.ps1
#
# 说明：打包出来的 LiveRec.exe 会内嵌 bin\ffmpeg.exe（约 100MB）。
# 因为是二进制，不放进 Git 仓库，克隆后跑一次本脚本即可。

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$bin = Join-Path $root "bin"
$dest = Join-Path $bin "ffmpeg.exe"

if (Test-Path $dest) {
    $size = [math]::Round((Get-Item $dest).Length / 1MB, 1)
    Write-Host "bin\ffmpeg.exe 已存在（$size MB），跳过。" -ForegroundColor Yellow
    exit 0
}

# 1) 先找本机 PATH 里的 ffmpeg
$found = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
if ($found) {
    Write-Host "复用本机 ffmpeg：$($found.Source)" -ForegroundColor Cyan
    New-Item -ItemType Directory -Force -Path $bin | Out-Null
    Copy-Item $found.Source $dest -Force
    Write-Host "已复制到 bin\ffmpeg.exe" -ForegroundColor Green
    exit 0
}

# 2) 没有就下载 gyan.dev 的 essentials 构建
$url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
$zip = Join-Path $env:TEMP "ffmpeg-essentials.zip"
$tmp = Join-Path $env:TEMP "ffmpeg-essentials"

Write-Host "本机没有 ffmpeg，正在下载（约 30MB）..." -ForegroundColor Cyan
Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
Expand-Archive -Path $zip -DestinationPath $tmp
$exe = Get-ChildItem $tmp -Recurse -Filter ffmpeg.exe | Select-Object -First 1
if (-not $exe) { Write-Host "压缩包里没找到 ffmpeg.exe" -ForegroundColor Red; exit 1 }

New-Item -ItemType Directory -Force -Path $bin | Out-Null
Copy-Item $exe.FullName $dest -Force
Write-Host "已就绪：bin\ffmpeg.exe（$([math]::Round((Get-Item $dest).Length/1MB,1)) MB）" -ForegroundColor Green
