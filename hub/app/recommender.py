"""Рекомендации: кандидаты от внешнего каталога + оценка по звучанию и профилю вкуса.

Внешний сервис (Deezer: «радио» исполнителя, похожие артисты, новые релизы)
предлагает кандидатов на основе своих данных о миллионах слушателей. Мы
оцениваем, насколько каждый подходит именно под наш профиль, и собираем
итоговый список сами.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import deezer, downloader, embedder, library, sources, taste
from .config import MUSIC_DIR

log = logging.getLogger("hub.rec")

MIX_SIZE = 50
FAMILIAR_SHARE = 0.4


def _key(m: dict) -> str:
    return sources.norm(m["artist"]["name"]) + "|" + sources.norm(m.get("title_short") or m["title"])


def _unwanted(m: dict) -> bool:
    return sources.has_any(m["title"], sources.JUNK) or sources.has_any(m["title"], sources.CLEAN)


def _pct(values: list[float | None]) -> list[float]:
    """Значения → перцентили 0..1 (None → 0.5). Так разные шкалы складываются честно."""
    idx = [i for i, v in enumerate(values) if v is not None]
    out = [0.5] * len(values)
    if len(idx) > 1:
        order = sorted(idx, key=lambda i: values[i])
        for r, i in enumerate(order):
            out[i] = r / (len(order) - 1)
    return out


@dataclass
class Cand:
    meta: dict
    prior: float = 0.0
    tags: set[str] = field(default_factory=set)
    familiar: bool = False
    vec: np.ndarray | None = None
    score: float = 0.0


class Pool:
    def __init__(self):
        self.items: dict[int, Cand] = {}

    def add(self, metas: list[dict], prior: float, tag: str, familiar=False):
        for i, m in enumerate(metas):
            # чуть выше — те, что внешний сервис поставил выше
            p = prior * (1 - 0.3 * i / max(len(metas), 1))
            c = self.items.get(m["id"])
            if c:
                c.prior = max(c.prior, p) + 0.1  # нашёлся в нескольких источниках
                c.tags.add(tag)
                c.familiar = c.familiar or familiar
            else:
                self.items[m["id"]] = Cand(meta=m, prior=p, tags={tag}, familiar=familiar)


async def _safe(coro, default):
    try:
        return await coro
    except Exception as e:  # noqa: BLE001
        log.warning("source failed: %s", e)
        return default


async def gather_external(pool: Pool, artist_weights: list[tuple[int, float]], new_releases=True,
                          related_per_artist=3):
    """Кандидаты от каталога для набора «любимых» исполнителей."""
    if not artist_weights:
        return
    mx = max(w for _, w in artist_weights) or 1

    async def radio(aid, a):
        pool.add(await _safe(deezer.artist_radio(aid, 25), []), 0.5 + 0.3 * a, f"radio:{aid}")

    async def related(aid, a):
        rel = await _safe(deezer.related_artists(aid, 8), [])
        tops = await asyncio.gather(*(_safe(deezer.artist_top(r["id"], 6), []) for r in rel[:related_per_artist]))
        for top in tops:
            pool.add(top, 0.4 + 0.2 * a, f"related:{aid}")

    async def fresh_releases(aid):
        albums = await _safe(deezer.artist_albums(aid), [])
        cutoff = (dt.date.today() - dt.timedelta(days=60)).isoformat()
        fresh = sorted([x for x in albums if x["release_date"] >= cutoff],
                       key=lambda x: x["release_date"], reverse=True)[:2]
        for full in await asyncio.gather(*(_safe(deezer.album(x["id"]), None) for x in fresh)):
            if full:
                for t in full["tracks"]:
                    t["year"] = full["year"]
                pool.add(full["tracks"][:6], 0.9, "new")

    async def per_artist(aid, w):
        a = w / mx
        jobs = [radio(aid, a), related(aid, a)]
        if new_releases:
            jobs.append(fresh_releases(aid))
        await asyncio.gather(*jobs)

    await asyncio.gather(*(per_artist(a, w) for a, w in artist_weights))


def _filter(pool: Pool, prof: taste.Profile, exclude_recent=True):
    seen = set()
    disliked = prof.disliked
    pos = prof.positives
    for tid in list(pool.items):
        c = pool.items[tid]
        m = c.meta
        k = _key(m)
        bad = (tid in disliked or k in seen
               or (exclude_recent and tid in prof.recent)
               or (_unwanted(m) and tid not in pos)
               or not m.get("duration"))
        if bad:
            del pool.items[tid]
        else:
            seen.add(k)


async def _score(pool: Pool, prof: taste.Profile):
    cands = list(pool.items.values())
    if not cands:
        return cands
    library.cache_meta([c.meta for c in cands])
    vecs = await embedder.embed_many([c.meta for c in cands], wait=150)
    for c in cands:
        c.vec = vecs.get(c.meta["id"])
    mx_aff = max([abs(v) for v in prof.artist_aff.values()] or [1]) or 1
    t_pct = _pct([prof.taste(c.vec) for c in cands])
    p_pct = _pct([c.prior for c in cands])
    for c, t, p in zip(cands, t_pct, p_pct):
        aff = prof.artist_aff.get(c.meta["artist"]["id"], 0) / mx_aff
        c.score = 0.5 * t + 0.3 * p + 0.2 * max(-1, min(1, aff))
    return cands


def _order(chosen: list[Cand]) -> list[Cand]:
    """Порядок: соседние треки похожи по звучанию, один исполнитель не идёт подряд."""
    if not chosen:
        return chosen
    rest = sorted(chosen, key=lambda c: c.score, reverse=True)
    out = [rest.pop(0)]
    while rest:
        prev = out[-1]
        recent_artists = {c.meta["artist"]["id"] for c in out[-3:]}

        def val(c):
            sim = float(prev.vec @ c.vec) if prev.vec is not None and c.vec is not None else 0.0
            pen = 1.0 if c.meta["artist"]["id"] in recent_artists else 0.0
            return 0.55 * c.score + 0.45 * sim - pen
        best = max(rest, key=val)
        rest.remove(best)
        out.append(best)
    return out


# ======================= Микс дня =======================

_mix_lock = asyncio.Lock()
mix_state = {"running": False, "stage": "", "error": None}


async def build_daily_mix() -> dict:
    async with _mix_lock:
        mix_state.update(running=True, stage="Строю профиль вкуса", error=None)
        try:
            return await _build_daily_mix()
        except Exception as e:
            log.exception("daily mix failed")
            mix_state["error"] = str(e)
            raise
        finally:
            mix_state.update(running=False, stage="")


async def _build_daily_mix() -> dict:
    prof = await taste.build_profile()
    pool = Pool()
    now = time.time()

    # Знакомое: любимое и давно забытое
    fam = sorted(prof.positives.items(), key=lambda kv: kv[1].weight, reverse=True)
    fam_metas = [prof.metas[i] for i, _ in fam if i in prof.metas]
    pool.add(fam_metas, 0.7, "familiar", familiar=True)
    for i, s in fam:
        if i in pool.items and s.last_play and now - s.last_play > 21 * 86400:
            pool.items[i].prior += 0.3
            pool.items[i].tags.add("forgotten")

    # Новое: от любимых исполнителей
    top_artists = sorted(((a, w) for a, w in prof.artist_aff.items() if w > 0), key=lambda x: -x[1])[:8]
    if not top_artists:
        raise RuntimeError("Пока не из чего собирать: послушайте или скачайте несколько треков")
    mix_state["stage"] = "Собираю кандидатов"
    await gather_external(pool, top_artists)
    _filter(pool, prof)
    mix_state["stage"] = f"Анализирую звучание {len(pool.items)} треков"
    cands = await _score(pool, prof)

    known = set(prof.positives)
    familiar = sorted([c for c in cands if c.meta["id"] in known], key=lambda c: -c.score)
    new = sorted([c for c in cands if c.meta["id"] not in known], key=lambda c: -c.score)
    n_fam = min(len(familiar), round(MIX_SIZE * FAMILIAR_SHARE))
    n_new = min(len(new), MIX_SIZE - n_fam)
    n_fam = min(len(familiar), MIX_SIZE - n_new)

    # не больше 4 треков одного исполнителя в новом
    picked_new, per_artist = [], {}
    for c in new:
        a = c.meta["artist"]["id"]
        if per_artist.get(a, 0) >= 4:
            continue
        per_artist[a] = per_artist.get(a, 0) + 1
        picked_new.append(c)
        if len(picked_new) >= n_new:
            break
    chosen = _order(familiar[:n_fam] + picked_new)

    tracks = []
    for c in chosen:
        m = dict(c.meta)
        m["rec"] = {"score": round(c.score, 3), "tags": sorted(c.tags), "familiar": c.meta["id"] in known}
        tracks.append(m)
    data = {"date": dt.date.today().isoformat(), "tracks": tracks,
            "stats": {"candidates": len(cands), "familiar": n_fam, "new": len(picked_new),
                      "clusters": len(prof.centroid_w), "embeddings": embedder.available()}}
    library.save_playlist("daily", data)

    mix_state["stage"] = "Скачиваю треки"
    for m in tracks:
        downloader.enqueue(m["id"], m)
    asyncio.create_task(_write_m3u_when_ready("Микс дня", [m["id"] for m in tracks]))
    return data


async def _write_m3u_when_ready(name: str, ids: list[int], timeout=3600):
    """Плейлист для Navidrome (он сам импортирует .m3u из папки с музыкой)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = library.statuses(ids)
        if all(st.get(i) in ("ready", "error") for i in ids):
            break
        await asyncio.sleep(10)
    lines = ["#EXTM3U"]
    for i in ids:
        r = library.get(i)
        if r and r["status"] == "ready" and r["path"]:
            p = Path(r["path"])
            try:
                rel = p.relative_to(MUSIC_DIR)
            except ValueError:
                continue
            lines.append(f"#EXTINF:{r['meta'].get('duration', 0)},{r['meta']['artist']['name']} - {r['meta']['title']}")
            lines.append("../" + rel.as_posix())
    pl = MUSIC_DIR / "Playlists"
    pl.mkdir(exist_ok=True)
    (pl / f"{name}.m3u").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def daily_scheduler(hour: int):
    """Каждое утро в `hour` часов пересобираем микс."""
    while True:
        try:
            cur = library.get_playlist("daily")
            today = dt.date.today().isoformat()
            if dt.datetime.now().hour >= hour and (not cur or cur.get("date") != today):
                stats, _ = taste.compute_stats()
                if any(s.weight > 0 for s in stats.values()):
                    await build_daily_mix()
        except Exception:  # noqa: BLE001
            log.exception("scheduler")
        await asyncio.sleep(600)


