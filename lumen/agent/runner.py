"""Streaming run wrapper.

Drives ``Runner.run_streamed`` and translates the SDK's rich event stream into a
small, stable protocol of plain dicts that the web UI understands:

    {"type": "agent",      "name": str}
    {"type": "token",      "delta": str}            # assistant text, streamed
    {"type": "reasoning",  "delta": str}            # optional reasoning summary
    {"type": "tool_call",  "name": str, "arguments": dict|str}
    {"type": "tool_output","name": str, "output": str}
    {"type": "error",      "kind": str, "message": str}
    {"type": "done",       "usage": {...}}

Errors are caught and surfaced as a terminal ``error`` event rather than crashing
the stream. If the consumer stops early (client disconnect), the underlying run is
cancelled in the ``finally`` block.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from agents import Agent, RunConfig, Runner, Session
from agents.exceptions import (
    AgentsException,
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
)
from openai.types.responses import (
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseOutputItemAddedEvent,
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseReasoningTextDeltaEvent,
    ResponseTextDeltaEvent,
)

from ..config import AppConfig
from ..logging_setup import get_logger
from .sdk_traces import flush_sdk_traces
from .traces import ModelTrace

logger = get_logger(__name__)

_MAX_OUTPUT_PREVIEW = 800


async def stream_agent_run(
    agent: Agent,
    user_input: str,
    session: Session,
    config: AppConfig,
    session_id: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield normalized streaming events for one user turn."""
    trace = ModelTrace.start(config=config, user_input=user_input, session_id=session_id, agent=agent)
    run_config = RunConfig(
        workflow_name="Lumen",
        tracing_disabled=False,
        group_id=session_id,
        trace_metadata={
            "session_id": session_id,
            "request_created_at": trace.created_at,
            "workspace": str(config.workspace_dir),
        },
    )
    result = Runner.run_streamed(
        agent,
        input=user_input,
        session=session,
        max_turns=config.max_turns,
        run_config=run_config,
    )
    call_names: dict[str, str] = {}
    streamed_reasoning_item_ids: set[str] = set()
    streamed_tool_call_ids: set[str] = set()
    argument_buffers: dict[str, str] = {}

    try:
        async for event in result.stream_events():
            trace.record_raw_event(event)
            if event.type == "raw_response_event":
                data = event.data
                if isinstance(data, ResponseTextDeltaEvent) and data.delta:
                    normalized = {"type": "token", "delta": data.delta}
                    trace.record_normalized_event(normalized)
                    yield normalized
                elif isinstance(data, ResponseOutputItemAddedEvent):
                    async for normalized in _normalize_raw_output_item_added(
                        data,
                        call_names,
                        streamed_tool_call_ids,
                    ):
                        trace.record_normalized_event(normalized)
                        yield normalized
                elif isinstance(data, ResponseFunctionCallArgumentsDeltaEvent) and data.delta:
                    item_id = data.item_id
                    argument_buffers[item_id] = argument_buffers.get(item_id, "") + data.delta
                    normalized = {
                        "type": "tool_call_update",
                        "id": item_id,
                        "arguments": argument_buffers[item_id],
                    }
                    trace.record_normalized_event(normalized)
                    yield normalized
                elif isinstance(data, ResponseFunctionCallArgumentsDoneEvent):
                    argument_buffers[data.item_id] = data.arguments
                    normalized = {
                        "type": "tool_call_update",
                        "id": data.item_id,
                        "name": data.name,
                        "arguments": data.arguments,
                    }
                    trace.record_normalized_event(normalized)
                    yield normalized
                elif isinstance(data, ResponseReasoningSummaryTextDeltaEvent) and data.delta:
                    streamed_reasoning_item_ids.add(data.item_id)
                    normalized = {"type": "reasoning", "delta": data.delta}
                    trace.record_normalized_event(normalized)
                    yield normalized
                elif isinstance(data, ResponseReasoningTextDeltaEvent) and data.delta:
                    streamed_reasoning_item_ids.add(data.item_id)
                    normalized = {"type": "reasoning", "delta": data.delta}
                    trace.record_normalized_event(normalized)
                    yield normalized

            elif event.type == "agent_updated_stream_event":
                normalized = {"type": "agent", "name": event.new_agent.name}
                trace.record_normalized_event(normalized)
                yield normalized

            elif event.type == "run_item_stream_event":
                async for normalized in _normalize_item(
                    event.item,
                    call_names,
                    streamed_reasoning_item_ids,
                    streamed_tool_call_ids,
                ):
                    trace.record_normalized_event(normalized)
                    yield normalized

    except InputGuardrailTripwireTriggered as exc:
        error = {
            "type": "error",
            "kind": "guardrail",
            "message": f"I can't help with that — it looks like {_guardrail_reason(exc)}.",
        }
        if trace.trace_path:
            error["trace_path"] = trace.trace_path
        trace.record_normalized_event(error)
        trace.finish("error", error=error)
        flush_sdk_traces()
        yield error
        return
    except MaxTurnsExceeded:
        error = {
            "type": "error",
            "kind": "max_turns",
            "message": "This task reached the step limit. Try splitting it into smaller asks.",
        }
        if trace.trace_path:
            error["trace_path"] = trace.trace_path
        trace.record_normalized_event(error)
        trace.finish("error", error=error)
        flush_sdk_traces()
        yield error
        return
    except AgentsException as exc:
        logger.exception("Agent run failed")
        error = {"type": "error", "kind": "agent", "message": f"The run failed: {exc}"}
        if trace.trace_path:
            error["trace_path"] = trace.trace_path
        trace.record_normalized_event(error)
        trace.finish("error", error=error)
        flush_sdk_traces()
        yield error
        return
    except Exception as exc:  # noqa: BLE001 - never let the stream crash the server
        logger.exception("Unexpected error during run")
        error = {"type": "error", "kind": "unexpected", "message": f"Unexpected error: {exc}"}
        if trace.trace_path:
            error["trace_path"] = trace.trace_path
        trace.record_normalized_event(error)
        trace.finish("error", error=error)
        flush_sdk_traces()
        yield error
        return
    finally:
        # If the consumer abandoned us (disconnect) before completion, stop the run.
        if not getattr(result, "is_complete", True):
            try:
                result.cancel()
            except Exception:  # noqa: BLE001
                pass
            trace.finish("cancelled")
            flush_sdk_traces()

    usage = _usage(result)
    done = {"type": "done", "usage": usage}
    if trace.trace_path:
        done["trace_path"] = trace.trace_path
    trace.record_normalized_event(done)
    trace.finish("completed", usage=usage)
    flush_sdk_traces()
    yield done


