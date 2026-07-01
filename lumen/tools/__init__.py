"""Tool package: importing it registers every built-in capability with the registry.

MCP tools are attached separately via ``Agent.mcp_servers`` — see ``lumen.mcp``.
"""

from __future__ import annotations

from . import data_tools, document_tools, fs_tools, pptx_tools, shell_tools  # noqa: F401
from .registry import (
    CATEGORY_LABELS,
    TOOL_REGISTRY,
    ToolSpec,
    get_all_tools,
    get_tool_specs,
)

__all__ = [
    "get_all_tools",
    "get_tool_specs",
    "TOOL_REGISTRY",
    "ToolSpec",
    "CATEGORY_LABELS",
]