# ======================= Моя волна =======================

@dataclass
class Wave:
    id: str
    prof: taste.Profile
    pool: Pool
    seed_vec: np.ndarray | None = None
    seed_artist: int | None = None
    pos: list[tuple[np.ndarray, float]] = field(default_factory=list)
    neg: list[tuple[np.ndarray, float]] = field(default_factory=list)
    artist_pen: dict[int, float] = field(default_factory=dict)
    seen: set[int] = field(default_factory=set)
    history: list[int] = field(default_factory=list)
    expanded: set[int] = field(default_factory=set)
    touched: float = field(default_factory=time.time)

    def direction(self) -> np.ndarray | None:
        parts = []
        if self.seed_vec is not None:
            parts.append(self.seed_vec * 1.0)
        parts += [v * w for v, w in self.pos]
        if not parts:
            return None
        d = np.sum(parts, axis=0)
        return d / (np.linalg.norm(d) + 1e-9)


waves: dict[str, Wave] = {}


async def _expand(w: Wave, artist_id: int, prior=0.6):
    if artist_id in w.expanded:
        return
    w.expanded.add(artist_id)
    p2 = Pool()
    await gather_external(p2, [(artist_id, 1.0)], new_releases=False, related_per_artist=4)
    for tid, c in p2.items.items():
        if tid not in w.pool.items:
            c.prior = c.prior * prior / 0.8
            w.pool.items[tid] = c
    _filter(w.pool, w.prof, exclude_recent=False)


