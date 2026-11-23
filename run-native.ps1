# Запуск Hub без Docker (Windows). Navidrome при этом не запускается —
# веб-плеер Hub работает и без него.
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$hub = Join-Path $root "hub"
$py = Join-Path $hub ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    python -m venv (Join-Path $hub ".venv")
    & $py -m pip install -r (Join-Path $hub "requirements.txt") imageio-ffmpeg
    # Эмбеддер: PyTorch с CUDA (для NVIDIA) + transformers. Без них рекомендации
    # работают только на сигналах каталога.
    & $py -m pip install torch --index-url https://download.pytorch.org/whl/cu128
    & $py -m pip install transformers
}
# yt-dlp и ytmusicapi обновляем при каждом запуске — YouTube часто ломает старые версии
& $py -m pip install -q -U yt-dlp ytmusicapi

# Переменные из .env (HUB_PASSWORD, MIX_HOUR и т.д.)
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$' -and $_ -notmatch '^\s*#') {
            Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2].Trim()
        }
    }
}
$env:MUSIC_DIR = Join-Path $root "music"
$env:DATA_DIR = Join-Path $root "data\hub"
Set-Location $hub
& $py -m uvicorn app.main:app --host 0.0.0.0 --port 8080
