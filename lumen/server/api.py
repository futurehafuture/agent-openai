"""FastAPI application: REST endpoints + the SSE chat-streaming endpoint."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..agent import stream_agent_run
from ..logging_setup import get_logger
from ..mcp import mcp_config_path, mcp_public_view
from ..tools import CATEGORY_LABELS, get_tool_specs
from ..workspace import workspace
from .app_state import AppState
from .schemas import ChatRequest, OpenRequest, RenameRequest, SettingsUpdate

logger = get_logger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


def create_app(state: AppState) -> FastAPI:
    """Build the FastAPI app bound to a shared :class:`AppState`."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ARG001
        await state.startup()
        try:
            yield
        finally:
            await state.shutdown()

    app = FastAPI(title="Lumen", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # local-only server bound to 127.0.0.1
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -- meta ---------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return state.health()

    @app.get("/api/tools")
    def tools() -> list[dict[str, str]]:
        builtin = [
            {
                "name": spec.name,
                "category": spec.category,
                "label": CATEGORY_LABELS.get(spec.category, spec.category.title()),
                "summary": spec.summary,
                "source": "builtin",
            }
            for spec in get_tool_specs()
        ]
        mcp_entries = [
            {
                "name": f"mcp:{srv.name}",
                "category": "mcp",
                "label": "MCP",
                "summary": f"Tools from MCP server '{srv.name}' (discovered at runtime)",
                "source": "mcp",
            }
            for srv in state.mcp_servers
        ]
        return builtin + mcp_entries

    @app.get("/api/mcp")
    def mcp_status() -> dict[str, Any]:
        return {
            "config_path": str(mcp_config_path()),
            "configured": mcp_public_view(),
            "connected": [s.name for s in state.mcp_servers],
            "failed": state.mcp_failed_count,
        }

    # -- settings -----------------------------------------------------------

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        view = state.store.public_view()
        view["workspace_resolved"] = str(state.config.workspace_dir)
        return view

    @app.post("/api/settings")
    async def update_settings(update: SettingsUpdate) -> dict[str, Any]:
        values = {k: v for k, v in update.model_dump().items() if v is not None}
        state.store.update(values)
        config = await state.reload()
        view = state.store.public_view()
        view["workspace_resolved"] = str(config.workspace_dir)
        return view

    # -- sessions -----------------------------------------------------------

    @app.get("/api/sessions")
    def list_sessions() -> list[dict[str, Any]]:
        return state.sessions.list()

    @app.post("/api/sessions")
    def create_session() -> dict[str, Any]:
        return state.sessions.create()

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, str]:
        await state.sessions.delete(session_id)
        return {"status": "deleted", "id": session_id}

    @app.post("/api/sessions/{session_id}/rename")
    def rename_session(session_id: str, body: RenameRequest) -> dict[str, Any]:
        return state.sessions.rename(session_id, body.title)

    @app.get("/api/sessions/{session_id}/messages")
    async def session_messages(session_id: str) -> list[dict[str, Any]]:
        session = state.sessions.get_session(session_id)
        items = await session.get_items()
        return _history_to_ui(items)

    # -- workspace ----------------------------------------------------------

    @app.post("/api/open")
    def open_file(body: OpenRequest) -> dict[str, str]:
        path = Path(body.path).expanduser().resolve()
        if not str(path).startswith(str(workspace.output_dir)):
            raise HTTPException(403, "Can only open files inside the workspace.")
        if not path.exists():
            raise HTTPException(404, "File not found.")
        _os_open(path)
        return {"status": "opened", "path": str(path)}

    # -- chat (SSE) ---------------------------------------------------------

    @app.post("/api/chat/stream")
    def chat_stream(req: ChatRequest) -> StreamingResponse:
        return StreamingResponse(
            _chat_events(state, req),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    # -- static UI ----------------------------------------------------------

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        index_file = WEB_DIR / "index.html"
        if not index_file.exists():
            return JSONResponse({"detail": "UI not built"}, status_code=404)
        return FileResponse(index_file)

    return app


async def _chat_events(state: AppState, req: ChatRequest) -> AsyncIterator[str]:
    """Async generator producing SSE frames for one chat turn."""
    if not state.config.has_api_key:
        yield _sse(
            {
                "type": "error",
                "kind": "no_key",
                "message": "Add your OpenAI API key in Settings to start chatting.",
            }
        )
        return

    session_id = req.session_id or state.sessions.create()["id"]
    state.sessions.touch(session_id, req.message)
    record = state.sessions.ensure(session_id)
    yield _sse({"type": "session", "id": session_id, "title": record["title"]})

    session = state.sessions.get_session(session_id)
    try:
        async for event in stream_agent_run(state.agent, req.message, session, state.config):
            yield _sse(event)
    except Exception as exc:  # noqa: BLE001 - terminal safety net for the SSE stream
        logger.exception("Chat stream failed")
        yield _sse({"type": "error", "kind": "stream", "message": str(exc)})


def _history_to_ui(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert stored response items into a simple transcript for the UI."""
    out: list[dict[str, Any]] = []
    for item in items:
        itype = item.get("type")
        role = item.get("role")
        if role == "user":
            out.append({"role": "user", "text": _extract_text(item.get("content"))})
        elif role == "assistant" or itype == "message":
            text = _extract_text(item.get("content"))
            if text:
                out.append({"role": "assistant", "text": text})
        elif itype == "function_call":
            out.append({"role": "tool_call", "name": item.get("name", "tool")})
        elif itype == "function_call_output":
            out.append({"role": "tool_output", "name": "tool"})
    return out


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            c.get("text", "")
            for c in content
            if isinstance(c, dict) and "text" in c
        ]
        return "".join(parts)
    return ""


def _os_open(path: Path) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        elif sys.platform.startswith("win"):
            import os

            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except OSError as exc:
        logger.warning("Could not open %s: %s", path, exc)


__all__ = ["create_app"]
