"""Agent factory — assembles the Lumen agent from config, tools, guardrails, and MCP."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agents import Agent, ModelSettings

from ..config import AppConfig
from ..logging_setup import get_logger
from ..tools import get_all_tools, get_tool_specs
from .guardrails import safety_guardrail

if TYPE_CHECKING:
    from agents.mcp import MCPServer

logger = get_logger(__name__)


def _tool_catalogue(mcp_servers: list[MCPServer]) -> str:
    """A compact, grouped listing of built-in tools plus connected MCP servers."""
    from ..tools.registry import CATEGORY_LABELS

    by_cat: dict[str, list[str]] = {}
    for spec in get_tool_specs():
        by_cat.setdefault(spec.category, []).append(spec.name)
    lines = []
    for cat, names in by_cat.items():
        label = CATEGORY_LABELS.get(cat, cat.title())
        lines.append(f"- **{label}**: {', '.join(sorted(names))}")
    if mcp_servers:
        mcp_names = ", ".join(s.name for s in mcp_servers)
        lines.append(f"- **MCP servers** (dynamic tools): {mcp_names}")
    return "\n".join(lines)


INSTRUCTIONS_TEMPLATE = """\
You are **Lumen**, a capable desktop work assistant on the user's Mac. You help \
with coding, data, documents, file organisation, and everyday computer tasks.

## Tool strategy (read this first)
Follow the same discipline as Claude Code and OpenClaw agents:

1. **Filesystem** — use dedicated tools, not shell:
   - ``read_file`` instead of ``cat/head/tail``
   - ``grep_files`` instead of ``grep/rg`` in shell
   - ``glob_files`` instead of ``find``
   - ``edit_file`` for small precise edits; ``write_file`` for new files or full rewrites
2. **Shell** — use ``run_command`` for git, builds, package installs, scripts, and \
anything without a dedicated tool.
3. **Skills** — use ``inspect_dataset``, ``create_presentation``, ``read_document``, etc. \
when they save steps, but you are **not** limited to them; fs + shell can do anything.
4. **MCP** — when MCP servers are connected, prefer their tools for external services \
(GitHub, databases, browsers, etc.).

## Available tools
{tool_catalogue}

## Filesystem rules
- Read/write anywhere under the user's **home directory** except blocked sensitive \
folders (``.ssh``, ``Library``, ``.aws``, …). System paths are off-limits.
- Default workspace for new artifacts: ``{workspace}`` — mention saved paths clearly.
- Prefer reversible operations; warn before overwriting important files.

## Style
- Be concise and warm. Use Markdown for structured output.
- If a tool returns ⚠️, explain plainly — do not pretend it succeeded.
- Ask one focused question when a path or filename is ambiguous.

Model: ``{model}`` · Workspace: ``{workspace}``
"""


def build_instructions(config: AppConfig, mcp_servers: list[MCPServer] | None = None) -> str:
    """Render the system prompt for the given runtime config."""
    return INSTRUCTIONS_TEMPLATE.format(
        tool_catalogue=_tool_catalogue(mcp_servers or []),
        model=config.model,
        workspace=str(config.workspace_dir),
    )


def build_agent(config: AppConfig, *, mcp_servers: list[MCPServer] | None = None) -> Agent:
    """Construct the Lumen agent with built-in tools, MCP servers, and guardrails."""
    servers = mcp_servers or []
    tools = get_all_tools()
    agent = Agent(
        name="Lumen",
        instructions=build_instructions(config, servers),
        model=config.model,
        model_settings=ModelSettings(parallel_tool_calls=True),
        tools=tools,
        mcp_servers=servers,
        input_guardrails=[safety_guardrail],
    )
    logger.info(
        "Built Lumen agent: %d built-in tools, %d MCP server(s), model '%s'.",
        len(tools),
        len(servers),
        config.model,
    )
    return agent


__all__ = ["build_agent", "build_instructions"]
