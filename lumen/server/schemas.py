"""Pydantic request/response models for the HTTP API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """A single user turn."""

    message: str = Field(..., min_length=1, max_length=20000)
    session_id: str | None = None


class SettingsUpdate(BaseModel):
    """Partial settings update from the Settings panel (all fields optional)."""

    # Non-provider fields
    theme: str | None = None
    workspace: str | None = None
    max_turns: int | None = Field(default=None, ge=1, le=100)
    enable_tracing: bool | None = None

    # Deprecated flat fields — kept for backward compat during migration
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None

    # Multi-provider fields
    providers: list[dict[str, Any]] | None = None
    active_provider_id: str | None = None
    selected_model: str | None = None


class RenameRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)


class OpenRequest(BaseModel):
    path: str = Field(..., min_length=1)


__all__ = ["ChatRequest", "SettingsUpdate", "RenameRequest", "OpenRequest"]
