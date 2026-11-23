"""API для домашнего воркера (режим server). Доступ только по токену WORKER_TOKEN.

Воркер забирает задачи (claim), выполняет их у себя и присылает результат:
  fetch   — скачать трек: воркер загружает готовый файл с тегами (PUT /file)
  resolve — подобрать источники: воркер присылает список кандидатов
  embed   — посчитать вектор звучания: воркер присылает 512 чисел
"""
import base64
import hmac
import logging
import secrets
import shutil

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import downloader, library, workerstate
from .config import DATA_DIR, WORKER_TOKEN

log = logging.getLogger("hub.remote")


def check(request: Request):
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not WORKER_TOKEN or not hmac.compare_digest(given.encode(), WORKER_TOKEN.encode()):
        raise HTTPException(401, "bad token")
    workerstate.seen()


router = APIRouter(prefix="/worker", dependencies=[Depends(check)])


class Claim(BaseModel):
    kinds: list[str] = ["fetch", "resolve", "embed"]
    n: int = 3


@router.post("/claim")
async def claim(body: Claim):
    jobs = library.claim_jobs([k for k in body.kinds if k in ("fetch", "resolve", "embed")], max(1, min(body.n, 10)))
    for j in jobs:
        if j["kind"] == "fetch":
            row = library.get(j["track_id"])
            if row and row["status"] == "queued":
                library.upsert(j["track_id"], status="resolving")
    return {"jobs": [{k: j[k] for k in ("id", "kind", "track_id", "meta", "video_id")} for j in jobs]}


@router.post("/hello")
async def hello():
    """Воркер только что запустился. Воркер один, поэтому его прежние незавершённые задачи
    (компьютер уснул, перезагрузился) сразу возвращаются в очередь."""
    n = library.release_running_jobs()
    if n:
        log.info("воркер перезапущен: %d задач возвращено в очередь", n)
    return {"released": n}


def _job(job_id: int) -> dict:
    j = library.get_job(job_id)
    if not j:
        raise HTTPException(404, "нет такой задачи")
    return j


class Candidates(BaseModel):
    candidates: list[dict]


@router.post("/jobs/{job_id}/candidates")
async def candidates(job_id: int, body: Candidates):
    j = _job(job_id)
    row = library.get(j["track_id"])
    if row is None:
        library.upsert(j["track_id"], j["meta"], status="resolved", candidates=body.candidates)
    else:
        library.upsert(j["track_id"], candidates=body.candidates)
    if j["kind"] == "resolve":
        library.finish_job(job_id)
    return {"ok": True}


class Status(BaseModel):
    status: str
    source_id: str | None = None
    source_title: str | None = None
    source_score: float | None = None


@router.post("/jobs/{job_id}/status")
async def status(job_id: int, body: Status):
    j = _job(job_id)
    if body.status not in ("resolving", "downloading"):
        raise HTTPException(400, "bad status")
    fields = {"status": body.status}
    for k in ("source_id", "source_title", "source_score"):
        if getattr(body, k) is not None:
            fields[k] = getattr(body, k)
    library.upsert(j["track_id"], **fields)
    library.touch_job(job_id)
    return {"ok": True}


class Embedding(BaseModel):
    vec_b64: str
    model: str


@router.post("/jobs/{job_id}/embedding")
async def embedding(job_id: int, body: Embedding):
    j = _job(job_id)
    raw = base64.b64decode(body.vec_b64)
    if len(raw) != 512 * 4:
        raise HTTPException(400, "ожидается 512 float32")
    library.put_embedding(j["track_id"], body.model, raw)
    library.finish_job(job_id)
    return {"ok": True}


class Failure(BaseModel):
    error: str


@router.post("/jobs/{job_id}/error")
async def error(job_id: int, body: Failure):
    j = _job(job_id)
    library.finish_job(job_id, "error", body.error[:500])
    if j["kind"] == "fetch":
        library.upsert(j["track_id"], status="error", error=body.error[:500])
    return {"ok": True}


@router.put("/jobs/{job_id}/file")
async def upload(job_id: int, request: Request, ext: str = "m4a", source_id: str = "",
                 source_title: str = "", source_score: float | None = None):
    j = _job(job_id)
    if j["kind"] != "fetch" or ext not in ("m4a", "mp3"):
        raise HTTPException(400, "не файловая задача")
    meta = j["meta"]
    tmp_dir = DATA_DIR / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    tmp = tmp_dir / f"up-{secrets.token_hex(6)}"
    size = 0
    with open(tmp, "wb") as f:
        async for chunk in request.stream():
            size += len(chunk)
            f.write(chunk)
    if size < 10_000:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, "файл слишком маленький")
    dst = downloader.target_path(meta, ext)
    dst.parent.mkdir(parents=True, exist_ok=True)
    old = library.get(j["track_id"])
    old_path = library.local_path(old) if old else None
    shutil.move(str(tmp), dst)
    if old_path and old_path != dst:
        old_path.unlink(missing_ok=True)
    library.upsert(j["track_id"], meta, status="ready", path=str(dst), error=None,
                   source_id=source_id or None, source_title=source_title or None, source_score=source_score)
    library.finish_job(job_id)
    log.info("uploaded %s — %s (%d KB)", meta["artist"]["name"], meta["title"], size // 1024)
    await downloader.trigger_scan()
    return {"ok": True, "size": size}
