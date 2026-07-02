"""Tool registry and factory.

Every capability Lumen exposes to the agent is a plain, typed, documented
Python function. :func:`register_tool` wraps it as an Agents-SDK ``FunctionTool``
and records metadata (category, summary) so the UI can show what the agent can
do. A guard decorator converts expected errors into readable messages the agent
can recover from, instead of crashing a run.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass

from agents import FunctionTool, function_tool

from ..logging_setup import get_logger
from ..workspace import ApprovalRequired, WorkspaceError

logger = get_logger(__name__)

CATEGORY_LABELS: dict[str, str] = {
    "fs": "Filesystem",
    "shell": "Shell",
    "skills": "Skills",
    "mcp": "MCP",
    # legacy aliases kept for any cached imports
    "data": "Skills",
    "pptx": "Skills",
    "files": "Filesystem",
    "docs": "Skills",
}


@dataclass(frozen=True)
class ToolSpec:
    """Immutable description of a registered tool."""

    name: str
    category: str
    summary: str
    tool: FunctionTool
    func: Callable[..., str]


TOOL_REGISTRY: dict[str, ToolSpec] = {}


def _guard(func: Callable[..., str]) -> Callable[..., str]:
    """Convert expected, recoverable errors into agent-readable messages."""

    @functools.wraps(func)
    def wrapper(*args: object, **kwargs: object) -> str:
        try:
            return func(*args, **kwargs)
        except ApprovalRequired as exc:
            return f"⚠️ Approval required: {exc}"
        except WorkspaceError as exc:
            return f"⚠️ Not allowed: {exc}"
        except FileNotFoundError as exc:
            return f"⚠️ File not found: {exc}"
        except (ValueError, KeyError, TypeError) as exc:
            return f"⚠️ {type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - last-resort safety net for the run loop
            logger.exception("Unexpected error in tool %s", func.__name__)
            return f"⚠️ Unexpected error in {func.__name__}: {exc}"

    return wrapper


def register_tool(
    func: Callable[..., str],
    *,
    category: str,
    summary: str | None = None,
) -> FunctionTool:
    """Register ``func`` as a FunctionTool under ``category`` and return the tool."""
    guarded = _guard(func)
    tool = function_tool(guarded)
    spec = ToolSpec(
        name=tool.name,
        category=category,
        summary=summary or _first_line(tool.description),
        tool=tool,
        func=func,
    )
    TOOL_REGISTRY[tool.name] = spec
    logger.debug("Registered tool '%s' [%s]", tool.name, category)
    return tool


def get_all_tools() -> list[FunctionTool]:
    """All registered tools, ready to attach to an Agent."""
    return [spec.tool for spec in TOOL_REGISTRY.values()]


def get_tool_specs() -> list[ToolSpec]:
    """All tool specs (for the UI tool palette)."""
    return list(TOOL_REGISTRY.values())


def _first_line(text: str | None) -> str:
    if not text:
        return ""
    return text.strip().splitlines()[0]


__all__ = [
    "register_tool",
    "get_all_tools",
    "get_tool_specs",
    "ToolSpec",
    "TOOL_REGISTRY",
    "CATEGORY_LABELS",
]
