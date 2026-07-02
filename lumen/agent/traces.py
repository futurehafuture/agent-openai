"""JSONL trace files for model runs."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agents import Agent

from ..config import AppConfig
from ..logging_setup import get_logger

logger = get_logger(__name__)

_MAX_TRACE_STRING = 200_000
_TRACE_DIRNAME = "model-traces"
_TRACE_INDEX_FILENAME = "sessions.json"


class ModelTrace:
    """Collect model-run events and append one JSONL record when the run ends."""

    def __init__(
        self,
        *,
        path: Path | None,
        run_id: str,
        created_at: str,
        config: AppConfig,
        user_input: str,
        session_id: str | None,
        agent: Agent | None,
    ) -> None:
        self.path = path
        self._finished = False
        self._raw_event_counts: Counter[str] = Counter()
        self._assistant_parts: list[str] = []
        self._reasoning_parts: list[str] = []
        self._agents: list[dict[str, Any]] = []
        self._errors: list[dict[str, Any]] = []
        self._tool_calls: dict[str, dict[str, Any]] = {}
        self._tool_aliases: dict[str, str] = {}
        self._tool_order: list[str] = []
        self._data: dict[str, Any] = {
            "schema_version": 1,
            "run_id": run_id,
            "session_id": session_id,
            "created_at": created_at,
            "finished_at": None,
            "status": "running",
            "request": {
                "user_input": user_input,
                "model": config.model,
                "base_url": config.base_url or "openai-default",
                "provider": config.active_provider.name if config.active_provider else None,
                "max_turns": config.max_turns,
                "workspace": str(config.workspace_dir),
                "agent": getattr(agent, "name", None),
                "tools": _tool_names(agent),
            },
            "raw_event_counts": {},
            "response": {
                "reasoning": "",
                "assistant": "",
                "tool_calls": [],
                "agents": [],
                "errors": [],
            },
            "usage": None,
            "error": None,
        }
        # Keep the trace in memory while the model streams. The JSONL file is
        # created only in finish(), so the file panel never shows an in-progress
        # trace growing token by token.

    @classmethod
    def start(
        cls,
        *,
        config: AppConfig,
        user_input: str,
        session_id: str | None = None,
        agent: Agent | None = None,
    ) -> ModelTrace:
        run_id = uuid.uuid4().hex
        created_at = _now()
        try:
            trace_dir = config.settings_dir / _TRACE_DIRNAME
            trace_dir.mkdir(parents=True, exist_ok=True)
            path = _trace_path(trace_dir, session_id, created_at)
        except OSError as exc:
            logger.warning("Could not create model trace directory: %s", exc)
            path = None
        return cls(
            path=path,
            run_id=run_id,
            created_at=created_at,
            config=config,
            user_input=user_input,
            session_id=session_id,
            agent=agent,
        )

    @property
    def trace_path(self) -> str | None:
        return str(self.path) if self.path else None

    @property
    def created_at(self) -> str:
        return str(self._data["created_at"])

    def record_raw_event(self, event: Any) -> None:
        try:
            self._raw_event_counts[str(getattr(event, "type", type(event).__name__))] += 1
        except Exception as exc:  # noqa: BLE001 - tracing must never break a run
            logger.warning("Could not record raw trace event: %s", exc)

    def record_normalized_event(self, event: dict[str, Any]) -> None:
        try:
            etype = event.get("type")
            if etype == "token":
                self._assistant_parts.append(str(event.get("delta", "")))
            elif etype == "reasoning":
                self._reasoning_parts.append(str(event.get("delta", "")))
            elif etype in {"tool_call", "tool_call_update"}:
                self._record_tool_call(event)
            elif etype == "tool_output":
                self._record_tool_output(event)
            elif etype == "agent":
                self._agents.append({"name": event.get("name"), "timestamp": _now()})
            elif etype == "error":
                self._errors.append(_to_jsonable(event))
        except Exception as exc:  # noqa: BLE001 - tracing must never break a run
            logger.warning("Could not record normalized trace event: %s", exc)

    def finish(
        self,
        status: str,
        *,
        usage: dict[str, int] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        if self._finished:
            return
        self._finished = True
        self._data["status"] = status
        self._data["finished_at"] = _now()
        self._data["raw_event_counts"] = dict(self._raw_event_counts)
        self._data["response"] = self._response()
        self._data["usage"] = usage
        self._data["error"] = _to_jsonable(error) if error else None
        self._write()

    def _record_tool_call(self, event: dict[str, Any]) -> None:
        event_key = _tool_key(event)
        key = self._tool_aliases.get(event_key, event_key) if event_key else ""
        key = key or f"tool-{len(self._tool_order) + 1}"
        tool = self._ensure_tool(key)
        if event.get("id"):
            tool["id"] = event["id"]
            self._tool_aliases[str(event["id"])] = key
        if event.get("call_id"):
            tool["call_id"] = event["call_id"]
            self._tool_aliases[str(event["call_id"])] = key
        if event.get("name"):
            tool["name"] = event["name"]
        if "arguments" in event:
            tool["arguments"] = _parse_jsonish(event["arguments"])

    def _record_tool_output(self, event: dict[str, Any]) -> None:
        event_key = _tool_key(event)
        key = self._tool_aliases.get(event_key, event_key) if event_key else ""
        if not key:
            key = _matching_tool_key(event.get("name"), self._tool_order, self._tool_calls)
        key = key or f"tool-{len(self._tool_order) + 1}"
        tool = self._ensure_tool(key)
        if event.get("call_id"):
            tool["call_id"] = event["call_id"]
            self._tool_aliases[str(event["call_id"])] = key
        if event.get("name"):
            tool["name"] = event["name"]
        tool["output"] = event.get("output", "")
        tool["finished_at"] = _now()

    def _ensure_tool(self, key: str) -> dict[str, Any]:
        if key not in self._tool_calls:
            self._tool_calls[key] = {
                "id": None,
                "call_id": None,
                "name": "tool",
                "arguments": "",
                "output": "",
                "started_at": _now(),
                "finished_at": None,
            }
            self._tool_order.append(key)
        return self._tool_calls[key]

    def _response(self) -> dict[str, Any]:
        return {
            "reasoning": "".join(self._reasoning_parts),
            "assistant": "".join(self._assistant_parts),
            "tool_calls": [_to_jsonable(self._tool_calls[key]) for key in self._tool_order],
            "agents": self._agents,
            "errors": self._errors,
        }

    def _write(self) -> None:
        if self.path is None:
            return
        try:
            record = json.dumps(self._data, ensure_ascii=False, separators=(",", ":"))
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(record + "\n")
        except Exception as exc:  # noqa: BLE001 - tracing must never break a run
            logger.warning("Could not write model trace %s: %s", self.path, exc)
            self.path = None


def _tool_names(agent: Agent | None) -> list[str]:
    if agent is None:
        return []
    return sorted(str(getattr(tool, "name", type(tool).__name__)) for tool in getattr(agent, "tools", []))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _trace_path(trace_dir: Path, session_id: str | None, created_at: str) -> Path:
    if session_id is None:
        return trace_dir / f"{_stamp(created_at)}.jsonl"

    index_path = trace_dir / _TRACE_INDEX_FILENAME
    index = _read_trace_index(index_path)
    session_record = index.get(session_id)
    if isinstance(session_record, dict) and isinstance(session_record.get("filename"), str):
        return trace_dir / session_record["filename"]

    filename = f"{_stamp(created_at)}.jsonl"
    index[session_id] = {"filename": filename, "first_request_at": created_at}
    _write_trace_index(index_path, index)
    return trace_dir / filename


def _read_trace_index(index_path: Path) -> dict[str, dict[str, str]]:
    if not index_path.exists():
        return {}
    try:
        raw = json.loads(index_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read trace session index (%s); starting fresh.", exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    index: dict[str, dict[str, str]] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        filename = value.get("filename")
        first_request_at = value.get("first_request_at")
        if isinstance(filename, str) and isinstance(first_request_at, str):
            index[str(key)] = {
                "filename": filename,
                "first_request_at": first_request_at,
            }
    return index


def _write_trace_index(index_path: Path, index: dict[str, dict[str, str]]) -> None:
    try:
        index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write trace session index: %s", exc)


def _stamp(timestamp: str) -> str:
    return (
        timestamp.replace(":", "")
        .replace("-", "")
        .replace(".", "")
        .replace("+", "")
    )


def _tool_key(event: dict[str, Any]) -> str:
    return str(event.get("id") or event.get("call_id") or "")


def _matching_tool_key(name: Any, order: list[str], calls: dict[str, dict[str, Any]]) -> str:
    for key in reversed(order):
        tool = calls[key]
        if tool.get("name") == name and not tool.get("output"):
            return key
    return ""


def _parse_jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return _to_jsonable(value)
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return _trim(value)


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _trim(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(v) for v in value]
    if hasattr(value, "model_dump"):
        try:
            return _to_jsonable(value.model_dump(mode="json", exclude_none=True))
        except Exception:  # noqa: BLE001
            try:
                return _to_jsonable(value.model_dump(exclude_none=True))
            except Exception:  # noqa: BLE001
                return _trim(str(value))
    if is_dataclass(value):
        return {field.name: _to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, SimpleNamespace):
        return _to_jsonable(vars(value))
    if hasattr(value, "__dict__"):
        return _to_jsonable(vars(value))
    return _trim(str(value))


def _trim(text: str) -> str:
    if len(text) <= _MAX_TRACE_STRING:
        return text
    return text[:_MAX_TRACE_STRING] + f"... [truncated {len(text) - _MAX_TRACE_STRING} chars]"


__all__ = ["ModelTrace"]