async def start_wave(track_id: int | None = None, artist_id: int | None = None) -> Wave:
    prof = await taste.build_profile()
    w = Wave(id=uuid.uuid4().hex[:12], prof=prof, pool=Pool())
    if track_id:
        meta = (library.cached_meta([track_id]).get(track_id)) or await deezer.track(track_id)
        library.cache_meta([meta])
        w.seed_artist = meta["artist"]["id"]
        w.seed_vec = await embedder.embed_track(meta)
        w.pool.add([meta], 2.0, "seed")
        await _expand(w, w.seed_artist, 0.8)
    elif artist_id:
        w.seed_artist = artist_id
        top = await deezer.artist_top(artist_id, 5)
        vecs = await embedder.embed_many(top)
        if vecs:
            v = np.mean(list(vecs.values()), axis=0)
            w.seed_vec = v / (np.linalg.norm(v) + 1e-9)
        w.pool.add(top[:3], 1.2, "seed")
        await _expand(w, artist_id, 0.8)
    else:
        # «по моему вкусу»: любимые исполнители + знакомое
        top_artists = sorted(((a, x) for a, x in prof.artist_aff.items() if x > 0), key=lambda t: -t[1])[:5]
        if not top_artists:
            raise RuntimeError("Пока нет истории: запустите волну от трека или исполнителя")
        fam = sorted(prof.positives.items(), key=lambda kv: -kv[1].weight)[:30]
        w.pool.add([prof.metas[i] for i, _ in fam if i in prof.metas], 0.5, "familiar", familiar=True)
        await gather_external(w.pool, top_artists, new_releases=True, related_per_artist=2)
    _filter(w.pool, prof, exclude_recent=False)
    waves[w.id] = w
    for k in [k for k, v in waves.items() if time.time() - v.touched > 6 * 3600]:
        waves.pop(k, None)
    return w


