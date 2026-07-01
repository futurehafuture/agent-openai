"""Conversation session management.

Wraps the SDK's :class:`SQLiteSession` (which persists message history) and keeps
a small JSON index of session metadata (title, timestamps) so the UI can show a
sidebar of past conversations.
"""

from __future__ import annotations

import builtins
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from agents import SQLiteSession

from ..config import AppConfig
from ..logging_setup import get_logger

logger = get_logger(__name__)

_DEFAULT_TITLE = "New conversation"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class SessionManager:
    """Creates, indexes, and tears down conversation sessions."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._db_path = str(config.sessions_db_path)
        self._index_path: Path = config.settings_dir / "sessions_index.json"
        self._index: dict[str, dict[str, Any]] = {}
        self._load_index()

    # -- index persistence ---------------------------------------------------

    def _load_index(self) -> None:
        if self._index_path.exists():
            try:
                self._index = json.loads(self._index_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not read session index (%s); starting fresh.", exc)
                self._index = {}

    def _save_index(self) -> None:
        try:
            self._index_path.write_text(json.dumps(self._index, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.error("Could not save session index: %s", exc)

    # -- public API ----------------------------------------------------------

    def get_session(self, session_id: str) -> SQLiteSession:
        """Return a persistent SQLiteSession bound to ``session_id``."""
        return SQLiteSession(session_id, db_path=self._db_path)

    def create(self, title: str | None = None) -> dict[str, Any]:
        """Create a new session and return its metadata record."""
        session_id = uuid.uuid4().hex[:12]
        record = {
            "id": session_id,
            "title": title or _DEFAULT_TITLE,
            "created_at": _now(),
            "updated_at": _now(),
        }
        self._index[session_id] = record
        self._save_index()
        logger.info("Created session %s", session_id)
        return record

    def ensure(self, session_id: str) -> dict[str, Any]:
        """Return metadata for a session, creating an index record if missing."""
        if session_id not in self._index:
            self._index[session_id] = {
                "id": session_id,
                "title": _DEFAULT_TITLE,
                "created_at": _now(),
                "updated_at": _now(),
            }
            self._save_index()
        return self._index[session_id]

    def touch(self, session_id: str, first_message: str | None = None) -> None:
        """Update the session's ``updated_at`` and set a title from the first message."""
        record = self.ensure(session_id)
        record["updated_at"] = _now()
        if first_message and record.get("title") in (None, "", _DEFAULT_TITLE):
            record["title"] = _derive_title(first_message)
        self._save_index()

    def rename(self, session_id: str, title: str) -> dict[str, Any]:
        record = self.ensure(session_id)
        record["title"] = title.strip() or _DEFAULT_TITLE
        record["updated_at"] = _now()
        self._save_index()
        return record

    def list(self) -> builtins.list[dict[str, Any]]:
        """All sessions, most recently updated first."""
        return sorted(self._index.values(), key=lambda r: r.get("updated_at", ""), reverse=True)

    async def delete(self, session_id: str) -> None:
        """Delete a session's history and remove it from the index."""
        try:
            await self.get_session(session_id).clear_session()
        except Exception as exc:  # noqa: BLE001 - deletion should be best-effort
            logger.warning("Could not clear session %s history: %s", session_id, exc)
        self._index.pop(session_id, None)
        self._save_index()
        logger.info("Deleted session %s", session_id)


def _derive_title(message: str, max_len: int = 48) -> str:
    text = " ".join(message.strip().split())
    return text[: max_len - 1] + "…" if len(text) > max_len else text or _DEFAULT_TITLE


__all__ = ["SessionManager"]
