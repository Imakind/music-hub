"""Кэш треков: прогрев микса, повторные попытки и очистка.

Скачанные треки делятся на два вида:
  kept=1  «Моя музыка»: треки, которые слушали, лайкнули или скачали руками
  kept=0  кэш: скачано заранее (следующие в очереди, Микс дня), ещё не слушали

Кэш не засоряет библиотеку и чистится сам: через CACHE_DAYS дней без прослушивания
или раньше, если он больше CACHE_MAX_GB. Треки сегодняшнего микса не удаляются.
"""
import asyncio
import logging
import os
import shutil
import time

from . import downloader, library, workerstate
from .config import HUB_MODE, MUSIC_DIR

log = logging.getLogger("hub.cache")

MAX_CACHE_GB = float(os.environ.get("CACHE_MAX_GB", "10"))
CACHE_DAYS = float(os.environ.get("CACHE_DAYS", "7"))
WARM_BUDGET = int(os.environ.get("WARM_BUDGET", "8"))   # сколько фоновых скачиваний держим в очереди
MAX_ATTEMPTS = 3


def stats() -> dict:
    kept = cached = 0
    cache_bytes = 0
    for r in library.all_ready():
        p = library.local_path(r)
        if not p:
            continue
        if r["kept"]:
            kept += 1
        else:
            cached += 1
            cache_bytes += p.stat().st_size
    return {"kept": kept, "cached": cached, "cache_mb": round(cache_bytes / 1e6),
            "cache_limit_gb": MAX_CACHE_GB, "cache_days": CACHE_DAYS,
            "disk_free_gb": round(shutil.disk_usage(MUSIC_DIR).free / 1e9, 1),
            "queued": library.pending_jobs().get("fetch", 0)}


def evict() -> int:
    """Удалить старый или лишний кэш. Возвращает число удалённых треков."""
    now = time.time()
    mix = library.get_playlist("daily")
    protect = {t["id"] for t in mix["tracks"]} if mix else set()
    items = []
    for r in library.all_ready():
        if r["kept"] or r["id"] in protect:
            continue
        p = library.local_path(r)
        if p:
            items.append((r["updated_at"] or 0, r["id"], p, p.stat().st_size))
    items.sort()  # старые первыми
    total = sum(i[3] for i in items)
    removed = 0
    for updated, tid, p, size in items:
        if not (now - updated > CACHE_DAYS * 86400 or total > MAX_CACHE_GB * 1e9):
            break
        p.unlink(missing_ok=True)
        library.upsert(tid, status="resolved", path=None)
        total -= size
        removed += 1
    if removed:
        log.info("кэш: удалено %d треков", removed)
    return removed


def warm_mix_once() -> int:
    """Дозакачать недостающее из сегодняшнего микса (и повторить ошибки), понемногу."""
    mix = library.get_playlist("daily")
    if not mix:
        return 0
    budget = WARM_BUDGET - library.pending_jobs().get("fetch", 0)
    queued = 0
    for t in mix["tracks"]:
        if queued >= budget:
            break
        row = library.get(t["id"])
        if row and row["status"] == "ready" and library.local_path(row):
            continue
        if row and row["status"] in ("queued", "resolving", "downloading"):
            continue
        if library.job_failures(t["id"]) >= MAX_ATTEMPTS:
            continue
        downloader.enqueue(t["id"], t, priority=2)
        queued += 1
    return queued


async def warm_mix_loop():
    """На сервере: пока домашний воркер на связи, догружаем микс."""
    while True:
        await asyncio.sleep(90)
        try:
            if HUB_MODE == "server" and workerstate.online():
                n = await asyncio.to_thread(warm_mix_once)
                if n:
                    log.info("прогрев микса: поставлено %d треков", n)
        except Exception:  # noqa: BLE001
            log.exception("warm_mix_loop")


async def housekeeping_loop():
    while True:
        try:
            await asyncio.to_thread(evict)
        except Exception:  # noqa: BLE001
            log.exception("evict")
        await asyncio.sleep(1800)
