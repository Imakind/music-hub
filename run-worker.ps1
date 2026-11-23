# Домашний воркер: качает треки по заданиям сервера и загружает их на сервер.
# Работает, пока запущен этот скрипт и включён компьютер.
#
# В .env нужны:  SERVER_URL=https://ваш-адрес   и   WORKER_TOKEN=тот же, что на сервере
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$hub = Join-Path $root "hub"
$py = Join-Path $hub ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Host "Сначала выполните .\run-native.ps1, чтобы создать окружение." -ForegroundColor Yellow; exit 1 }

$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$' -and $_ -notmatch '^\s*#') {
            Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2].Trim()
        }
    }
}
if (-not $env:SERVER_URL -or -not $env:WORKER_TOKEN) {
    Write-Host "Задайте SERVER_URL и WORKER_TOKEN в .env" -ForegroundColor Yellow; exit 1
}
# Свежий PATH из реестра: чтобы находился только что установленный Deno (JS для проверок YouTube)
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
if (-not (Get-Command deno -ErrorAction SilentlyContinue)) {
    Write-Host "Совет: установите Deno (winget install DenoLand.Deno) — с ним YouTube реже отвечает 403." -ForegroundColor Yellow
}
# Воркер всегда работает в обычном режиме; серверный режим только у сервера
Remove-Item Env:HUB_MODE -ErrorAction SilentlyContinue
# Плеер заранее просит ~5 следующих треков: качаем по 3 одновременно, берём до 6 задач
if (-not $env:MAX_DOWNLOADS) { $env:MAX_DOWNLOADS = "3" }
if (-not $env:WORKER_CAPACITY) { $env:WORKER_CAPACITY = "6" }

& $py -m pip install -q -U yt-dlp ytmusicapi   # YouTube часто ломает старые версии

$env:MUSIC_DIR = Join-Path $root "data\worker-music"
$env:DATA_DIR = Join-Path $root "data\worker"
Set-Location $hub
& $py -u -m app.worker
