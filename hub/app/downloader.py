"""Очередь: поиск источника → скачивание → теги → файл в библиотеке Navidrome."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import shutil
from pathlib import Path

import httpx
import yt_dlp
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TRCK, TPOS, TDRC, APIC, TXXX, ID3NoHeaderError

from . import library, sources, workerstate
from .config import AUDIO_FORMAT, DATA_DIR, FFMPEG_LOCATION, HUB_MODE, JS_RUNTIMES, MAX_DOWNLOADS, MUSIC_DIR, NAVIDROME_URL, NAVIDROME_USER, NAVIDROME_PASSWORD

log = logging.getLogger("hub.downloader")
TMP = DATA_DIR / "tmp"
TMP.mkdir(exist_ok=True)

_dl_sem = asyncio.Semaphore(MAX_DOWNLOADS)
_resolve_sem = asyncio.Semaphore(3)
_tasks: dict[int, asyncio.Task] = {}
_resolving: dict[int, asyncio.Task] = {}


def safe(name: str, limit: int = 80) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(" .")
    return (name or "Unknown")[:limit]


def target_path(meta: dict, ext: str) -> Path:
    artist = safe(meta["artist"]["name"])
    album = safe(meta["album"]["title"] or "Singles")
    pos = meta.get("track_position") or 0
    prefix = f"{pos:02d} - " if pos else ""
    return MUSIC_DIR / artist / album / f"{prefix}{safe(meta['title'], 120)}.{ext}"


# ---------- поиск источника ----------

async def resolve(track_id: int, meta: dict, force: bool = False) -> list[dict]:
    row = library.get(track_id)
    if row and row["candidates"] and not force:
        return row["candidates"]
    if HUB_MODE == "server":
        # на сервере YouTube не трогаем: просим домашнего воркера и ждём его ответ
        if row is None:
            library.upsert(track_id, meta, status="resolved")
        job = library.add_job("resolve", track_id, meta)
        for _ in range(25 if workerstate.online() else 0):
            await asyncio.sleep(1)
            j = library.get_job(job)
            if j and j["state"] in ("done", "error"):
                break
        row = library.get(track_id)
        return (row and row["candidates"]) or []
    if track_id in _resolving:
        return await _resolving[track_id]

    async def run():
        async with _resolve_sem:
            cands = await asyncio.to_thread(sources.find_candidates, meta)
        data = [c.to_dict() for c in cands[:12]]
        r = library.get(track_id)
        if r is None:
            library.upsert(track_id, meta, status="resolved", candidates=data)
        else:
            library.upsert(track_id, candidates=data)
        return data

    task = asyncio.create_task(run())
    _resolving[track_id] = task
    try:
        return await task
    finally:
        _resolving.pop(track_id, None)


# ---------- скачивание ----------

def enqueue(track_id: int, meta: dict, video_id: str | None = None) -> str:
    """Поставить трек в очередь. Возвращает текущий статус."""
    row = library.get(track_id)
    if row and row["status"] == "ready" and not video_id and library.local_path(row):
        return "ready"
    if HUB_MODE == "server":
        library.upsert(track_id, meta, status="queued", error=None)
        library.add_job("fetch", track_id, meta, video_id)
        return "queued"
    if track_id in _tasks and not _tasks[track_id].done():
        return row["status"] if row else "queued"
    library.upsert(track_id, meta, status="queued", error=None)
    _tasks[track_id] = asyncio.create_task(_download(track_id, meta, video_id))
    return "queued"


async def _download(track_id: int, meta: dict, video_id: str | None):
    try:
        library.upsert(track_id, status="resolving")
        cands = await resolve(track_id, meta)
        if video_id:
            chosen = next((c for c in cands if c["video_id"] == video_id), None) or {
                "video_id": video_id, "title": video_id, "score": 0,
            }
        else:
            if not cands:
                raise RuntimeError("источник не найден")
            chosen = cands[0]
        async with _dl_sem:
            library.upsert(track_id, status="downloading", source_id=chosen["video_id"],
                           source_title=chosen["title"], source_score=chosen.get("score"))
            path = await asyncio.to_thread(_fetch_and_tag, meta, chosen["video_id"])
        old = library.get(track_id)
        if old and old["path"] and old["path"] != str(path):
            Path(old["path"]).unlink(missing_ok=True)
        library.upsert(track_id, status="ready", path=str(path), error=None)
        log.info("ready %s — %s → %s", meta["artist"]["name"], meta["title"], path)
        asyncio.create_task(trigger_scan())
    except Exception as e:  # noqa: BLE001
        log.exception("download failed %s", track_id)
        library.upsert(track_id, status="error", error=str(e)[:500])


def _fetch_and_tag(meta: dict, video_id: str) -> Path:
    stem = TMP / secrets.token_hex(8)
    codec = "mp3" if AUDIO_FORMAT == "mp3" else "m4a"
    opts = {
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(stem) + ".%(ext)s",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": codec,
                            "preferredquality": "320" if codec == "mp3" else "0"}],
    }
    if FFMPEG_LOCATION:
        opts["ffmpeg_location"] = FFMPEG_LOCATION
    if JS_RUNTIMES:
        opts["js_runtimes"] = JS_RUNTIMES
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
    src = Path(f"{stem}.{codec}")
    if not src.exists():
        raise RuntimeError("yt-dlp не создал файл")

    cover = None
    if meta["album"].get("cover"):
        try:
            cover = httpx.get(meta["album"]["cover"], timeout=15).content
        except Exception:
            pass
    (_tag_mp3 if codec == "mp3" else _tag_m4a)(src, meta, video_id, cover)

    dst = target_path(meta, codec)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), dst)
    return dst


def _tag_m4a(p: Path, meta: dict, video_id: str, cover: bytes | None):
    a = MP4(p)
    a["\xa9nam"] = meta["title"]
    a["\xa9ART"] = meta["artist"]["name"]
    a["aART"] = meta.get("album_artist") or meta["artist"]["name"]
    a["\xa9alb"] = meta["album"]["title"] or meta["title"]
    if meta.get("track_position"):
        a["trkn"] = [(int(meta["track_position"]), 0)]
    if meta.get("disk_number"):
        a["disk"] = [(int(meta["disk_number"]), 0)]
    if meta.get("year"):
        a["\xa9day"] = str(meta["year"])
    a["rtng"] = [1 if meta.get("explicit") else 0]
    a["----:com.apple.iTunes:DEEZER_TRACK_ID"] = [MP4FreeForm(str(meta["id"]).encode())]
    a["----:com.apple.iTunes:SOURCE"] = [MP4FreeForm(f"youtube:{video_id}".encode())]
    if cover:
        a["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
    a.save()


def _tag_mp3(p: Path, meta: dict, video_id: str, cover: bytes | None):
    try:
        t = ID3(p)
    except ID3NoHeaderError:
        t = ID3()
    t.add(TIT2(encoding=3, text=meta["title"]))
    t.add(TPE1(encoding=3, text=meta["artist"]["name"]))
    t.add(TPE2(encoding=3, text=meta.get("album_artist") or meta["artist"]["name"]))
    t.add(TALB(encoding=3, text=meta["album"]["title"] or meta["title"]))
    if meta.get("track_position"):
        t.add(TRCK(encoding=3, text=str(meta["track_position"])))
    if meta.get("disk_number"):
        t.add(TPOS(encoding=3, text=str(meta["disk_number"])))
    if meta.get("year"):
        t.add(TDRC(encoding=3, text=str(meta["year"])))
    t.add(TXXX(encoding=3, desc="DEEZER_TRACK_ID", text=str(meta["id"])))
    t.add(TXXX(encoding=3, desc="SOURCE", text=f"youtube:{video_id}"))
    if cover:
        t.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
    t.save(p)


# ---------- Navidrome ----------

async def trigger_scan():
    """Попросить Navidrome пересканировать библиотеку (если задана учётка).
    Без учётки Navidrome сам заметит файл через watcher/расписание."""
    if not (NAVIDROME_URL and NAVIDROME_USER and NAVIDROME_PASSWORD):
        return
    salt = secrets.token_hex(6)
    token = hashlib.md5((NAVIDROME_PASSWORD + salt).encode()).hexdigest()
    params = {"u": NAVIDROME_USER, "t": token, "s": salt, "v": "1.16.1", "c": "hub", "f": "json"}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.get(f"{NAVIDROME_URL.rstrip('/')}/rest/startScan", params=params)
    except Exception as e:  # noqa: BLE001
        log.warning("navidrome scan failed: %s", e)
