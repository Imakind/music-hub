"""Источники аудио и выбор лучшей (желательно нецензурной) версии трека.

Каталог (Deezer) говорит, ЧТО за трек; здесь мы ищем, ГДЕ взять звук.
Кандидаты собираются из YouTube Music (официальные «songs» с флагом isExplicit
и пользовательские «videos») и обычного YouTube-поиска с пометками
«uncensored / без цензуры». Каждый кандидат получает оценку, лучший побеждает.
Ни один признак не гарантирует, что слова сохранились, поэтому в UI можно
посмотреть кандидатов и выбрать другой вручную.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, asdict, field

from ytmusicapi import YTMusic
import yt_dlp

_yt = YTMusic()

CYRILLIC = re.compile(r"[а-яё]", re.I)

# Признаки, что версия НЕ та (если они не встречаются в названии из каталога)
JUNK = [
    "karaoke", "караоке", "instrumental", "инструментал", "минус", "minus",
    "cover", "кавер", "remix", "ремикс", "live", "концерт", "acoustic",
    "sped up", "speed up", "slowed", "nightcore", "8d", "reverb", "bass boosted",
    "tiktok", "1 hour", "1 час", "mashup", "мэшап", "piano", "loop",
    "hz", "432", "pitched", "ускоренная", "замедленная", "edit audio",
]
# Признаки цензурированной версии
CLEAN = ["clean", "radio edit", "censored", "цензура", "без мата", "edited", "радио версия"]
# Признаки нецензурной версии
UNCENSORED = ["uncensored", "explicit", "без цензуры", "нецензур", "dirty", "original version"]


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower().replace("ё", "е")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def has_any(text: str, words: list[str]) -> bool:
    t = " " + norm(text) + " "
    return any((" " + norm(w) + " ") in t or (len(w) > 5 and norm(w) in t) for w in words)


@dataclass
class Candidate:
    video_id: str
    title: str
    channel: str = ""
    duration: int = 0
    kind: str = "video"          # song | video | search
    is_explicit: bool | None = None
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    def to_dict(self):
        d = asdict(self)
        d["url"] = self.url
        return d


def _from_ytm(r: dict) -> Candidate | None:
    vid = r.get("videoId")
    if not vid:
        return None
    artists = ", ".join(a.get("name", "") for a in r.get("artists") or [])
    return Candidate(
        video_id=vid,
        title=r.get("title") or "",
        channel=artists,
        duration=int(r.get("duration_seconds") or 0),
        kind="song" if r.get("resultType") == "song" else "video",
        is_explicit=r.get("isExplicit"),
    )


def _yt_search(query: str, n: int = 8) -> list[Candidate]:
    opts = {"quiet": True, "skip_download": True, "extract_flat": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    out = []
    for e in info.get("entries") or []:
        if not e or not e.get("id"):
            continue
        out.append(Candidate(
            video_id=e["id"], title=e.get("title") or "", channel=e.get("channel") or e.get("uploader") or "",
            duration=int(e.get("duration") or 0), kind="search",
        ))
    return out


def score(c: Candidate, track: dict) -> Candidate:
    want_title = track["title_short"]
    want_full = track["title"]
    artist = track["artist"]["name"]
    hay = f"{c.title} {c.channel}"
    s, why = 0.0, []

    # Совпадение названия и исполнителя
    tt = set(norm(want_title).split())
    ct = set(norm(c.title).split())
    if tt:
        ratio = len(tt & ct) / len(tt)
        s += 40 * ratio
        if ratio < 0.5:
            s -= 30; why.append("название не совпадает")
    if norm(artist) and norm(artist) in norm(hay):
        s += 20
    elif set(norm(artist).split()) & set(norm(hay).split()):
        s += 8
    else:
        s -= 15; why.append("другой исполнитель?")

    # Длительность
    if track["duration"] and c.duration:
        d = abs(track["duration"] - c.duration)
        if d <= 3: s += 30
        elif d <= 8: s += 15
        elif d <= 20: s -= 5
        else:
            s -= 50; why.append(f"длительность отличается на {d}с")

    # Мусорные версии
    for w in JUNK:
        if has_any(c.title, [w]) and not has_any(want_full, [w]):
            s -= 35; why.append(f"«{w}»")
            break

    # Цензура
    if track["explicit"]:
        if c.is_explicit is True:
            s += 25; why.append("YT Music: Explicit")
        elif c.kind == "song" and c.is_explicit is False:
            s -= 20; why.append("YT Music: не Explicit — возможно, зацензурено")
        if has_any(c.title, UNCENSORED):
            s += 20; why.append("пометка «без цензуры»")
        if has_any(c.title, CLEAN):
            s -= 60; why.append("цензурированная версия")

    if c.kind == "song":
        s += 10

    c.score = round(s, 1)
    c.reasons = why
    return c


def find_candidates(track: dict) -> list[Candidate]:
    artist = track["artist"]["name"]
    title = track["title"]
    q = f"{artist} {title}"
    cands: dict[str, Candidate] = {}

    def add(lst):
        for c in lst:
            if c and c.video_id not in cands:
                cands[c.video_id] = c

    russian = bool(CYRILLIC.search(q))
    try:
        add(_from_ytm(r) for r in _yt.search(q, filter="songs", limit=8))
    except Exception:
        pass
    try:
        add(_from_ytm(r) for r in _yt.search(q, filter="videos", limit=6))
    except Exception:
        pass
    if track["explicit"]:
        suffix = "без цензуры" if russian else "uncensored explicit"
        try:
            add(_yt_search(f"{q} {suffix}", 6))
        except Exception:
            pass
    if not cands:
        add(_yt_search(q, 8))

    ranked = sorted((score(c, track) for c in cands.values()), key=lambda c: c.score, reverse=True)
    return ranked
