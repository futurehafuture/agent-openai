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
from openai.types.responses import ResponseTextDeltaEvent

from ..config import AppConfig
from ..logging_setup import get_logger

logger = get_logger(__name__)

_MAX_OUTPUT_PREVIEW = 800


async def stream_agent_run(
    agent: Agent,
    user_input: str,
    session: Session,
    config: AppConfig,
) -> AsyncIterator[dict[str, Any]]:
    """Yield normalized streaming events for one user turn."""
    run_config = RunConfig(
        workflow_name="Lumen",
        tracing_disabled=not config.enable_tracing,
    )
    result = Runner.run_streamed(
        agent,
        input=user_input,
        session=session,
        max_turns=config.max_turns,
        run_config=run_config,
    )
    call_names: dict[str, str] = {}

    try:
        async for event in result.stream_events():
            if event.type == "raw_response_event":
                data = event.data
                if isinstance(data, ResponseTextDeltaEvent) and data.delta:
                    yield {"type": "token", "delta": data.delta}

            elif event.type == "agent_updated_stream_event":
                yield {"type": "agent", "name": event.new_agent.name}

            elif event.type == "run_item_stream_event":
                async for normalized in _normalize_item(event.item, call_names):
                    yield normalized

    except InputGuardrailTripwireTriggered as exc:
        yield {
            "type": "error",
            "kind": "guardrail",
            "message": f"I can't help with that — it looks like {_guardrail_reason(exc)}.",
        }
        return
    except MaxTurnsExceeded:
        yield {
            "type": "error",
            "kind": "max_turns",
            "message": "This task reached the step limit. Try splitting it into smaller asks.",
        }
        return
    except AgentsException as exc:
        logger.exception("Agent run failed")
        yield {"type": "error", "kind": "agent", "message": f"The run failed: {exc}"}
        return
    except Exception as exc:  # noqa: BLE001 - never let the stream crash the server
        logger.exception("Unexpected error during run")
        yield {"type": "error", "kind": "unexpected", "message": f"Unexpected error: {exc}"}
        return
    finally:
        # If the consumer abandoned us (disconnect) before completion, stop the run.
        if not getattr(result, "is_complete", True):
            try:
                result.cancel()
            except Exception:  # noqa: BLE001
                pass

    yield {"type": "done", "usage": _usage(result)}


async def _normalize_item(item: Any, call_names: dict[str, str]) -> AsyncIterator[dict[str, Any]]:
    itype = getattr(item, "type", "")
    if itype == "tool_call_item":
        name, call_id, args = _tool_call_info(item)
        if call_id:
            call_names[call_id] = name
        yield {"type": "tool_call", "name": name, "arguments": args}
    elif itype == "tool_call_output_item":
        call_id = _get(item.raw_item, "call_id")
        name = call_names.get(call_id or "", "tool")
        yield {"type": "tool_output", "name": name, "output": _preview(_output_text(item))}
    elif itype == "reasoning_item":
        text = _reasoning_text(item)
        if text:
            yield {"type": "reasoning", "delta": text}


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
