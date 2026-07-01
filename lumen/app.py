"""Lumen desktop entry point.

Starts the FastAPI server on a background thread and opens a native window
(PyWebView) pointed at it. The server and window share a single :class:`AppState`.

Run modes:
* ``lumen``               — desktop window (default).
* ``lumen --server``      — headless; just serve HTTP (useful for dev / browsers).
"""

from __future__ import annotations

import contextlib
import socket
import sys
import threading
import time

import uvicorn
from dotenv import load_dotenv

from . import APP_NAME, APP_TAGLINE, __version__
from .logging_setup import configure_logging, get_logger
from .server import AppState, create_app

logger = get_logger(__name__)

WINDOW_WIDTH = 1240
WINDOW_HEIGHT = 840
MIN_SIZE = (920, 640)


def _find_free_port(host: str, preferred: int) -> int:
    """Return ``preferred`` if free, otherwise the next available port."""
    for candidate in [preferred, *range(preferred + 1, preferred + 50)]:
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if sock.connect_ex((host, candidate)) != 0:
                return candidate
    # Last resort: let the OS choose.
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def _start_server(server: uvicorn.Server) -> threading.Thread:
    thread = threading.Thread(target=server.run, name="lumen-server", daemon=True)
    thread.start()
    return thread


def _wait_until_ready(server: uvicorn.Server, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if server.started:
            return True
        time.sleep(0.05)
    return False


def main() -> int:
    """Application entry point."""
    load_dotenv()  # read .env from the working directory, if present
    headless = "--server" in sys.argv

    state = AppState.create()
    configure_logging(state.config.log_level)
    app = create_app(state)

    host = state.config.host
    port = _find_free_port(host, state.config.port)
    url = f"http://{host}:{port}"

    uconfig = uvicorn.Config(app, host=host, port=port, log_level="warning", loop="asyncio")
    server = uvicorn.Server(uconfig)

    if headless:
        logger.info("%s %s — serving headless at %s", APP_NAME, __version__, url)
        server.run()
        return 0

    _start_server(server)
    if not _wait_until_ready(server):
        logger.error("Server did not start in time; exiting.")
        return 1
    logger.info("%s %s ready at %s", APP_NAME, __version__, url)

    try:
        import webview  # imported lazily so headless mode needs no GUI backend
    except ImportError:
        logger.error(
            "pywebview is not installed. Run `uv sync`, or use `lumen --server` "
            "and open %s in a browser.",
            url,
        )
        return 1

    webview.create_window(
        f"{APP_NAME} — {APP_TAGLINE}",
        url=url,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=MIN_SIZE,
        background_color="#FAFBFC",
    )
    try:
        webview.start()  # blocks on the main thread until the window closes
    finally:
        logger.info("Window closed; shutting down server.")
        server.should_exit = True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
