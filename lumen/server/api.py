"""FastAPI application: REST endpoints + the SSE chat-streaming endpoint."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
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
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
    "Pragma": "no-cache",
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
        if "workspace" in values:
            try:
                values["workspace"] = str(workspace.validate_project_root(values["workspace"]))
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
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
        raw_path = Path(body.path).expanduser()
        if raw_path.is_absolute():
            path = raw_path.resolve()
            try:
                path.relative_to(workspace.output_dir)
            except ValueError as exc:
                raise HTTPException(403, "Can only open files inside the project folder.") from exc
        else:
            path = _resolve_workspace_path(body.path)
        if not path.exists():
            raise HTTPException(404, "File not found.")
        _os_open(path)
        return {"status": "opened", "path": str(path)}

    @app.post("/api/workspace/choose")
    def choose_workspace_folder() -> dict[str, str]:
        path = _choose_folder()
        if path is None:
            raise HTTPException(400, "No folder selected.")
        try:
            resolved = workspace.validate_project_root(path)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"path": str(resolved)}

    @app.get("/api/workspace/files")
    def workspace_files() -> dict[str, Any]:
        root = workspace.output_dir
        return {
            "root": str(root),
            "files": _list_workspace_files(root),
        }

    @app.get("/api/workspace/preview")
    def workspace_preview(path: str) -> dict[str, Any]:
        target = _resolve_workspace_path(path)
        if not target.is_file():
            raise HTTPException(400, "Not a file.")

        size = target.stat().st_size
        suffix = target.suffix.lower()
        rel_path = _workspace_rel(target)
        raw_url = f"/api/workspace/raw?path={quote(rel_path)}"

        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
            return {
                "path": rel_path,
                "name": target.name,
                "kind": "image",
                "size": size,
                "raw_url": raw_url,
            }
        if suffix == ".pdf":
            return {
                "path": rel_path,
                "name": target.name,
                "kind": "pdf",
                "size": size,
                "raw_url": raw_url,
            }
        if suffix in {".txt", ".md", ".json", ".csv", ".tsv", ".py", ".js", ".css", ".html", ".xml", ".yaml", ".yml"}:
            if size > 512_000:
                return {
                    "path": rel_path,
                    "name": target.name,
                    "kind": "too_large",
                    "size": size,
                    "message": "File is too large to preview.",
                }
            text = target.read_text(encoding="utf-8", errors="replace")
            return {
                "path": rel_path,
                "name": target.name,
                "kind": "text",
                "size": size,
                "text": text,
            }
        return {
            "path": rel_path,
            "name": target.name,
            "kind": "unsupported",
            "size": size,
            "message": "Preview is not available for this file type.",
        }

    @app.get("/api/workspace/raw")
    def workspace_raw(path: str) -> Response:
        target = _resolve_workspace_path(path)
        if not target.is_file():
            raise HTTPException(400, "Not a file.")
        return FileResponse(target)

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

    # Repair orphaned tool-call sequences left by interrupted runs.
    await state.sessions.repair_if_needed(session_id)

    session = state.sessions.get_session(session_id)
    try:
        events = stream_agent_run(state.agent, req.message, session, state.config, session_id=session_id)
        async for frame in _batched_sse(events):
            yield frame
    except Exception as exc:  # noqa: BLE001 - terminal safety net for the SSE stream
        msg = str(exc)
        # DeepSeek rejects sessions where the SDK's internal item storage
        # format doesn't round-trip cleanly into Chat Completions tool_calls.
        # When this happens, clear the corrupted session and retry once.
        if "tool_calls" in msg and "tool messages" in msg:
            logger.warning("Session %s has incompatible tool-call history; resetting.", session_id)
            await state.sessions.repair_if_needed(session_id)
            # Also force-clear the underlying session storage.
            try:
                await session.clear_session()
            except Exception:  # noqa: BLE001
                pass
            # Create a fresh session for the retry.
            new_id = state.sessions.create()["id"]
            state.sessions.touch(new_id, req.message)
            record2 = state.sessions.ensure(new_id)
            yield _sse({"type": "session", "id": new_id, "title": record2["title"]})
            fresh_session = state.sessions.get_session(new_id)
            try:
                retry_events = stream_agent_run(
                    state.agent,
                    req.message,
                    fresh_session,
                    state.config,
                    session_id=new_id,
                )
                async for frame in _batched_sse(retry_events):
                    yield frame
            except Exception as exc2:  # noqa: BLE001
                logger.exception("Chat stream retry also failed")
                yield _sse({"type": "error", "kind": "stream", "message": str(exc2)})
            return

        logger.exception("Chat stream failed")
        yield _sse({"type": "error", "kind": "stream", "message": str(exc)})


async def _batched_sse(
    events: AsyncIterator[dict[str, Any]],
    batch_window: float = 0.016,
    max_batch: int = 8,
) -> AsyncIterator[str]:
    """Buffer SSE events briefly so each TCP write is large enough that
    Nagle's algorithm doesn't stall delivery of tiny (~40 B) frames.

    ``batch_window`` is ~16 ms (one 60 fps frame) — imperceptible latency.
    """
    import asyncio

    batch: list[str] = []
    deadline = 0.0
    iterator = events.__aiter__()
    loop = asyncio.get_running_loop()
    pending: asyncio.Task[dict[str, Any]] | None = None

    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(anext(iterator))

            if not batch:
                try:
                    event_dict = await pending
                except StopAsyncIteration:
                    break
                pending = None
                batch.append(_sse(event_dict))
                deadline = loop.time() + batch_window
            else:
                remaining = max(0.0, deadline - loop.time())
                done, _ = await asyncio.wait({pending}, timeout=remaining)
                if not done:
                    yield "".join(batch)
                    batch.clear()
                    deadline = 0.0
                    continue

                try:
                    event_dict = pending.result()
                except StopAsyncIteration:
                    yield "".join(batch)
                    break
                pending = None
                batch.append(_sse(event_dict))

            if len(batch) >= max_batch:
                yield "".join(batch)
                batch.clear()
                deadline = 0.0
    finally:
        if pending is not None and not pending.done():
            pending.cancel()


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


def _resolve_workspace_path(path: str) -> Path:
    target = (workspace.output_dir / path).resolve()
    try:
        target.relative_to(workspace.output_dir)
    except ValueError as exc:
        raise HTTPException(403, "Can only access files inside the project folder.") from exc
    if not target.exists():
        raise HTTPException(404, "File not found.")
    return target


def _workspace_rel(path: Path) -> str:
    return path.resolve().relative_to(workspace.output_dir).as_posix()


def _list_workspace_files(root: Path, max_entries: int = 500, max_depth: int = 5) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    def walk(directory: Path, depth: int) -> None:
        if len(entries) >= max_entries or depth > max_depth:
            return
        try:
            children = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        for child in children:
            if len(entries) >= max_entries:
                return
            if child.name.startswith("."):
                continue
            resolved = child.resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            try:
                stat = resolved.stat()
            except OSError:
                continue
            is_dir = resolved.is_dir()
            entries.append(
                {
                    "path": _workspace_rel(resolved),
                    "name": resolved.name,
                    "kind": "directory" if is_dir else "file",
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "depth": depth,
                    "ext": resolved.suffix.lower(),
                }
            )
            if is_dir:
                walk(resolved, depth + 1)

    walk(root, 0)
    return entries


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


def _choose_folder() -> Path | None:
    try:
        if sys.platform == "darwin":
            script = 'POSIX path of (choose folder with prompt "Choose a Lumen project folder")'
            proc = subprocess.run(
                ["osascript", "-e", script],
                check=False,
                capture_output=True,
                text=True,
            )
            selected = proc.stdout.strip()
            return Path(selected) if proc.returncode == 0 and selected else None
        if sys.platform.startswith("win"):
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            selected = filedialog.askdirectory(title="Choose a Lumen project folder")
            root.destroy()
            return Path(selected) if selected else None

        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        selected = filedialog.askdirectory(title="Choose a Lumen project folder")
        root.destroy()
        return Path(selected) if selected else None
    except Exception as exc:  # noqa: BLE001 - folder pickers are platform best-effort
        logger.warning("Could not choose workspace folder: %s", exc)
        return None


__all__ = ["create_app"]