async def _normalize_item(
    item: Any,
    call_names: dict[str, str],
    streamed_reasoning_item_ids: set[str],
    streamed_tool_call_ids: set[str],
) -> AsyncIterator[dict[str, Any]]:
    itype = getattr(item, "type", "")
    if itype == "tool_call_item":
        name, call_id, args = _tool_call_info(item)
        if call_id:
            call_names[call_id] = name
        if _get(item.raw_item, "id") in streamed_tool_call_ids:
            return
        yield {
            "type": "tool_call",
            "id": _get(item.raw_item, "id"),
            "name": name,
            "call_id": call_id,
            "arguments": args,
        }
    elif itype == "tool_call_output_item":
        call_id = _get(item.raw_item, "call_id")
        name = call_names.get(call_id or "", "tool")
        yield {"type": "tool_output", "call_id": call_id, "name": name, "output": _preview(_output_text(item))}
    elif itype == "reasoning_item":
        if _get(item.raw_item, "id") in streamed_reasoning_item_ids:
            return
        text = _reasoning_text(item)
        if text:
            yield {"type": "reasoning", "delta": text}


async def _normalize_raw_output_item_added(
    event: ResponseOutputItemAddedEvent,
    call_names: dict[str, str],
    streamed_tool_call_ids: set[str],
) -> AsyncIterator[dict[str, Any]]:
    item = event.item
    if _get(item, "type") != "function_call":
        return

    name = _get(item, "name") or "tool"
    call_id = _get(item, "call_id")
    item_id = _get(item, "id")
    if call_id:
        call_names[call_id] = name
    if item_id:
        streamed_tool_call_ids.add(item_id)

    yield {
        "type": "tool_call",
        "id": item_id,
        "call_id": call_id,
        "name": name,
        "arguments": _get(item, "arguments") or "",
    }


def _tool_call_info(item: Any) -> tuple[str, str | None, Any]:
    raw = item.raw_item
    name = _get(raw, "name") or "tool"
    call_id = _get(raw, "call_id")
    raw_args = _get(raw, "arguments")
    args: Any = raw_args
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args)
        except (json.JSONDecodeError, TypeError):
            args = raw_args
    return name, call_id, args


def _output_text(item: Any) -> str:
    output = getattr(item, "output", None)
    if output is None:
        return ""
    return output if isinstance(output, str) else json.dumps(output, default=str)


def _reasoning_text(item: Any) -> str:
    raw = getattr(item, "raw_item", None)
    summary = _get(raw, "summary")
    if isinstance(summary, list):
        parts = [_get(s, "text") or "" for s in summary]
        return " ".join(p for p in parts if p).strip()
    return ""


def _preview(text: str) -> str:
    text = text.strip()
    if len(text) <= _MAX_OUTPUT_PREVIEW:
        return text
    return text[:_MAX_OUTPUT_PREVIEW] + "…"


def _guardrail_reason(exc: InputGuardrailTripwireTriggered) -> str:
    try:
        info = exc.guardrail_result.output.output_info
        if isinstance(info, dict) and info.get("reason"):
            return str(info["reason"])
    except AttributeError:
        pass
    return "a disallowed request"


def _usage(result: Any) -> dict[str, int]:
    try:
        usage = result.context_wrapper.usage
        return {
            "input_tokens": int(getattr(usage, "input_tokens", 0)),
            "output_tokens": int(getattr(usage, "output_tokens", 0)),
            "total_tokens": int(getattr(usage, "total_tokens", 0)),
        }
    except AttributeError:
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def _get(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


__all__ = ["stream_agent_run"]
