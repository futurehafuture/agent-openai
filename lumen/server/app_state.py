"""Mutable application state shared across requests.

Holds the resolved config, the built agent, MCP connections, and the session
manager. MCP servers are connected during app startup (FastAPI lifespan).
"""

from __future__ import annotations

import json
from typing import Any

from agents import (
    Agent,
    set_default_openai_api,
    set_default_openai_client,
    set_default_openai_key,
)
from agents.mcp import MCPServerManager
from openai import AsyncOpenAI

from ..agent import SessionManager, build_agent
from ..agent.sdk_traces import configure_sdk_tracing
from ..config import AppConfig, SettingsStore
from ..logging_setup import get_logger
from ..mcp import connect_mcp_manager
from ..workspace import workspace

logger = get_logger(__name__)


class _DeepSeekClient(AsyncOpenAI):
    """AsyncOpenAI subclass that strips ``reasoning_content`` from outgoing messages.

    DeepSeek's Chat Completions API supports ``reasoning_content`` in streaming
    *responses* but rejects input messages that carry it alongside ``tool_calls``.
    The OpenAI Agents SDK replays reasoning items as ``reasoning_content`` on the
    next assistant message, which causes a 400 error when tools were called.
    """

    def _strip_reasoning_content(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        messages = kwargs.get("messages")
        if not isinstance(messages, list):
            return kwargs
        stripped = 0
        for msg in messages:
            if isinstance(msg, dict) and "reasoning_content" in msg:
                del msg["reasoning_content"]
                stripped += 1
        if stripped:
            logger.debug("Stripped reasoning_content from %d message(s).", stripped)
        return kwargs

    async def post(self, *args: Any, **kwargs: Any) -> Any:
        # Intercept JSON body before it hits the wire.
        if "body" in kwargs:
            body = kwargs["body"]
            if isinstance(body, dict):
                self._strip_reasoning_content(body)
                self._merge_duplicate_assistant_messages(body)
            elif isinstance(body, str):
                try:
                    parsed = json.loads(body)
                    if isinstance(parsed, dict):
                        self._strip_reasoning_content(parsed)
                        self._merge_duplicate_assistant_messages(parsed)
                        kwargs["body"] = json.dumps(parsed)
                except (json.JSONDecodeError, TypeError):
                    pass
        return await super().post(*args, **kwargs)

    @staticmethod
    def _merge_duplicate_assistant_messages(body: dict[str, Any]) -> None:
        """Merge consecutive assistant messages into one.

        The SDK sometimes emits two back-to-back assistant messages — one
        carrying ``tool_calls`` and another with an empty text body. DeepSeek
        is stricter than OpenAI and requires tool messages to directly follow
        the assistant message that made the calls.
        """
        messages = body.get("messages")
        if not isinstance(messages, list):
            return
        merged: list[dict[str, Any]] = []
        i = 0
        while i < len(messages):
            msg = dict(messages[i])  # shallow copy
            role = msg.get("role", "")
            if role == "assistant" and i + 1 < len(messages):
                next_msg = messages[i + 1]
                if isinstance(next_msg, dict) and next_msg.get("role") == "assistant":
                    # Merge tool_calls from both messages.
                    tc = list(msg.get("tool_calls") or [])
                    tc2 = list(next_msg.get("tool_calls") or [])
                    if tc2:
                        tc.extend(tc2)
                    if tc:
                        msg["tool_calls"] = tc
                    # Use the longer content.
                    c1 = msg.get("content") or ""
                    c2 = next_msg.get("content") or ""
                    if isinstance(c1, str) and isinstance(c2, str) and len(c2) > len(c1):
                        msg["content"] = c2
                    # Remove empty content if we have tool_calls (DeepSeek wants null).
                    if msg.get("tool_calls") and not msg.get("content"):
                        msg["content"] = None
                    # Remove empty tool_calls.
                    if not msg.get("tool_calls"):
                        msg.pop("tool_calls", None)
                    logger.debug("Merged two consecutive assistant messages.")
                    i += 2
                    merged.append(msg)
                    continue
            merged.append(msg)
            i += 1
        body["messages"] = merged


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
            client = _DeepSeekClient(
                base_url=self.config.base_url,
                api_key=self.config.api_key or "not-set",
            )
            set_default_openai_client(client, use_for_tracing=False)
            set_default_openai_api("chat_completions")
        elif self.config.has_api_key:
            set_default_openai_key(self.config.api_key)
        send_traces_to_openai = self.config.enable_tracing and not self.config.base_url and self.config.has_api_key
        configure_sdk_tracing(self.config, send_to_openai=send_traces_to_openai)
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
