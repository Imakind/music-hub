"""Локальная библиотека: какие треки скачаны, откуда и где лежат."""
import json
import sqlite3
import threading
import time

from .config import DB_PATH

_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS tracks (
        id INTEGER PRIMARY KEY,          -- Deezer track id
        status TEXT NOT NULL,            -- queued | resolving | downloading | ready | error
        meta TEXT NOT NULL,              -- JSON трека из каталога
        path TEXT,
        source_id TEXT,
        source_title TEXT,
        source_score REAL,
        candidates TEXT,                 -- JSON кандидатов
        error TEXT,
        added_at REAL,
        updated_at REAL
    );
    CREATE INDEX IF NOT EXISTS tracks_status ON tracks(status);

    -- Действия пользователя: complete | like | unlike | skip | dislike | undislike | play
    CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        track_id INTEGER NOT NULL,
        artist_id INTEGER,
        type TEXT NOT NULL,
        position REAL,
        ts REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS events_track ON events(track_id);

    -- Текущие оценки: 1 = лайк, -1 = «не нравится»
    CREATE TABLE IF NOT EXISTS ratings (track_id INTEGER PRIMARY KEY, value INTEGER NOT NULL, ts REAL);

    -- Кэш метаданных каталога для всех треков, которые встречались рекомендациям
    CREATE TABLE IF NOT EXISTS catalog (id INTEGER PRIMARY KEY, meta TEXT NOT NULL, ts REAL);

    -- Аудио-эмбеддинги (float32, нормированные)
    CREATE TABLE IF NOT EXISTS embeddings (id INTEGER PRIMARY KEY, model TEXT NOT NULL, vec BLOB NOT NULL, ts REAL);

    -- Сгенерированные плейлисты (Микс дня)
    CREATE TABLE IF NOT EXISTS playlists (name TEXT PRIMARY KEY, data TEXT NOT NULL, ts REAL);
    """
)


_conn.executescript(
    """
    -- Задачи для домашнего воркера (режим server): fetch | resolve | embed
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        track_id INTEGER NOT NULL,
        meta TEXT NOT NULL,
        video_id TEXT,
        state TEXT NOT NULL,             -- pending | running | done | error
        error TEXT,
        created REAL, updated REAL
    );
    CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, kind);
    """
)


# ---------- задачи воркера ----------

def _migrate():
    tcols = {r["name"] for r in _conn.execute("PRAGMA table_info(tracks)")}
    if "kept" not in tcols:
        # kept=1: трек в «Моей музыке» (слушали, лайкнули, скачали вручную). kept=0: просто кэш.
        _conn.execute("ALTER TABLE tracks ADD COLUMN kept INTEGER NOT NULL DEFAULT 0")
        _conn.execute("UPDATE tracks SET kept=1 WHERE status='ready'")  # всё, что было до кэша, оставляем
    jcols = {r["name"] for r in _conn.execute("PRAGMA table_info(jobs)")}
    if "priority" not in jcols:
        _conn.execute("ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 1")
    _conn.commit()


_migrate()

# Приоритеты (меньше — раньше): 0 — пользователь ждёт, 1 — следующие в очереди,
# 2 — фоновый прогрев микса, 3 — векторы звучания
DEFAULT_PRIORITY = {"resolve": 0, "fetch": 1, "embed": 3}


def add_job(kind: str, track_id: int, meta: dict, video_id: str | None = None,
            priority: int | None = None) -> int:
    now = time.time()
    if priority is None:
        priority = DEFAULT_PRIORITY.get(kind, 1)
    with _lock:
        row = _conn.execute(
            "SELECT id, video_id, state, priority FROM jobs WHERE kind=? AND track_id=? "
            "AND state IN ('pending','running') ORDER BY id DESC LIMIT 1", (kind, track_id)).fetchone()
        if row and (row["video_id"] == video_id or (row["state"] == "pending" and kind == "fetch")):
            if row["state"] == "pending":
                sets, args = [], []
                if priority < row["priority"]:  # трек ждут срочнее, чем планировали
                    sets.append("priority=?"); args.append(priority)
                if video_id and row["video_id"] != video_id:
                    sets.append("video_id=?"); args.append(video_id)
                if sets:
                    _conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", (*args, row["id"]))
                    _conn.commit()
            return row["id"]
        cur = _conn.execute(
            "INSERT INTO jobs(kind, track_id, meta, video_id, state, created, updated, priority) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (kind, track_id, json.dumps(meta, ensure_ascii=False), video_id, "pending", now, now, priority))
        _conn.commit()
        return cur.lastrowid


def job_failures(track_id: int) -> int:
    with _lock:
        return _conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='fetch' AND track_id=? AND state='error'",
                             (track_id,)).fetchone()[0]


def set_kept(track_id: int):
    with _lock:
        _conn.execute("UPDATE tracks SET kept=1 WHERE id=?", (track_id,))
        _conn.commit()


def all_ready():
    with _lock:
        rows = _conn.execute("SELECT * FROM tracks WHERE status='ready' ORDER BY added_at DESC").fetchall()
    return [_row(r) for r in rows]


def kept_tracks():
    return [r for r in all_ready() if r["kept"]]


def claim_jobs(kinds: list[str], n: int, lease: float = 600) -> list[dict]:
    """Выдать воркеру до n задач. Зависшие (lease секунд без отчёта) выдаются повторно."""
    now = time.time()
    if not kinds:
        return []
    q = ",".join("?" * len(kinds))
    with _lock:
        rows = _conn.execute(
            f"SELECT * FROM jobs WHERE kind IN ({q}) AND (state='pending' OR (state='running' AND updated<?)) "
            "ORDER BY priority, CASE kind WHEN 'fetch' THEN 0 WHEN 'resolve' THEN 1 ELSE 2 END, id LIMIT ?",
            (*kinds, now - lease, n)).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            _conn.execute(f"UPDATE jobs SET state='running', updated=? WHERE id IN ({','.join('?' * len(ids))})",
                          (now, *ids))
            _conn.commit()
    out = []
    for r in rows:
        d = dict(r)
        d["meta"] = json.loads(d["meta"])
        out.append(d)
    return out


def get_job(job_id: int):
    with _lock:
        r = _conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["meta"] = json.loads(d["meta"])
    return d


def finish_job(job_id: int, state: str = "done", error: str | None = None):
    with _lock:
        _conn.execute("UPDATE jobs SET state=?, error=?, updated=? WHERE id=?", (state, error, time.time(), job_id))
        _conn.commit()


def touch_job(job_id: int):
    with _lock:
        _conn.execute("UPDATE jobs SET updated=? WHERE id=?", (time.time(), job_id))
        _conn.commit()


def pending_jobs() -> dict[str, int]:
    with _lock:
        rows = _conn.execute(
            "SELECT kind, COUNT(*) c FROM jobs WHERE state IN ('pending','running') GROUP BY kind").fetchall()
    return {r["kind"]: r["c"] for r in rows}


def release_running_jobs() -> int:
    """Воркер перезапустился: всё, что он делал, возвращаем в очередь сразу, без ожидания lease."""
    with _lock:
        n = _conn.execute("UPDATE jobs SET state='pending' WHERE state='running'").rowcount
        _conn.execute("UPDATE tracks SET status='queued' WHERE status IN ('resolving','downloading')")
        _conn.commit()
    return n


def reset_stuck_jobs():
    """Задачи, которые сервер считал выполняемыми до перезапуска, вернуть в очередь."""
    with _lock:
        _conn.execute("UPDATE jobs SET state='pending' WHERE state='running'")
        _conn.commit()


def local_path(row) -> "Path | None":
    """Путь к файлу трека. Если база перенесена с другой машины, ищем по хвосту
    «Исполнитель/Альбом/файл» внутри текущей папки музыки."""
    from pathlib import Path, PureWindowsPath
    from .config import MUSIC_DIR
    if not row or not row.get("path"):
        return None
    p = Path(row["path"])
    if p.exists():
        return p
    parts = PureWindowsPath(row["path"]).parts[-3:]
    alt = MUSIC_DIR.joinpath(*parts)
    return alt if alt.exists() else None


# ---------- события и оценки ----------

def add_event(track_id: int, artist_id: int | None, type_: str, position: float | None = None):
    now = time.time()
    with _lock:
        _conn.execute("INSERT INTO events(track_id, artist_id, type, position, ts) VALUES (?,?,?,?,?)",
                      (track_id, artist_id, type_, position, now))
        if type_ in ("like", "dislike"):
            _conn.execute("INSERT OR REPLACE INTO ratings(track_id, value, ts) VALUES (?,?,?)",
                          (track_id, 1 if type_ == "like" else -1, now))
        elif type_ in ("unlike", "undislike"):
            _conn.execute("DELETE FROM ratings WHERE track_id=?", (track_id,))
        _conn.commit()


def events():
    with _lock:
        return [dict(r) for r in _conn.execute("SELECT * FROM events ORDER BY seq").fetchall()]


def ratings() -> dict[int, int]:
    with _lock:
        return {r["track_id"]: r["value"] for r in _conn.execute("SELECT * FROM ratings").fetchall()}


# ---------- кэш каталога ----------

def cache_meta(tracks: list[dict]):
    now = time.time()
    with _lock:
        _conn.executemany("INSERT OR REPLACE INTO catalog(id, meta, ts) VALUES (?,?,?)",
                          [(t["id"], json.dumps(t, ensure_ascii=False), now) for t in tracks])
        _conn.commit()


def cached_meta(ids: list[int]) -> dict[int, dict]:
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ",".join("?" * len(chunk))
        with _lock:
            for r in _conn.execute(f"SELECT id, meta FROM catalog WHERE id IN ({q})", chunk).fetchall():
                out[r["id"]] = json.loads(r["meta"])
            for r in _conn.execute(f"SELECT id, meta FROM tracks WHERE id IN ({q})", chunk).fetchall():
                m = json.loads(r["meta"])
                if m:
                    out.setdefault(r["id"], m)
    return out


# ---------- эмбеддинги ----------

def get_embeddings(ids: list[int], model: str) -> dict[int, bytes]:
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ",".join("?" * len(chunk))
        with _lock:
            for r in _conn.execute(f"SELECT id, vec FROM embeddings WHERE model=? AND id IN ({q})", (model, *chunk)):
                out[r["id"]] = r["vec"]
    return out


def put_embedding(track_id: int, model: str, vec: bytes):
    with _lock:
        _conn.execute("INSERT OR REPLACE INTO embeddings(id, model, vec, ts) VALUES (?,?,?,?)",
                      (track_id, model, vec, time.time()))
        _conn.commit()


# ---------- плейлисты ----------

def save_playlist(name: str, data: dict):
    with _lock:
        _conn.execute("INSERT OR REPLACE INTO playlists(name, data, ts) VALUES (?,?,?)",
                      (name, json.dumps(data, ensure_ascii=False), time.time()))
        _conn.commit()


def get_playlist(name: str):
    with _lock:
        r = _conn.execute("SELECT data, ts FROM playlists WHERE name=?", (name,)).fetchone()
    return {**json.loads(r["data"]), "ts": r["ts"]} if r else None


def _row(r):
    if r is None:
        return None
    d = dict(r)
    d["meta"] = json.loads(d["meta"])
    d["candidates"] = json.loads(d["candidates"]) if d["candidates"] else None
    return d


def get(track_id: int):
    with _lock:
        return _row(_conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone())


def statuses(ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    with _lock:
        rows = _conn.execute(f"SELECT id, status FROM tracks WHERE id IN ({q})", ids).fetchall()
    return {r["id"]: r["status"] for r in rows}


def upsert(track_id: int, meta: dict | None = None, **fields):
    now = time.time()
    with _lock:
        exists = _conn.execute("SELECT 1 FROM tracks WHERE id=?", (track_id,)).fetchone()
        if not exists:
            _conn.execute(
                "INSERT INTO tracks(id, status, meta, added_at, updated_at) VALUES (?,?,?,?,?)",
                (track_id, fields.pop("status", "queued"), json.dumps(meta or {}, ensure_ascii=False), now, now),
            )
        elif meta is not None:
            fields["meta"] = json.dumps(meta, ensure_ascii=False)
        if "candidates" in fields and fields["candidates"] is not None:
            fields["candidates"] = json.dumps(fields["candidates"], ensure_ascii=False)
        if fields:
            fields["updated_at"] = now
            cols = ", ".join(f"{k}=?" for k in fields)
            _conn.execute(f"UPDATE tracks SET {cols} WHERE id=?", (*fields.values(), track_id))
        _conn.commit()


def ready_tracks():
    with _lock:
        rows = _conn.execute("SELECT * FROM tracks WHERE status='ready' ORDER BY added_at DESC").fetchall()
    return [_row(r) for r in rows]


def delete(track_id: int):
    with _lock:
        _conn.execute("DELETE FROM tracks WHERE id=?", (track_id,))
        _conn.commit()


def reset_stuck():
    """После рестарта незавершённые задачи считаем упавшими."""
    with _lock:
        _conn.execute(
            "UPDATE tracks SET status='error', error='прервано перезапуском' "
            "WHERE status IN ('queued','resolving','downloading')"
        )
        _conn.commit()
