"""Hub: поиск по каталогу, подбор источника, скачивание и раздача треков + веб-плеер."""
import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import os
from contextlib import asynccontextmanager

from . import auth, deezer, downloader, embedder, library, recommender, taste
from .config import HUB_MODE, WEB_DIR, WORKER_TOKEN

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
if HUB_MODE == "server":
    library.release_running_jobs()  # очередь переживает перезапуск; «качается» без воркера → «в очереди»
else:
    library.reset_stuck()


@asynccontextmanager
async def lifespan(_app):
    from . import cache
    tasks = [asyncio.create_task(recommender.daily_scheduler(int(os.environ.get("MIX_HOUR", "6")))),
             asyncio.create_task(cache.warm_mix_loop()),
             asyncio.create_task(cache.housekeeping_loop())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Music Hub", lifespan=lifespan)
app.middleware("http")(auth.middleware)
if HUB_MODE == "server" and WORKER_TOKEN:
    from . import remote
    app.include_router(remote.router)


@app.get("/api/worker")
async def worker_info():
    from . import workerstate
    return {"mode": HUB_MODE, "online": workerstate.online() if HUB_MODE == "server" else True,
            "pending": library.pending_jobs()}


def annotate(tracks: list[dict]) -> list[dict]:
    st = library.statuses([t["id"] for t in tracks])
    rt = library.ratings()
    for t in tracks:
        t["status"] = st.get(t["id"])
        t["rating"] = rt.get(t["id"], 0)
    library.cache_meta([{k: v for k, v in t.items() if k not in ("status", "rating")} for t in tracks])
    return tracks


async def full_meta(track_id: int) -> dict:
    row = library.get(track_id)
    if row and row["meta"].get("track_position"):
        return row["meta"]
    try:
        return await deezer.track(track_id)
    except Exception as e:
        raise HTTPException(502, f"Deezer: {e}")


# ---------- каталог ----------

@app.get("/api/search")
async def search(q: str):
    if not q.strip():
        return {"tracks": [], "artists": [], "albums": []}
    try:
        res = await deezer.search(q.strip())
    except Exception as e:
        raise HTTPException(502, f"Deezer: {e}")
    annotate(res["tracks"])
    return res


@app.get("/api/artist/{artist_id}")
async def artist(artist_id: int):
    a = await deezer.artist(artist_id)
    annotate(a["top"])
    return a


@app.get("/api/album/{album_id}")
async def album(album_id: int):
    a = await deezer.album(album_id)
    for t in a["tracks"]:
        t["year"] = a["year"]
    annotate(a["tracks"])
    return a


# ---------- библиотека / скачивание ----------

class Ids(BaseModel):
    ids: list[int]


class Source(BaseModel):
    video_id: str


@app.post("/api/prefetch")
async def prefetch(body: Ids):
    """Заранее подобрать источники для видимых треков (без скачивания)."""
    async def one(i):
        try:
            row = library.get(i)
            if row and row["candidates"]:
                return
            await downloader.resolve(i, await full_meta(i))
        except Exception:
            pass
    if HUB_MODE == "server":  # не засоряем очередь воркера; источники подберутся при скачивании
        return {"ok": True}
    for i in body.ids[:30]:
        asyncio.create_task(one(i))
    return {"ok": True}


@app.post("/api/tracks/{track_id}/fetch")
async def fetch(track_id: int, priority: int = 0, keep: bool = False):
    meta = await full_meta(track_id)
    status = downloader.enqueue(track_id, meta, priority=priority)
    if keep:
        library.set_kept(track_id)
    return {"status": status}


class Warm(BaseModel):
    ids: list[int]
    priority: int = 1
    keep: bool = False


@app.post("/api/cache/warm")
async def cache_warm(body: Warm):
    """Скачать заранее. Плеер просит так следующие треки очереди (приоритет 1),
    кнопка «Скачать альбом» ещё и оставляет их в «Моей музыке» (keep)."""
    ids = list(dict.fromkeys(body.ids))[:30]
    out = {}
    for i in ids:
        row = library.get(i)
        if row and row["status"] == "ready" and library.local_path(row):
            out[i] = "ready"
        else:
            # полные метаданные (с номером трека) нужны для имени файла; Deezer кэшируется
            out[i] = downloader.enqueue(i, await full_meta(i), priority=max(0, min(body.priority, 2)))
        if body.keep:
            library.set_kept(i)
    return {"statuses": out}


@app.get("/api/cache")
async def cache_info():
    from . import cache
    return await asyncio.to_thread(cache.stats)


@app.get("/api/tracks/{track_id}")
async def track_status(track_id: int):
    row = library.get(track_id)
    if not row:
        return {"id": track_id, "status": None}
    return {k: row[k] for k in ("id", "status", "error", "source_id", "source_title", "source_score", "meta")}


@app.get("/api/tracks/{track_id}/candidates")
async def candidates(track_id: int, refresh: bool = False):
    meta = await full_meta(track_id)
    cands = await downloader.resolve(track_id, meta, force=refresh)
    row = library.get(track_id)
    from . import workerstate
    return {"chosen": row["source_id"] if row else None, "candidates": cands,
            "worker_online": workerstate.online() if HUB_MODE == "server" else True}


@app.post("/api/tracks/{track_id}/source")
async def set_source(track_id: int, body: Source):
    """Перекачать трек из выбранного вручную источника."""
    meta = await full_meta(track_id)
    return {"status": downloader.enqueue(track_id, meta, video_id=body.video_id, priority=0)}


@app.delete("/api/tracks/{track_id}")
async def delete(track_id: int):
    row = library.get(track_id)
    p = library.local_path(row) if row else None
    if p:
        p.unlink(missing_ok=True)
    library.delete(track_id)
    return {"ok": True}


@app.get("/api/library")
async def lib():
    out = []
    rt = library.ratings()
    for r in library.kept_tracks():
        m = r["meta"]
        m["status"] = "ready"
        m["rating"] = rt.get(r["id"], 0)
        out.append(m)
    return {"tracks": out}


# ---------- действия и рекомендации ----------

EVENT_TYPES = {"play", "complete", "skip", "like", "unlike", "dislike", "undislike"}


class Event(BaseModel):
    track_id: int
    type: str
    position: float | None = None
    wave: str | None = None


@app.post("/api/events")
async def event(e: Event):
    if e.type not in EVENT_TYPES:
        raise HTTPException(400, "unknown event")
    meta = library.cached_meta([e.track_id]).get(e.track_id)
    if not meta:
        meta = await full_meta(e.track_id)
        library.cache_meta([meta])
    library.add_event(e.track_id, meta["artist"]["id"], e.type, e.position)
    if e.type in ("play", "complete", "like"):
        library.set_kept(e.track_id)  # послушали или лайкнули: остаётся в «Моей музыке»
    if e.wave and e.wave in recommender.waves:
        await recommender.wave_event(recommender.waves[e.wave], e.track_id, e.type)
    # считаем вектор заранее, чтобы профиль строился быстро
    asyncio.create_task(embedder.embed_track(meta))
    return {"ok": True, "rating": library.ratings().get(e.track_id, 0)}


@app.get("/api/mix/daily")
async def daily_mix():
    mix = library.get_playlist("daily")
    if mix:
        annotate(mix["tracks"])
    return {"mix": mix, "state": recommender.mix_state}


@app.post("/api/mix/daily/rebuild")
async def daily_rebuild():
    if recommender.mix_state["running"]:
        return {"ok": True, "state": recommender.mix_state}

    async def run():
        try:
            await recommender.build_daily_mix()
        except Exception:
            pass
    asyncio.create_task(run())
    await asyncio.sleep(0.05)
    return {"ok": True, "state": recommender.mix_state}


class WaveStart(BaseModel):
    track_id: int | None = None
    artist_id: int | None = None


@app.post("/api/wave/start")
async def wave_start(body: WaveStart):
    try:
        w = await recommender.start_wave(body.track_id, body.artist_id)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    head = []
    if body.track_id and body.track_id in w.pool.items:
        head = [w.pool.items[body.track_id].meta]
        w.seen.add(body.track_id)
        w.history.append(body.track_id)
    tracks = head + await recommender.wave_next(w, 4)
    return {"id": w.id, "tracks": annotate(tracks)}


@app.get("/api/wave/{wave_id}/next")
async def wave_next(wave_id: str, n: int = 3):
    w = recommender.waves.get(wave_id)
    if not w:
        raise HTTPException(404, "волна устарела")
    return {"tracks": annotate(await recommender.wave_next(w, max(1, min(n, 10))))}


@app.get("/api/profile")
async def profile():
    stats, _ = taste.compute_stats()
    prof = await taste.build_profile() if embedder.available() else None
    metas = library.cached_meta(list(stats))
    aff: dict[int, list] = {}
    for tid, s in stats.items():
        m = metas.get(tid)
        if m:
            a = aff.setdefault(m["artist"]["id"], [m["artist"]["name"], 0.0])
            a[1] += s.weight
    top = sorted(aff.values(), key=lambda x: -x[1])[:10]
    return {
        "events": len(library.events()),
        "tracks": len(stats),
        "liked": sum(1 for s in stats.values() if s.like),
        "disliked": sum(1 for s in stats.values() if s.dislike),
        "top_artists": [{"name": n, "weight": round(w, 2)} for n, w in top],
        "clusters": [round(x, 2) for x in (prof.centroid_w if prof else [])],
        "embedder": embedder.info(),
    }


@app.get("/api/stream/{track_id}")
async def stream(track_id: int):
    row = library.get(track_id)
    p = library.local_path(row) if row and row["status"] == "ready" else None
    if not p:
        raise HTTPException(404, "трек ещё не скачан")
    media = "audio/mpeg" if p.suffix == ".mp3" else "audio/mp4"
    return FileResponse(p, media_type=media)


# ---------- веб-плеер ----------

@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=WEB_DIR), name="web")
