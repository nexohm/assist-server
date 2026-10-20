"""Punto di avvio del server installatore.

    uvicorn app.main:app --host 0.0.0.0 --port 8080
"""
import asyncio
import os
import secrets
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from . import agent_api, db, notify, web

VERSION = os.environ.get("SUITE_VERSION", "dev")


def secret_key() -> str:
    """Chiave per firmare i cookie di sessione: da variabile d'ambiente o generata e salvata in /data."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(db.DATA_DIR, "secret.key")
    os.makedirs(db.DATA_DIR, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_urlsafe(48))
        os.chmod(path, 0o600)
    with open(path) as f:
        return f.read().strip()


db.migrate()
web.templates.env.globals["VERSION"] = VERSION

@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(notify.watcher())  # controllo impianti offline/critici per le notifiche
    try:
        yield
    finally:
        task.cancel()


app = Starlette(
    lifespan=lifespan,
    routes=[
        *agent_api.routes,
        *web.routes,
        Mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static"),
    ],
    middleware=[
        Middleware(
            SessionMiddleware,
            secret_key=secret_key(),
            session_cookie="suite_session",
            max_age=60 * 60 * 12,
            same_site="lax",
            https_only=os.environ.get("SECURE_COOKIES", "1") == "1",
        )
    ],
)
