"""Эмбеддер: звук → вектор из 512 чисел.

Используем готовую модель CLAP, обученную на музыке (laion/larger_clap_music):
спектрограмма → нейросеть → 512-мерный вектор. Близкое звучание даёт близкие
векторы, похожесть меряем косинусом. Обучать ничего не нужно.

Анализируем 30-секундное превью из каталога Deezer, поэтому качать трек целиком
не нужно и можно оценивать музыку, которой ещё нет в библиотеке. Превью режем
на три куска по 10 с (окно модели), усредняем и нормируем.

Если torch/transformers не установлены, эмбеддер выключен, а рекомендации
работают только на сигналах внешних сервисов.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading

import httpx
import numpy as np

import time

from . import deezer, library, workerstate
from .config import FFMPEG_LOCATION, HUB_MODE

SERVER = HUB_MODE == "server"

log = logging.getLogger("hub.embedder")

MODEL_ID = os.environ.get("EMBED_MODEL", "laion/larger_clap_music")
SR = 48000
DIM = 512

_model = None
_fe = None
_device = "cpu"
_load_lock = threading.Lock()
_gpu_lock = threading.Lock()
_sem = asyncio.Semaphore(4)
_inflight: dict[int, asyncio.Future] = {}
_disabled_reason: str | None = None


def available() -> bool:
    global _disabled_reason
    if SERVER:  # векторы приходят от домашнего воркера
        return True
    if _disabled_reason:
        return False
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
        return True
    except Exception as e:  # noqa: BLE001
        _disabled_reason = f"нет torch/transformers: {e}"
        return False


def info() -> dict:
    if SERVER:
        return {"enabled": True, "model": MODEL_ID, "device": "домашний воркер", "loaded": True, "reason": None}
    return {"enabled": available(), "model": MODEL_ID, "device": _device if _model else None,
            "loaded": _model is not None, "reason": _disabled_reason}


def _load():
    global _model, _fe, _device
    with _load_lock:
        if _model is not None:
            return
        import torch
        from transformers import ClapModel, ClapFeatureExtractor
        _device = "cuda" if torch.cuda.is_available() else "cpu"
        log.info("loading %s on %s", MODEL_ID, _device)
        _fe = ClapFeatureExtractor.from_pretrained(MODEL_ID)
        # .bin для этой модели уже скачан целиком; не тянем второй вариант (safetensors)
        m = ClapModel.from_pretrained(MODEL_ID, use_safetensors=False)
        if _device == "cuda":
            m = m.half()
        _model = m.to(_device).eval()


def _ffmpeg_exe() -> str:
    if FFMPEG_LOCATION:
        return FFMPEG_LOCATION if FFMPEG_LOCATION.lower().endswith((".exe", "ffmpeg")) else os.path.join(FFMPEG_LOCATION, "ffmpeg")
    return "ffmpeg"


def decode(data: bytes) -> np.ndarray:
    """mp3/m4a → float32 моно 48 кГц."""
    p = subprocess.run(
        [_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-ac", "1", "-ar", str(SR), "-f", "f32le", "pipe:1"],
        input=data, capture_output=True, timeout=60,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode(errors="ignore")[:200])
    return np.frombuffer(p.stdout, dtype=np.float32)


def embed_audio(wave: np.ndarray) -> np.ndarray:
    """Волна → нормированный 512-мерный вектор."""
    import torch
    _load()
    win = SR * 10
    chunks = [wave[i:i + win] for i in range(0, max(len(wave) - win // 2, 1), win)] or [wave]
    chunks = [c for c in chunks if len(c) > SR * 3] or [wave]
    feats = _fe([c.astype(np.float32) for c in chunks], sampling_rate=SR, return_tensors="pt")
    with _gpu_lock, torch.no_grad():
        kw = {k: v.to(_device) for k, v in feats.items()}
        if _device == "cuda":
            kw = {k: (v.half() if v.dtype == torch.float32 else v) for k, v in kw.items()}
        out = _model.get_audio_features(**kw)
        if not isinstance(out, torch.Tensor):  # новые transformers возвращают ModelOutput
            emb = getattr(out, "audio_embeds", None)
            out = emb if emb is not None else out.pooler_output
        v = out.float().mean(0).cpu().numpy()
    return (v / (np.linalg.norm(v) + 1e-9)).astype(np.float32)


async def _fetch_preview(meta: dict) -> bytes:
    url = meta.get("preview")
    async with httpx.AsyncClient(timeout=20) as c:
        if url:
            r = await c.get(url)
            if r.status_code == 200 and r.content:
                return r.content
        # ссылка на превью подписана и протухает — берём свежую
        deezer._cache.pop((f"/track/{meta['id']}", ()), None)
        fresh = await deezer.track(meta["id"])
        if not fresh.get("preview"):
            raise RuntimeError("у трека нет превью")
        r = await c.get(fresh["preview"])
        r.raise_for_status()
        return r.content


async def embed_track(meta: dict) -> np.ndarray | None:
    """Вектор трека (из кэша или посчитать по превью / скачанному файлу)."""
    if not available():
        return None
    tid = meta["id"]
    got = library.get_embeddings([tid], MODEL_ID)
    if tid in got:
        return np.frombuffer(got[tid], dtype=np.float32)
    if SERVER:
        library.add_job("embed", tid, meta)  # посчитает воркер
        return None
    if tid in _inflight:
        return await _inflight[tid]
    fut = asyncio.get_running_loop().create_future()
    _inflight[tid] = fut
    try:
        async with _sem:
            row = library.get(tid)
            lp = library.local_path(row) if row and row["status"] == "ready" else None
            if lp:
                data = await asyncio.to_thread(_read_middle, str(lp))
            else:
                data = await _fetch_preview(meta)
            wave = await asyncio.to_thread(decode, data)
            vec = await asyncio.to_thread(embed_audio, wave)
        library.put_embedding(tid, MODEL_ID, vec.tobytes())
        fut.set_result(vec)
        return vec
    except Exception as e:  # noqa: BLE001
        log.warning("embed %s failed: %s", tid, e)
        fut.set_result(None)
        return None
    finally:
        _inflight.pop(tid, None)


def _read_middle(path: str) -> bytes:
    """Из скачанного файла берём 30 с из середины (как превью)."""
    p = subprocess.run(
        [_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-ss", "45", "-t", "30", "-i", path,
         "-f", "mp3", "pipe:1"], capture_output=True, timeout=60)
    if p.returncode != 0 or not p.stdout:
        with open(path, "rb") as f:
            return f.read()
    return p.stdout


async def embed_many(metas: list[dict], wait: float = 0) -> dict[int, np.ndarray]:
    """Векторы для списка треков; недостающие считаются параллельно.

    В режиме server недостающие уходят воркеру; ждём их не дольше `wait` секунд
    (и только если воркер на связи), остальное используется как есть."""
    if not metas or not available():
        return {}
    ids = [m["id"] for m in metas]
    have = {k: np.frombuffer(v, dtype=np.float32) for k, v in library.get_embeddings(ids, MODEL_ID).items()}
    todo = [m for m in metas if m["id"] not in have]
    if SERVER:
        jobs = [library.add_job("embed", m["id"], m) for m in todo]
        deadline = time.time() + (wait if workerstate.online() else 0)
        while jobs and time.time() < deadline:
            await asyncio.sleep(2)
            jobs = [j for j in jobs if (library.get_job(j) or {}).get("state") in ("pending", "running")]
        if todo:
            have = {k: np.frombuffer(v, dtype=np.float32)
                    for k, v in library.get_embeddings(ids, MODEL_ID).items()}
        return have
    if todo:
        res = await asyncio.gather(*(embed_track(m) for m in todo))
        for m, v in zip(todo, res):
            if v is not None:
                have[m["id"]] = v
    return have
