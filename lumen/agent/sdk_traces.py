"""Local exporter for OpenAI Agents SDK traces."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agents.tracing import flush_traces, set_trace_processors, set_tracing_disabled
from agents.tracing.processor_interface import TracingExporter, TracingProcessor
from agents.tracing.processors import BatchTraceProcessor, default_processor
from agents.tracing.spans import Span
from agents.tracing.traces import Trace

from ..config import AppConfig
from ..logging_setup import get_logger

logger = get_logger(__name__)

_TRACE_DIRNAME = "model-traces"
_SDK_DIRNAME = "sdk"
_TRACE_INDEX_FILENAME = "sessions.json"

_exporter: LocalSDKTraceExporter | None = None
_processor: BatchTraceProcessor | None = None
_configure_lock = threading.Lock()


class LocalSDKTraceExporter(TracingExporter):
    """Write raw Agents SDK trace/span exports to per-session JSONL files."""

    def __init__(self, trace_dir: Path) -> None:
        self._lock = threading.Lock()
        self._trace_sessions: dict[str, str] = {}
        self.configure(trace_dir)

    def configure(self, trace_dir: Path) -> None:
        with self._lock:
            self._trace_dir = trace_dir
            self._trace_dir.mkdir(parents=True, exist_ok=True)
            self._index_path = self._trace_dir / _TRACE_INDEX_FILENAME
            self._index = self._read_index()

    def export(self, items: list[Trace | Span[Any]]) -> None:
        with self._lock:
            for item in items:
                try:
                    payload = item.export()
                    if not payload:
                        continue
                    session_id = self._session_id_for(item, payload)
                    path = self._path_for(session_id, payload)
                    record = {
                        "recorded_at": _now(),
                        "source": "openai-agents-sdk",
                        "session_id": session_id,
                        "item": payload,
                    }
                    with path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                except Exception as exc:  # noqa: BLE001 - tracing must never break a run
                    logger.warning("Could not write SDK trace item: %s", exc)

    def _session_id_for(self, item: Trace | Span[Any], payload: dict[str, Any]) -> str:
        if isinstance(item, Trace):
            trace_id = str(payload.get("id") or item.trace_id)
            group_id = payload.get("group_id")
            session_id = str(group_id or trace_id)
            self._trace_sessions[trace_id] = session_id
            return session_id

        trace_id = str(payload.get("trace_id") or item.trace_id)
        return self._trace_sessions.get(trace_id, trace_id)

    def _path_for(self, session_id: str, payload: dict[str, Any]) -> Path:
        record = self._index.get(session_id)
        if record:
            return self._trace_dir / record["filename"]

        timestamp = _first_request_timestamp(payload) or _now()
        filename = f"{_stamp(timestamp)}.jsonl"
        self._index[session_id] = {
            "filename": filename,
            "first_request_at": timestamp,
        }
        self._write_index()
        return self._trace_dir / filename

    def _read_index(self) -> dict[str, dict[str, str]]:
        if not self._index_path.exists():
            return {}
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read SDK trace session index (%s); starting fresh.", exc)
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
                index[str(key)] = {"filename": filename, "first_request_at": first_request_at}
        return index

    def _write_index(self) -> None:
        try:
            self._index_path.write_text(json.dumps(self._index, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not write SDK trace session index: %s", exc)


def configure_sdk_tracing(config: AppConfig, *, send_to_openai: bool) -> None:
    """Route Agents SDK traces to local JSONL files, optionally also to OpenAI."""
    global _exporter
    global _processor

    trace_dir = config.settings_dir / _TRACE_DIRNAME / _SDK_DIRNAME
    with _configure_lock:
        if _exporter is None:
            _exporter = LocalSDKTraceExporter(trace_dir)
            _processor = BatchTraceProcessor(_exporter)
        else:
            _exporter.configure(trace_dir)

        processor = _processor
        assert processor is not None
        processors: list[TracingProcessor] = [processor]
        if send_to_openai:
            processors.append(default_processor())
        set_trace_processors(processors)
        set_tracing_disabled(False)


def flush_sdk_traces() -> None:
    """Flush pending SDK trace/span records to disk."""
    try:
        flush_traces()
    except Exception as exc:  # noqa: BLE001 - tracing must never break a run
        logger.warning("Could not flush SDK traces: %s", exc)


def _first_request_timestamp(payload: dict[str, Any]) -> str | None:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None
    timestamp = metadata.get("request_created_at")
    return timestamp if isinstance(timestamp, str) and timestamp else None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _stamp(timestamp: str) -> str:
    return timestamp.replace(":", "").replace("-", "").replace(".", "").replace("+", "")


__all__ = ["LocalSDKTraceExporter", "configure_sdk_tracing", "flush_sdk_traces"]
