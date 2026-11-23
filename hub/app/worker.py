"""Домашний воркер: забирает у сервера задачи и выполняет их у себя.

  python -m app.worker

Переменные окружения:
  SERVER_URL    адрес сервера, например https://1-2-3-4.sslip.io
  WORKER_TOKEN  общий секрет с сервером
  MUSIC_DIR     временная папка для скачанного (после загрузки на сервер файл удаляется)
  DATA_DIR      локальный кэш (источники, векторы)

Воркер работает только пока включён компьютер. Всё скачанное остаётся на сервере и
играет без него; невыполненные задачи ждут в очереди на сервере.
"""
import asyncio
import base64
import logging
import os
import sys
import time

import httpx

from . import downloader, embedder, library
from .config import AUDIO_FORMAT

log = logging.getLogger("hub.worker")
SERVER_URL = os.environ.get("SERVER_URL", "").rstrip("/")
TOKEN = os.environ.get("WORKER_TOKEN", "")
CAPACITY = int(os.environ.get("WORKER_CAPACITY", "4"))
POLL = 3


async def _file_chunks(path, size=1 << 20):
    with open(path, "rb") as f:
        while chunk := f.read(size):
            yield chunk


class Worker:
    def __init__(self):
        self.http = httpx.AsyncClient(
            base_url=SERVER_URL, headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=httpx.Timeout(60, read=300, write=600))
        self.running: set[int] = set()
        self.kinds = ["fetch", "resolve"] + (["embed"] if embedder.available() else [])

    async def post(self, path: str, **json):
        r = await self.http.post(path, json=json)
        r.raise_for_status()
        return r.json()

    async def run(self):
        log.info("воркер подключается к %s, задачи: %s", SERVER_URL, ", ".join(self.kinds))
        try:
            r = await self.post("/worker/hello")
            if r.get("released"):
                log.info("вернул в очередь %d незавершённых задач прошлого запуска", r["released"])
        except Exception as e:  # noqa: BLE001
            log.warning("hello не удался: %s", e)
        backoff = POLL
        while True:
            try:
                free = CAPACITY - len(self.running)
                # при полной загрузке просто сообщаем серверу, что мы на связи
                jobs = (await self.post("/worker/claim", kinds=self.kinds if free > 0 else [], n=max(free, 1)))["jobs"]
                backoff = POLL
                for j in jobs:
                    self.running.add(j["id"])
                    asyncio.create_task(self.handle(j))
            except httpx.HTTPStatusError as e:
                log.error("сервер ответил %s: проверьте WORKER_TOKEN", e.response.status_code)
                backoff = 30
            except Exception as e:  # noqa: BLE001
                log.warning("нет связи с сервером: %s", e)
                backoff = min(backoff * 2, 60)
            await asyncio.sleep(backoff)

    async def handle(self, job: dict):
        jid, kind, tid, meta = job["id"], job["kind"], job["track_id"], job["meta"]
        label = f"{meta['artist']['name']} — {meta['title']}"
        try:
            if kind == "resolve":
                cands = await downloader.resolve(tid, meta, force=True)
                await self.post(f"/worker/jobs/{jid}/candidates", candidates=cands)
                log.info("источники: %s (%d)", label, len(cands))
            elif kind == "embed":
                vec = await embedder.embed_track(meta)
                if vec is None:
                    raise RuntimeError("не удалось посчитать вектор")
                await self.post(f"/worker/jobs/{jid}/embedding", vec_b64=base64.b64encode(vec.tobytes()).decode(),
                                model=embedder.MODEL_ID)
            elif kind == "fetch":
                await self.fetch(job, label)
        except Exception as e:  # noqa: BLE001
            log.exception("задача %s (%s) не выполнена", jid, label)
            try:
                await self.post(f"/worker/jobs/{jid}/error", error=str(e)[:500])
            except Exception:  # noqa: BLE001
                pass
        finally:
            self.running.discard(jid)

    async def fetch(self, job: dict, label: str):
        jid, tid, meta = job["id"], job["track_id"], job["meta"]
        await self.post(f"/worker/jobs/{jid}/status", status="resolving")
        cands = await downloader.resolve(tid, meta)
        if cands:
            await self.post(f"/worker/jobs/{jid}/candidates", candidates=cands)
        if job.get("video_id"):  # источник выбран вручную — пробуем только его
            chosen = next((c for c in cands if c["video_id"] == job["video_id"]), None)
            tries = [chosen or {"video_id": job["video_id"], "title": job["video_id"], "score": 0}]
        else:
            # YouTube иногда отвечает 403 на конкретное видео: тогда берём следующего кандидата
            tries = [c for c in cands[:3] if c.get("score", 0) >= cands[0].get("score", 0) - 40] if cands else []
        if not tries:
            raise RuntimeError("источник не найден")
        path, chosen, errors = None, None, []
        t0 = time.monotonic()
        for cand in tries:
            await self.post(f"/worker/jobs/{jid}/status", status="downloading", source_id=cand["video_id"],
                            source_title=cand["title"], source_score=cand.get("score"))
            try:
                async with downloader._dl_sem:
                    path = await asyncio.to_thread(downloader._fetch_and_tag, meta, cand["video_id"])
                chosen = cand
                break
            except Exception as e:  # noqa: BLE001
                errors.append(f"{cand['video_id']}: {str(e)[-120:]}")
                log.warning("источник %s не скачался (%s), пробую следующий", cand["video_id"], str(e)[-80:])
        if not path:
            raise RuntimeError("все источники не скачались: " + " | ".join(errors))
        t_dl = time.monotonic() - t0
        try:
            r = await self.http.put(
                f"/worker/jobs/{jid}/file", content=_file_chunks(path),
                params={"ext": path.suffix.lstrip("."), "source_id": chosen["video_id"],
                        "source_title": chosen["title"], "source_score": chosen.get("score") or 0},
                headers={"Content-Type": "application/octet-stream"})
            r.raise_for_status()
        finally:
            path.unlink(missing_ok=True)  # на сервере файл уже есть; дома копию не держим
            for d in (path.parent, path.parent.parent):
                try:
                    d.rmdir()
                except OSError:
                    break
        log.info("готово: %s → сервер (%d КБ; скачивание %.0f с, загрузка %.0f с)", label,
                 r.json().get("size", 0) // 1024, t_dl, time.monotonic() - t0 - t_dl)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not SERVER_URL or not TOKEN:
        sys.exit("Задайте SERVER_URL и WORKER_TOKEN (файл .env).")
    try:
        asyncio.run(Worker().run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
