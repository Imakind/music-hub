# Публичный адрес для Hub через Cloudflare Tunnel.
#
# Быстрый режим (без аккаунта): временный адрес вида https://xxxx.trycloudflare.com,
# меняется при каждом запуске. Для постоянного адреса см. README, раздел «Удалённый доступ».
#
# Требования: cloudflared  (winget install Cloudflare.cloudflared)
#             HUB_PASSWORD в .env — без пароля скрипт откажется запускаться.
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

$pass = $env:HUB_PASSWORD
$envFile = Join-Path $root ".env"
if (-not $pass -and (Test-Path $envFile)) {
    $line = Get-Content $envFile | Where-Object { $_ -match '^\s*HUB_PASSWORD\s*=\s*(.+)$' } | Select-Object -First 1
    if ($line) { $pass = ($line -split '=', 2)[1].Trim() }
}
if (-not $pass -or $pass.Length -lt 8) {
    Write-Host "Задайте HUB_PASSWORD (не короче 8 символов) в файле .env и перезапустите Hub." -ForegroundColor Yellow
    exit 1
}
if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
    Write-Host "cloudflared не найден. Установите: winget install Cloudflare.cloudflared" -ForegroundColor Yellow
    exit 1
}
try { Invoke-WebRequest -UseBasicParsing http://localhost:8080/ -TimeoutSec 5 | Out-Null }
catch {
    # 401 — это нормально: значит, пароль включён
    if ($_.Exception.Response.StatusCode.value__ -ne 401) {
        Write-Host "Hub не отвечает на localhost:8080. Сначала запустите его (run-native.ps1 или docker compose up)." -ForegroundColor Yellow
        exit 1
    }
}
cloudflared tunnel --url http://localhost:8080 --no-autoupdate
