"""Каталог: метаданные из публичного API Deezer (ключ не нужен)."""
import asyncio
import time
from collections import deque

import httpx

API = "https://api.deezer.com"
_client = httpx.AsyncClient(base_url=API, timeout=15)

# Лимит Deezer — 50 запросов за 5 секунд; держимся с запасом
_calls: deque[float] = deque()
_rl = asyncio.Lock()
_cache: dict[tuple, tuple[float, object]] = {}
CACHE_TTL = 6 * 3600


async def _throttle():
    async with _rl:
        while True:
            now = time.monotonic()
            while _calls and now - _calls[0] > 5:
                _calls.popleft()
            if len(_calls) < 40:
                _calls.append(now)
                return
            await asyncio.sleep(5 - (now - _calls[0]) + 0.05)


async def _get(path: str, **params):
    key = (path, tuple(sorted(params.items())))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    for attempt in range(3):
        await _throttle()
        r = await _client.get(path, params=params)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "error" in data:
            if data["error"].get("code") == 4 and attempt < 2:  # quota exceeded
                await asyncio.sleep(2)
                continue
            raise RuntimeError(data["error"].get("message", "Deezer error"))
        if len(_cache) > 5000:
            _cache.clear()
        _cache[key] = (time.time(), data)
        return data


def track_out(t: dict, album: dict | None = None) -> dict:
    alb = t.get("album") or album or {}
    return {
        "id": t["id"],
        "title": t.get("title") or "",
        "title_short": t.get("title_short") or t.get("title") or "",
        "version": t.get("title_version") or "",
        "duration": t.get("duration") or 0,
        "explicit": bool(t.get("explicit_lyrics")),
        "track_position": t.get("track_position"),
        "disk_number": t.get("disk_number"),
        "artist": {"id": t["artist"]["id"], "name": t["artist"]["name"]},
        "album": {
            "id": alb.get("id"),
            "title": alb.get("title") or "",
            "cover": alb.get("cover_xl") or alb.get("cover_big") or alb.get("cover_medium") or "",
            "cover_small": alb.get("cover_medium") or alb.get("cover_small") or "",
        },
        "preview": t.get("preview") or "",
        "year": (t.get("release_date") or alb.get("release_date") or "")[:4],
    }


def album_out(a: dict) -> dict:
    return {
        "id": a["id"],
        "title": a.get("title") or "",
        "cover": a.get("cover_xl") or a.get("cover_big") or "",
        "cover_small": a.get("cover_medium") or a.get("cover_small") or "",
        "year": (a.get("release_date") or "")[:4],
        "type": a.get("record_type") or "album",
        "explicit": bool(a.get("explicit_lyrics")),
        "artist": {"id": a["artist"]["id"], "name": a["artist"]["name"]} if a.get("artist") else None,
    }


def artist_out(a: dict) -> dict:
    return {
        "id": a["id"],
        "name": a.get("name") or "",
        "picture": a.get("picture_xl") or a.get("picture_big") or "",
        "picture_small": a.get("picture_medium") or a.get("picture_small") or "",
        "fans": a.get("nb_fan"),
    }


async def search(q: str) -> dict:
    tracks = await _get("/search/track", q=q, limit=25)
    artists = await _get("/search/artist", q=q, limit=6)
    albums = await _get("/search/album", q=q, limit=12)
    return {
        "tracks": [track_out(t) for t in tracks.get("data", [])],
        "artists": [artist_out(a) for a in artists.get("data", [])],
        "albums": [album_out(a) for a in albums.get("data", [])],
    }


async def track(track_id: int) -> dict:
    return track_out(await _get(f"/track/{track_id}"))


async def album(album_id: int) -> dict:
    a = await _get(f"/album/{album_id}")
    out = album_out(a)
    out["tracks"] = [track_out(t, a) for t in a.get("tracks", {}).get("data", [])]
    # у треков из /album нет track_position — проставим по порядку
    for i, t in enumerate(out["tracks"], 1):
        t["track_position"] = t["track_position"] or i
    out["genres"] = [g["name"] for g in a.get("genres", {}).get("data", [])]
    return out


async def related_artists(artist_id: int, limit: int = 15) -> list[dict]:
    d = await _get(f"/artist/{artist_id}/related", limit=limit)
    return [artist_out(a) for a in d.get("data", [])]


async def artist_top(artist_id: int, limit: int = 10) -> list[dict]:
    d = await _get(f"/artist/{artist_id}/top", limit=limit)
    return [track_out(t) for t in d.get("data", [])]


async def artist_radio(artist_id: int, limit: int = 25) -> list[dict]:
    """«Радио» исполнителя у Deezer: его треки вперемешку с похожими."""
    d = await _get(f"/artist/{artist_id}/radio", limit=limit)
    return [track_out(t) for t in d.get("data", [])]


async def artist_albums(artist_id: int) -> list[dict]:
    d = await _get(f"/artist/{artist_id}/albums", limit=50)
    return [album_out({**x, "artist": None}) | {"release_date": x.get("release_date") or ""} for x in d.get("data", [])]


async def artist(artist_id: int) -> dict:
    a = await _get(f"/artist/{artist_id}")
    top = await _get(f"/artist/{artist_id}/top", limit=10)
    albums = await _get(f"/artist/{artist_id}/albums", limit=100)
    out = artist_out(a)
    out["top"] = [track_out(t) for t in top.get("data", [])]
    al = [album_out({**x, "artist": {"id": a["id"], "name": a["name"]}}) for x in albums.get("data", [])]
    al.sort(key=lambda x: x["year"], reverse=True)
    out["albums"] = al
    return out
