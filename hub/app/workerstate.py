"""Жив ли домашний воркер: он опрашивает сервер каждые несколько секунд."""
import time

_last_seen = 0.0
ONLINE_WINDOW = 90


def seen():
    global _last_seen
    _last_seen = time.time()


def online() -> bool:
    return time.time() - _last_seen < ONLINE_WINDOW
