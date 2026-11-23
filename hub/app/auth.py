"""Вход по паролю для доступа из интернета.

Если задана переменная HUB_PASSWORD, все страницы и API закрыты формой входа.
После входа выдаётся подписанная cookie на 30 дней. Без HUB_PASSWORD защиты нет —
так и задумано для работы только в домашней сети.
"""
import hashlib
import hmac
import os
import time
from collections import defaultdict

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

PASSWORD = os.environ.get("HUB_PASSWORD", "")
COOKIE = "hub_session"
MAX_AGE = 30 * 86400
# секрет для подписи: производный от пароля (смена пароля разлогинивает всех)
_KEY = hashlib.sha256(("hub-session:" + PASSWORD).encode()).digest()

_fails: dict[str, list[float]] = defaultdict(list)

LOGIN_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Вход</title>
<style>:root{color-scheme:dark}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0f0f12;color:#f2f2f5;
font:16px system-ui,sans-serif}form{width:min(320px,86vw);display:flex;flex-direction:column;gap:12px}
h1{font-size:22px;margin:0 0 4px}input,button{font:inherit;padding:12px 14px;border-radius:10px;border:0}
input{background:#222229;color:#f2f2f5}button{background:#ff4d6d;color:#fff;font-weight:600;cursor:pointer}
.err{color:#ffb547;font-size:14px;min-height:1.2em}</style></head><body>
<form method="post" action="/login"><h1>Плеер</h1>
<input type="password" name="password" placeholder="Пароль" autofocus autocomplete="current-password" required>
<button>Войти</button><div class="err">__ERR__</div></form></body></html>"""


def enabled() -> bool:
    return bool(PASSWORD)


def _sign(expires: int) -> str:
    sig = hmac.new(_KEY, str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def valid_cookie(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    exp, _, sig = value.partition(".")
    try:
        expires = int(exp)
    except ValueError:
        return False
    return expires > time.time() and hmac.compare_digest(value, _sign(expires))


def _client_ip(request: Request) -> str:
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


def _blocked(ip: str) -> bool:
    now = time.time()
    _fails[ip] = [t for t in _fails[ip] if now - t < 900]
    return len(_fails[ip]) >= 5


async def middleware(request: Request, call_next):
    if not PASSWORD:
        return await call_next(request)
    path = request.url.path
    if path.startswith("/worker/") and os.environ.get("WORKER_TOKEN"):
        return await call_next(request)  # свой отдельный вход: токен воркера
    if path == "/login" and request.method == "POST":
        return await _login(request)
    if valid_cookie(request.cookies.get(COOKIE)):
        if path == "/logout":
            r = RedirectResponse("/", status_code=303)
            r.delete_cookie(COOKIE)
            return r
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "требуется вход"}, status_code=401)
    return HTMLResponse(LOGIN_HTML.replace("__ERR__", ""), status_code=401)


async def _login(request: Request):
    ip = _client_ip(request)
    if _blocked(ip):
        return HTMLResponse(LOGIN_HTML.replace("__ERR__", "Слишком много попыток, подождите 15 минут"), status_code=429)
    form = (await request.body()).decode()
    from urllib.parse import parse_qs
    given = parse_qs(form).get("password", [""])[0]
    if hmac.compare_digest(given.encode(), PASSWORD.encode()):
        _fails.pop(ip, None)
        secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
        r = RedirectResponse("/", status_code=303)
        r.set_cookie(COOKIE, _sign(int(time.time()) + MAX_AGE), max_age=MAX_AGE, httponly=True,
                     samesite="lax", secure=secure)
        return r
    _fails[ip].append(time.time())
    await __import__("asyncio").sleep(1)
    return HTMLResponse(LOGIN_HTML.replace("__ERR__", "Неверный пароль"), status_code=401)
