import os
import shutil
from pathlib import Path

MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", "/music"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
WEB_DIR = Path(os.environ.get("WEB_DIR", Path(__file__).resolve().parent.parent / "web"))
DB_PATH = DATA_DIR / "hub.sqlite3"

# Одновременных скачиваний
MAX_DOWNLOADS = int(os.environ.get("MAX_DOWNLOADS", "2"))
# Формат хранения: m4a (без перекодирования) или mp3
AUDIO_FORMAT = os.environ.get("AUDIO_FORMAT", "m4a")

# local — всё делает этот процесс; server — работает на сервере, а скачивание и
# анализ звука делает домашний воркер (app/worker.py), забирая задачи по токену.
HUB_MODE = os.environ.get("HUB_MODE", "local")
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")

# Необязательно: учётка Navidrome, чтобы сразу запускать пересканирование
NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "")
NAVIDROME_USER = os.environ.get("NAVIDROME_USER", "")
NAVIDROME_PASSWORD = os.environ.get("NAVIDROME_PASSWORD", "")


def _ffmpeg() -> str | None:
    if os.environ.get("FFMPEG_PATH"):
        return os.environ["FFMPEG_PATH"]
    if shutil.which("ffmpeg"):
        return None  # yt-dlp найдёт сам
    try:  # нативный запуск без системного ffmpeg
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG_LOCATION = _ffmpeg()
# yt-dlp нужен JS-рантайм для YouTube: deno (по умолчанию) или node
JS_RUNTIMES = None if shutil.which("deno") else ({"node": {}} if shutil.which("node") else None)

MUSIC_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
