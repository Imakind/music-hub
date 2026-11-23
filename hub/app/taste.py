"""Профиль вкуса: из действий пользователя → веса треков → кластеры векторов.

Веса действий (как в видео):
  дослушал до конца  +2   (повторы дают убывающий эффект: 2·log2(1+n))
  лайк               +1
  пропуск < 30 с     −1
  «не нравится»      −5 и трек исключается из рекомендаций
Старые действия постепенно теряют вес по мере новой активности (а не по
календарю): каждое следующее событие умножает вес предыдущих на DECAY.
Скачанные вручную треки без событий получают небольшой базовый вес — это
явный интерес, особенно пока истории ещё нет.
"""
from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from . import embedder, library

DECAY = 0.997          # множитель за каждое более новое событие
BASELINE = 0.5         # скачанный трек без событий
MAX_CLUSTERS = 5


@dataclass
class TrackStat:
    plays: float = 0.0
    skips: float = 0.0
    like: bool = False
    dislike: bool = False
    last_play: float = 0.0
    weight: float = 0.0


@dataclass
class Profile:
    stats: dict[int, TrackStat]
    metas: dict[int, dict]
    artist_aff: dict[int, float]
    artist_names: dict[int, str]
    centroids: np.ndarray | None = None          # k×512
    centroid_w: list[float] = field(default_factory=list)
    neg_vecs: np.ndarray | None = None           # m×512
    neg_strength: np.ndarray | None = None
    recent: set[int] = field(default_factory=set)  # слушали за последние 48 ч

    @property
    def positives(self):
        return {i: s for i, s in self.stats.items() if s.weight > 0 and not s.dislike}

    @property
    def disliked(self):
        return {i for i, s in self.stats.items() if s.dislike or s.weight <= -2}

    def taste(self, v: np.ndarray | None) -> float | None:
        """Сходство с ближайшей частью вкуса минус сходство с нелюбимым."""
        if v is None or self.centroids is None:
            return None
        pos = float(np.max(self.centroids @ v))
        neg = 0.0
        if self.neg_vecs is not None and len(self.neg_vecs):
            neg = float(np.max((self.neg_vecs @ v) * self.neg_strength))
        return pos - 0.5 * max(0.0, neg - 0.5) * 2


def compute_stats() -> tuple[dict[int, TrackStat], dict[int, int]]:
    evs = library.events()
    ratings = library.ratings()
    n = len(evs)
    stats: dict[int, TrackStat] = defaultdict(TrackStat)
    artist_of: dict[int, int] = {}
    for idx, e in enumerate(evs):
        d = DECAY ** (n - 1 - idx)
        s = stats[e["track_id"]]
        if e["artist_id"]:
            artist_of[e["track_id"]] = e["artist_id"]
        if e["type"] == "complete":
            s.plays += d
            s.last_play = max(s.last_play, e["ts"])
        elif e["type"] == "play":
            s.last_play = max(s.last_play, e["ts"])
        elif e["type"] == "skip":
            s.skips += d
    for tid, val in ratings.items():
        stats[tid].like = val > 0
        stats[tid].dislike = val < 0
    for r in library.ready_tracks():
        stats[r["id"]]  # создать запись
        artist_of.setdefault(r["id"], r["meta"]["artist"]["id"])
    for s in stats.values():
        w = 2 * math.log2(1 + s.plays) + (1 if s.like else 0) - min(s.skips, 3)
        if s.dislike:
            w = -5
        elif s.plays == 0 and s.skips == 0 and not s.like:
            w = BASELINE
        s.weight = w
    return dict(stats), artist_of


def _spherical_kmeans(X: np.ndarray, w: np.ndarray, k: int, iters: int = 25) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(0)
    # k-means++ инициализация с учётом весов
    idx = [int(np.argmax(w))]
    for _ in range(1, k):
        d = 1 - np.max(X @ X[idx].T, axis=1)
        p = np.clip(d, 0, None) * w
        if p.sum() <= 0:
            break
        idx.append(int(rng.choice(len(X), p=p / p.sum())))
    C = X[idx].copy()
    for _ in range(iters):
        assign = np.argmax(X @ C.T, axis=1)
        newC = []
        for j in range(len(C)):
            m = assign == j
            if not m.any():
                newC.append(C[j]); continue
            c = (X[m] * w[m, None]).sum(0)
            newC.append(c / (np.linalg.norm(c) + 1e-9))
        newC = np.array(newC)
        if np.allclose(newC, C):
            break
        C = newC
    assign = np.argmax(X @ C.T, axis=1)
    cw = np.array([w[assign == j].sum() for j in range(len(C))])
    return C, cw


async def build_profile() -> Profile:
    stats, artist_of = compute_stats()
    metas = library.cached_meta(list(stats))
    aff: dict[int, float] = defaultdict(float)
    names: dict[int, str] = {}
    for tid, s in stats.items():
        m = metas.get(tid)
        aid = artist_of.get(tid) or (m and m["artist"]["id"])
        if not aid:
            continue
        aff[aid] += s.weight
        if m:
            names[aid] = m["artist"]["name"]
    prof = Profile(stats=stats, metas=metas, artist_aff=dict(aff), artist_names=names)
    now = time.time()
    prof.recent = {i for i, s in stats.items() if s.last_play and now - s.last_play < 48 * 3600}

    if not embedder.available():
        return prof
    pos_ids = [i for i, s in prof.positives.items() if i in metas]
    neg_ids = [i for i, s in stats.items() if s.weight < 0 and i in metas]
    vecs = await embedder.embed_many([metas[i] for i in pos_ids + neg_ids], wait=60)
    P = [(vecs[i], stats[i].weight) for i in pos_ids if i in vecs]
    if P:
        X = np.stack([p[0] for p in P]); w = np.array([p[1] for p in P], dtype=np.float32)
        k = max(1, min(MAX_CLUSTERS, len(P) // 4))
        prof.centroids, cw = _spherical_kmeans(X, w, k)
        prof.centroid_w = cw.tolist()
    N = [(vecs[i], min(1.0, -stats[i].weight / 5)) for i in neg_ids if i in vecs]
    if N:
        prof.neg_vecs = np.stack([x[0] for x in N])
        prof.neg_strength = np.array([x[1] for x in N], dtype=np.float32)
    return prof