async def wave_next(w: Wave, n: int = 3) -> list[dict]:
    w.touched = time.time()
    unseen = [c for tid, c in w.pool.items.items() if tid not in w.seen]
    if len(unseen) < 15:
        # пул заканчивается — расширяем от последних понравившихся исполнителей
        for aid in list(dict.fromkeys(reversed([c.meta["artist"]["id"] for c in w.pool.items.values()
                                                 if c.meta["id"] in w.history])))[:3]:
            await _expand(w, aid, 0.5)
        unseen = [c for tid, c in w.pool.items.items() if tid not in w.seen]
    if not unseen:
        return []
    # оцениваем звучание для первых по приоритету кандидатов
    unseen.sort(key=lambda c: -c.prior)
    head = unseen[:40]
    need = [c.meta for c in head if c.vec is None]
    if need:
        vecs = await embedder.embed_many(need)
        for c in head:
            if c.vec is None:
                c.vec = vecs.get(c.meta["id"])
    d = w.direction()
    taste_v = _pct([w.prof.taste(c.vec) for c in head])
    dir_v = _pct([float(d @ c.vec) if d is not None and c.vec is not None else None for c in head])
    prior_v = _pct([c.prior for c in head])
    last_artists = [w.pool.items[t].meta["artist"]["id"] for t in w.history[-2:] if t in w.pool.items]
    for c, t, s, p in zip(head, taste_v, dir_v, prior_v):
        neg = 0.0
        if c.vec is not None and w.neg:
            neg = max(float(v @ c.vec) * k for v, k in w.neg)
        a = c.meta["artist"]["id"]
        c.score = (0.3 * t + 0.4 * s + 0.3 * p
                   - w.artist_pen.get(a, 0)
                   - 0.6 * max(0.0, neg - 0.6)
                   - (0.5 if a in last_artists else 0)
                   + random.uniform(0, 0.05))
    out = []
    for c in sorted(head, key=lambda c: -c.score):
        if len(out) >= n:
            break
        if out and c.meta["artist"]["id"] == out[-1]["artist"]["id"]:
            continue
        out.append(c.meta)
        w.seen.add(c.meta["id"])
        w.history.append(c.meta["id"])
    library.cache_meta(out)
    return out


async def wave_event(w: Wave, track_id: int, type_: str):
    """Реакция внутри волны меняет подбор только на эту сессию."""
    w.touched = time.time()
    c = w.pool.items.get(track_id)
    if not c:
        return
    a = c.meta["artist"]["id"]
    # прошлые негативные сигналы ослабевают — случайный пропуск не становится запретом
    w.neg = [(v, k * 0.6) for v, k in w.neg if k * 0.6 > 0.1]
    if type_ in ("skip", "dislike"):
        w.artist_pen[a] = w.artist_pen.get(a, 0) + (0.15 if type_ == "skip" else 0.5)
        if c.vec is not None:
            w.neg.append((c.vec, 1.0))
    elif type_ in ("complete", "like"):
        w.artist_pen[a] = max(0.0, w.artist_pen.get(a, 0) - 0.1)
        if c.vec is not None:
            w.pos.append((c.vec, 2.0 if type_ == "complete" else 1.0))
            w.pos = w.pos[-12:]
        asyncio.create_task(_expand(w, a, 0.7))
