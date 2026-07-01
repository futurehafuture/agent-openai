"""MCP (Model Context Protocol) server configuration and lifecycle.

Loads server definitions from ``~/.lumen/mcp.json`` (OpenClaw / Claude Code
style) and connects them via the OpenAI Agents SDK ``MCPServerManager``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents.mcp import MCPServer, MCPServerManager, MCPServerSse, MCPServerStdio, MCPServerStreamableHttp

from ..config import settings_dir
from ..logging_setup import get_logger

logger = get_logger(__name__)

MCP_FILENAME = "mcp.json"
_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


@dataclass(frozen=True)
class McpServerSpec:
    """One configured MCP server entry."""

    name: str
    transport: str
    enabled: bool
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] | None = None
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    allowed_tools: frozenset[str] | None = None
    blocked_tools: frozenset[str] | None = None


def mcp_config_path() -> Path:
    return settings_dir() / MCP_FILENAME


def _expand_env(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        return os.environ.get(key, "")

    return _ENV_PATTERN.sub(repl, value)


def _expand_mapping(data: dict[str, Any] | None) -> dict[str, str] | None:
    if not data:
        return None
    return {k: _expand_env(str(v)) for k, v in data.items()}


def load_mcp_specs(path: Path | None = None) -> list[McpServerSpec]:
    """Parse MCP server definitions from disk."""
    cfg_path = path or mcp_config_path()
    if not cfg_path.exists():
        return []
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read MCP config (%s): %s", cfg_path, exc)
        return []

    servers = raw.get("servers") or raw.get("mcpServers") or raw
    if not isinstance(servers, dict):
        logger.warning("MCP config must be an object of server definitions.")
        return []

    specs: list[McpServerSpec] = []
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("enabled") is False:
            continue
        transport = str(entry.get("transport") or _infer_transport(entry)).lower()
        allowed = entry.get("allowedTools") or entry.get("allowed_tools")
        blocked = entry.get("blockedTools") or entry.get("blocked_tools")
        specs.append(
            McpServerSpec(
                name=name,
                transport=transport,
                enabled=True,
                command=entry.get("command"),
                args=tuple(str(a) for a in (entry.get("args") or [])),
                env=_expand_mapping(entry.get("env")),
                cwd=entry.get("cwd"),
                url=entry.get("url"),
                headers=_expand_mapping(entry.get("headers")),
                allowed_tools=frozenset(allowed) if allowed else None,
                blocked_tools=frozenset(blocked) if blocked else None,
            )
        )
    return specs


def _infer_transport(entry: dict[str, Any]) -> str:
    if entry.get("url"):
        return "sse"
    return "stdio"


def _tool_filter(spec: McpServerSpec):
    """Build an SDK tool filter from allow/block lists."""
    if not spec.allowed_tools and not spec.blocked_tools:
        return None

    def _filter(_ctx: Any, tool: Any) -> bool:
        name = getattr(tool, "name", "")
        if spec.blocked_tools and name in spec.blocked_tools:
            return False
        if spec.allowed_tools:
            return name in spec.allowed_tools
        return True

    return _filter


def build_mcp_server(spec: McpServerSpec) -> MCPServer:
    """Instantiate one SDK MCP server from a spec."""
    tool_filter = _tool_filter(spec)
    cache = True
    if spec.transport == "stdio":
        if not spec.command:
            raise ValueError(f"MCP server {spec.name!r} requires 'command' for stdio transport.")
        params: dict[str, Any] = {
            "command": spec.command,
            "args": list(spec.args),
        }
        if spec.env:
            params["env"] = spec.env
        if spec.cwd:
            params["cwd"] = spec.cwd
        return MCPServerStdio(params, name=spec.name, cache_tools_list=cache, tool_filter=tool_filter)

    if not spec.url:
        raise ValueError(f"MCP server {spec.name!r} requires 'url' for {spec.transport} transport.")
    url = _expand_env(spec.url)
    headers = spec.headers

    if spec.transport in {"http", "streamable-http", "streamable_http"}:
        params = {"url": url}
        if headers:
            params["headers"] = headers
        return MCPServerStreamableHttp(
            params, name=spec.name, cache_tools_list=cache, tool_filter=tool_filter
        )

    # Default remote transport: SSE
    params = {"url": url}
    if headers:
        params["headers"] = headers
    return MCPServerSse(params, name=spec.name, cache_tools_list=cache, tool_filter=tool_filter)


def build_mcp_servers(path: Path | None = None) -> list[MCPServer]:
    """Build SDK MCP server instances from config (not yet connected)."""
    servers: list[MCPServer] = []
    for spec in load_mcp_specs(path):
        try:
            servers.append(build_mcp_server(spec))
        except ValueError as exc:
            logger.warning("Skipping MCP server %s: %s", spec.name, exc)
    return servers


async def connect_mcp_manager(path: Path | None = None) -> MCPServerManager | None:
    """Connect configured MCP servers; returns None if config is empty."""
    servers = build_mcp_servers(path)
    if not servers:
        return None
    manager = MCPServerManager(
        servers,
        strict=False,
        drop_failed_servers=True,
        connect_in_parallel=True,
        connect_timeout_seconds=15.0,
    )
    await manager.__aenter__()
    active = len(manager.active_servers)
    failed = len(manager.failed_servers)
    logger.info("MCP: %d server(s) connected, %d failed.", active, failed)
    for srv, err in manager.errors.items():
        logger.warning("MCP server %s failed: %s", srv.name, err)
    return manager


def mcp_public_view(path: Path | None = None) -> list[dict[str, Any]]:
    """Summary of configured MCP servers for the UI/API."""
    out: list[dict[str, Any]] = []
    for spec in load_mcp_specs(path):
        out.append(
            {
                "name": spec.name,
                "transport": spec.transport,
                "command": spec.command,
                "url": spec.url,
                "allowed_tools": sorted(spec.allowed_tools) if spec.allowed_tools else None,
                "blocked_tools": sorted(spec.blocked_tools) if spec.blocked_tools else None,
            }
        )
    return out


__all__ = [
    "McpServerSpec",
    "MCP_FILENAME",
    "build_mcp_servers",
    "connect_mcp_manager",
    "load_mcp_specs",
    "mcp_config_path",
    "mcp_public_view",
]
