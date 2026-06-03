"""
FastAPI web server for the Algo Trading Bot browser UI.

Lifecycle
---------
start_web_server_in_thread(port)
  Starts uvicorn in a daemon thread with its own asyncio event loop.
  Returns immediately so the calling thread (main.py) can continue to run
  the console config prompts and then the trading loop.

  The uvicorn thread registers its event loop into web.state as soon as it
  is running, which allows the trading loop thread to safely push log events
  onto WebSocket queues via call_soon_threadsafe().

  A threading.Event (_ready) is set once the server is bound and listening,
  so main.py can wait for it before printing the URL or opening a browser.

Phase 1: read-only endpoints — observe a running session.
Phase 2: POST /api/configure + POST /api/start replace the console prompts.
"""
from __future__ import annotations

import asyncio
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from web import state as web_state

_STATIC_DIR = Path(__file__).parent / "static"
_ready = threading.Event()   # set when uvicorn is bound and accepting


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # register the running loop so trading thread can push events safely
    web_state.set_web_loop(asyncio.get_running_loop())
    _ready.set()              # unblock main thread waiting in start_web_server_in_thread
    yield
    web_state.request_stop()


def _build_app() -> FastAPI:
    from web.routes.config import router as config_router
    from web.routes.session import router as session_router
    from web.routes.ws import router as ws_router

    app = FastAPI(
        title="Algo Trading Bot",
        version="1.0",
        lifespan=_lifespan,
        docs_url="/docs",
        redoc_url=None,
    )

    app.include_router(session_router)
    app.include_router(config_router)
    app.include_router(ws_router)

    if not _STATIC_DIR.exists():
        raise RuntimeError(
            f"[WEB] Static directory not found: {_STATIC_DIR}\n"
            "Ensure _STATIC_DIR is set to the project's web/static/ directory before starting."
        )

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(
            str(_STATIC_DIR / "index.html"),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    return app


async def _serve(port: int) -> None:
    """Async entry point that runs uvicorn and sets _ready once bound."""
    config = uvicorn.Config(
        _build_app(),
        host="0.0.0.0",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    # patch server.startup to set _ready after the socket is bound
    _orig_startup = server.startup

    async def _patched_startup(sockets=None):
        await _orig_startup(sockets=sockets)
        _ready.set()

    server.startup = _patched_startup
    await server.serve()


def _uvicorn_thread(port: int) -> None:
    """Target for the server daemon thread — owns its own event loop."""
    asyncio.run(_serve(port))


def start_web_server_in_thread(port: int = 8080, open_browser: bool = True) -> None:
    """
    Start uvicorn in a daemon thread and return once the server is bound.
    The caller (main.py) can then run config prompts and the trading loop.
    """
    _ready.clear()
    t = threading.Thread(target=_uvicorn_thread, args=(port,), name="web-server", daemon=True)
    t.start()

    # wait up to 15 s for the server to bind before continuing
    if not _ready.wait(timeout=15):
        raise RuntimeError("[WEB] Server did not start within 15 seconds")

    if open_browser:
        def _open():
            import time
            time.sleep(0.4)
            webbrowser.open(f"http://localhost:{port}")
        threading.Thread(target=_open, daemon=True).start()
