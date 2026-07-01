"""Mutable application state shared across requests.

Holds the resolved config, the built agent, MCP connections, and the session
manager. MCP servers are connected during app startup (FastAPI lifespan).
"""

from __future__ import annotations

from typing import Any

from agents import (
    Agent,
    set_default_openai_api,
    set_default_openai_client,
    set_default_openai_key,
    set_tracing_disabled,
)
from agents.mcp import MCPServerManager
from openai import AsyncOpenAI

from ..agent import SessionManager, build_agent
from ..config import AppConfig, SettingsStore
from ..logging_setup import get_logger
from ..mcp import connect_mcp_manager
from ..workspace import workspace

logger = get_logger(__name__)


class AppState:
    """Container for runtime state; rebuildable on settings change."""

    def __init__(self, store: SettingsStore, config: AppConfig) -> None:
        self.store = store
        self.config = config
        self.sessions = SessionManager(config)
        self._mcp_manager: MCPServerManager | None = None
        self.agent: Agent = build_agent(config)
        self._apply_side_effects()

    @classmethod
    def create(cls) -> AppState:
        store = SettingsStore()
        config = AppConfig.resolve(store)
        return cls(store, config)

    @property
    def mcp_failed_count(self) -> int:
        if self._mcp_manager is None:
            return 0
        return len(self._mcp_manager.failed_servers)

    @property
    def mcp_servers(self) -> list:
        if self._mcp_manager is None:
            return []
        return self._mcp_manager.active_servers

    async def startup(self) -> None:
        """Connect MCP servers and rebuild the agent (call from FastAPI lifespan)."""
        await self._connect_mcp()

    async def shutdown(self) -> None:
        """Disconnect MCP servers."""
        await self._disconnect_mcp()

    async def _connect_mcp(self) -> None:
        await self._disconnect_mcp()
        self._mcp_manager = await connect_mcp_manager()
        self.agent = build_agent(self.config, mcp_servers=self.mcp_servers)

    async def _disconnect_mcp(self) -> None:
        if self._mcp_manager is not None:
            try:
                await self._mcp_manager.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                logger.warning("MCP shutdown error: %s", exc)
            self._mcp_manager = None

    def _apply_side_effects(self) -> None:
        """Configure the workspace sandbox and the model provider from current config."""
        workspace.configure(self.config.workspace_dir)
        if self.config.base_url:
            client = AsyncOpenAI(
                base_url=self.config.base_url,
                api_key=self.config.api_key or "not-set",
            )
            set_default_openai_client(client, use_for_tracing=False)
            set_default_openai_api("chat_completions")
            set_tracing_disabled(True)
        elif self.config.has_api_key:
            set_default_openai_key(self.config.api_key)
            set_tracing_disabled(not self.config.enable_tracing)
        else:
            set_tracing_disabled(not self.config.enable_tracing)
        provider = self.config.active_provider
        logger.info(
            "App state ready (provider=%s): %s",
            provider.name if provider else "none",
            self.config.redacted(),
        )

    async def reload(self) -> AppConfig:
        """Re-resolve config, reconnect MCP, and rebuild the agent."""
        self.config = AppConfig.resolve(self.store)
        self.sessions = SessionManager(self.config)
        self._apply_side_effects()
        await self._connect_mcp()
        return self.config

    def health(self) -> dict[str, Any]:
        from .. import __version__

        provider = self.config.active_provider
        return {
            "status": "ok",
            "version": __version__,
            "model": self.config.model,
            "has_api_key": self.config.has_api_key,
            "base_url": self.config.base_url,
            "workspace": str(self.config.workspace_dir),
            "tracing": self.config.enable_tracing,
            "builtin_tools": len(self.agent.tools),
            "mcp_servers": len(self.mcp_servers),
            "mcp_failed": self.mcp_failed_count,
            "provider_name": provider.name if provider else None,
            "active_provider_id": self.config.active_provider_id,
        }


__all__ = ["AppState"]
