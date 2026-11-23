# Загрузка проекта на сервер по SSH (из Windows, нужны встроенные tar, ssh и scp).
#
#   .\upload-server.ps1 -Server ubuntu@203.0.113.7 -Key C:\путь\ssh-key.key
#   .\upload-server.ps1 -Server ubuntu@203.0.113.7 -Key ... -WithLibrary   # + уже скачанная музыка и история
param(
    [Parameter(Mandatory)][string]$Server,
    [string]$Key,
    [switch]$WithLibrary
)
# ssh/scp пишут предупреждения в stderr; в Windows PowerShell 5.1 это не должно прерывать скрипт,
# поэтому результат проверяем по коду возврата
$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
$ssh = @(); if ($Key) { $ssh = @("-i", $Key) }
$opts = @("-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR")
function Assert-Ok($what) { if ($LASTEXITCODE -ne 0) { throw "Не удалось: $what (код $LASTEXITCODE)" } }

if (-not (Test-Path (Join-Path $root "server.env"))) {
    Write-Host "Нет файла server.env (настройки сервера). Создайте его по .env.server.example." -ForegroundColor Yellow; exit 1
}

# Docker на сервере ставится в фоне при первом запуске (AWS user-data): дождёмся
$waited = 0
while ($waited -lt 300) {
    ssh @ssh @opts $Server "test -f ~/.hub-ready" 2>$null
    if ($LASTEXITCODE -eq 0) { break }
    Write-Host "Жду, пока сервер установит Docker…"; Start-Sleep 15; $waited += 15
}

$tgz = Join-Path $env:TEMP "player-deploy.tgz"
tar -czf $tgz -C $root --exclude=.venv --exclude=__pycache__ hub docker-compose.server.yml; Assert-Ok "упаковка проекта"
scp @ssh @opts $tgz "${Server}:player-deploy.tgz"; Assert-Ok "загрузка архива"
ssh @ssh @opts $Server "mkdir -p ~/player/data/hub && tar -xzf ~/player-deploy.tgz -C ~/player && rm ~/player-deploy.tgz"; Assert-Ok "распаковка на сервере"
scp @ssh @opts (Join-Path $root "server.env") "${Server}:player/.env"; Assert-Ok "загрузка server.env"
ssh @ssh @opts $Server "chmod 600 ~/player/.env"
Remove-Item $tgz

if ($WithLibrary) {
    $py = Join-Path $root "hub\.venv\Scripts\python.exe"
    $db = Join-Path $root "data\hub\hub.sqlite3"
    $copy = Join-Path $env:TEMP "hub-copy.sqlite3"
    # консистентная копия базы, даже если Hub сейчас работает
    & $py -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close()" $db $copy
    scp @ssh @opts $copy "${Server}:player/data/hub/hub.sqlite3"
    Remove-Item $copy
    scp @ssh @opts -r (Join-Path $root "music") "${Server}:player/"
}
Write-Host "`nГотово. На сервере выполните:" -ForegroundColor Green
Write-Host "  cd ~/player && docker compose -f docker-compose.server.yml up -d --build"
