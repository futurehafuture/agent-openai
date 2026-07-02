"""Configuration and persisted user settings for Lumen.

Two layers:

* :class:`SettingsStore` — mutable user settings persisted to
  ``~/.lumen/settings.json`` (model, theme, workspace, API key pasted from the
  Settings panel). This is what the UI reads and writes.
* :class:`AppConfig` — an immutable, fully-resolved runtime view built from
  defaults + settings file + environment variables (env wins). Passed around the
  app so behaviour can't be mutated mid-run.

Precedence (highest first): process environment > settings file > built-in default.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .logging_setup import get_logger

logger = get_logger(__name__)

DEFAULT_MODEL = "gpt-4.1"
SETTINGS_DIRNAME = ".lumen"
SETTINGS_FILENAME = "settings.json"
SESSIONS_DB_FILENAME = "sessions.sqlite3"

# Keys the UI is allowed to persist, with their defaults.
_PERSISTED_DEFAULTS: dict[str, Any] = {
    "model": DEFAULT_MODEL,
    "theme": "light",
    "workspace": str(Path.home() / "Lumen"),
    "max_turns": 24,
    "enable_tracing": False,
    "api_key": "",  # only used if OPENAI_API_KEY env var is not set
    # Optional OpenAI-compatible endpoint (e.g. DeepSeek: https://api.deepseek.com).
    # When set, the app talks Chat Completions to this base URL instead of OpenAI.
    "base_url": "",
    # Multi-provider fields (added post-migration)
    "providers": [],
    "active_provider_id": "",
    "selected_model": "",
}


# ---------------------------------------------------------------------------
# ProviderConfig — one configured LLM provider
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderConfig:
    """Immutable configuration for one LLM provider."""

    id: str
    name: str
    api_key: str = ""
    base_url: str = ""
    models: tuple[str, ...] = ()
    default_model: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ProviderConfig:
        """Build from a raw JSON dict with validation."""
        mid = str(d.get("id", "")).strip()
        if not mid:
            raise ValueError("Provider 'id' is required.")
        name = str(d.get("name", mid)).strip()
        models = tuple(
            m.strip() for m in (d.get("models") or []) if isinstance(m, str) and m.strip()
        )
        default_model = str(d.get("default_model", "")).strip()
        if default_model and default_model not in models:
            # If default_model is set but not in models, add it
            models = models + (default_model,)
        if models and not default_model:
            default_model = models[0]
        return cls(
            id=mid,
            name=name,
            api_key=str(d.get("api_key", "")).strip(),
            base_url=str(d.get("base_url", "")).strip(),
            models=models,
            default_model=default_model,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict (api_key included for disk)."""
        return {
            "id": self.id,
            "name": self.name,
            "api_key": self.api_key,
            "base_url": self.base_url,
            "models": list(self.models),
            "default_model": self.default_model,
        }

    def to_public(self) -> dict[str, Any]:
        """Serialise for the UI — api_key is masked."""
        return {
            "id": self.id,
            "name": self.name,
            "api_key_set": bool(self.api_key),
            "base_url": self.base_url,
            "models": list(self.models),
            "default_model": self.default_model,
        }

    def with_model(self, model: str) -> ProviderConfig:
        """Return a copy with *model* as the new default (adds to models if new)."""
        models = self.models
        if model not in models:
            models = models + (model,)
        return replace(self, models=models, default_model=model)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def settings_dir() -> Path:
    """Return (and create) the per-user Lumen settings directory."""
    path = Path.home() / SETTINGS_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _generate_provider_id(name: str, existing_ids: set[str]) -> str:
    """Create a unique, URL-safe slug from a display name."""
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "provider"
    if slug not in existing_ids:
        return slug
    i = 2
    while f"{slug}-{i}" in existing_ids:
        i += 1
    return f"{slug}-{i}"


# ---------------------------------------------------------------------------
# SettingsStore
# ---------------------------------------------------------------------------

class SettingsStore:
    """Read/write persisted user settings as JSON, with validation."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (settings_dir() / SETTINGS_FILENAME)
        self._data: dict[str, Any] = dict(_PERSISTED_DEFAULTS)
        self.load()

    def load(self) -> dict[str, Any]:
        """Load settings from disk, falling back to defaults on any error."""
        if not self.path.exists():
            return self._data
        migrated = False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            # Auto-migrate old flat config on first load
            raw = self._migrate_flat_to_providers(raw)
            migrated = "providers" in raw and not self._data.get("providers")
            for key in _PERSISTED_DEFAULTS:
                if key in raw:
                    self._data[key] = raw[key]
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read settings (%s); using defaults.", exc)
        if migrated:
            # Remove deprecated flat keys from in-memory data
            for old_key in ("model", "api_key", "base_url"):
                self._data.pop(old_key, None)
            try:
                self.save()
            except OSError:
                pass
        return self._data

    def _migrate_flat_to_providers(self, raw: dict[str, Any]) -> dict[str, Any]:
        """One-time migration: old flat model/key/url → a 'Default' provider."""
        if raw.get("providers") is not None:
            return raw  # already migrated
        old_model = raw.get("model", "")
        old_key = raw.get("api_key", "")
        old_url = raw.get("base_url", "")
        if not old_model and not old_key and not old_url:
            return raw  # nothing to migrate
        provider = {
            "id": "default",
            "name": "Default",
            "api_key": old_key,
            "base_url": old_url,
            "models": [old_model] if old_model and old_model != DEFAULT_MODEL else [DEFAULT_MODEL],
            "default_model": old_model or DEFAULT_MODEL,
        }
        raw["providers"] = [provider]
        raw["active_provider_id"] = "default"
        raw.pop("model", None)
        raw.pop("api_key", None)
        raw.pop("base_url", None)
        logger.info("Migrated flat config to multi-provider format: 'Default' provider created.")
        return raw

    def save(self) -> None:
        """Persist current settings to disk (0600 perms, since it may hold a key)."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            os.chmod(self.path, 0o600)
        except OSError as exc:
            logger.error("Could not save settings: %s", exc)
            raise

    def update(self, values: dict[str, Any]) -> dict[str, Any]:
        """Merge a partial settings dict, validate, persist, and return public view."""
        for key, value in values.items():
            if key in _PERSISTED_DEFAULTS and value is not None:
                if key == "providers":
                    self._data[key] = self._validate_providers(
                        value,
                        existing=self._data.get("providers", []),
                    )
                elif key == "active_provider_id":
                    pid = str(value).strip()
                    known = {p.get("id") for p in self._data.get("providers", [])}
                    if pid and pid not in known:
                        logger.warning("Ignoring unknown active_provider_id: %s", pid)
                        continue
                    self._data[key] = pid
                elif key == "selected_model":
                    # Update the active provider's default_model
                    model = str(value).strip()
                    providers = list(self._data.get("providers", []))
                    active_id = self._data.get("active_provider_id", "")
                    updated = False
                    for p in providers:
                        if p.get("id") == active_id:
                            if model not in p.get("models", []):
                                p.setdefault("models", []).append(model)
                            p["default_model"] = model
                            updated = True
                            break
                    if updated:
                        self._data["providers"] = providers
                    else:
                        logger.warning("Could not set selected_model: no active provider.")
                        continue
                elif key in ("model", "api_key", "base_url") and self._data.get("providers"):
                    # Deprecated flat keys — route to active provider if providers exist
                    providers = list(self._data["providers"])
                    active_id = self._data.get("active_provider_id", "")
                    for p in providers:
                        if p.get("id") == active_id:
                            if key == "model":
                                if value not in p.get("models", []):
                                    p.setdefault("models", []).append(value)
                                p["default_model"] = value
                            elif key == "api_key":
                                p["api_key"] = value
                            elif key == "base_url":
                                p["base_url"] = value
                            break
                    self._data["providers"] = providers
                else:
                    self._data[key] = value
        # Coerce known numeric/bool fields defensively.
        self._data["max_turns"] = max(
            1, min(int(self._data.get("max_turns", _PERSISTED_DEFAULTS["max_turns"])), 100)
        )
        self._data["enable_tracing"] = bool(
            self._data.get("enable_tracing", _PERSISTED_DEFAULTS["enable_tracing"])
        )
        self.save()
        return self.public_view()

    @staticmethod
    def _validate_providers(
        providers: list[dict[str, Any]],
        existing: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Validate and normalise a provider list from the UI."""
        validated: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        existing_by_id = {
            str(p.get("id", "")): p
            for p in (existing or [])
            if isinstance(p, dict) and p.get("id")
        }
        for entry in providers:
            if not isinstance(entry, dict):
                continue
            pid = str(entry.get("id", "")).strip()
            name = str(entry.get("name", pid)).strip()
            # Generate unique IDs
            if not pid:
                pid = _generate_provider_id(name, seen_ids)
            elif pid in seen_ids:
                pid2 = _generate_provider_id(name, seen_ids)
                logger.warning("Duplicate provider id %r, renamed to %r", entry.get("id"), pid2)
                pid = pid2
            seen_ids.add(pid)
            models = [
                m.strip()
                for m in (entry.get("models") or [])
                if isinstance(m, str) and m.strip()
            ]
            default_model = str(entry.get("default_model", "")).strip()
            if not default_model and models:
                default_model = models[0]
            elif default_model and default_model not in models:
                models.append(default_model)
            api_key = str(entry.get("api_key", "")).strip()
            if not api_key and entry.get("api_key_set"):
                api_key = str(existing_by_id.get(pid, {}).get("api_key", "")).strip()
            validated.append(
                {
                    "id": pid,
                    "name": name,
                    "api_key": api_key,
                    "base_url": str(entry.get("base_url", "")).strip(),
                    "models": models,
                    "default_model": default_model,
                }
            )
        return validated

    def public_view(self) -> dict[str, Any]:
        """Settings safe to send to the UI: API keys masked, never raw."""
        view: dict[str, Any] = {}
        # Non-sensitive settings
        for key in ("theme", "workspace", "max_turns", "enable_tracing", "active_provider_id"):
            view[key] = self._data.get(key, _PERSISTED_DEFAULTS.get(key))
        # Resolve model from active provider
        view["model"] = self._resolve_model()
        view["base_url"] = self._resolve_base_url()
        view["api_key_set"] = bool(self._resolve_api_key()) or bool(
            os.environ.get("OPENAI_API_KEY")
        )
        view["api_key_from_env"] = bool(os.environ.get("OPENAI_API_KEY"))
        # Provider list with masked keys
        providers = self._data.get("providers", [])
        view["providers"] = [_provider_public(p) for p in providers]
        return view

    def _resolve_api_key(self) -> str:
        providers = self._data.get("providers", [])
        active_id = self._data.get("active_provider_id", "")
        for p in providers:
            if p.get("id") == active_id:
                return str(p.get("api_key", ""))
        # Fallback: old flat key
        return str(self._data.get("api_key", ""))

    def _resolve_model(self) -> str:
        providers = self._data.get("providers", [])
        active_id = self._data.get("active_provider_id", "")
        for p in providers:
            if p.get("id") == active_id:
                return str(p.get("default_model", DEFAULT_MODEL))
        return str(self._data.get("model", DEFAULT_MODEL))

    def _resolve_base_url(self) -> str:
        providers = self._data.get("providers", [])
        active_id = self._data.get("active_provider_id", "")
        for p in providers:
            if p.get("id") == active_id:
                return str(p.get("base_url", ""))
        return str(self._data.get("base_url", ""))

    @property
    def data(self) -> dict[str, Any]:
        return dict(self._data)


def _provider_public(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": raw.get("id", ""),
        "name": raw.get("name", ""),
        "api_key_set": bool(raw.get("api_key", "")),
        "base_url": raw.get("base_url", ""),
        "models": raw.get("models", []),
        "default_model": raw.get("default_model", ""),
    }


# ---------------------------------------------------------------------------
# AppConfig
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AppConfig:
    """Immutable, fully-resolved runtime configuration."""

    api_key: str
    model: str
    base_url: str
    workspace_dir: Path
    settings_dir: Path
    sessions_db_path: Path
    host: str
    port: int
    max_turns: int
    enable_tracing: bool
    log_level: str
    providers: tuple[ProviderConfig, ...] = ()
    active_provider_id: str = ""

    @classmethod
    def resolve(cls, store: SettingsStore) -> AppConfig:
        """Build a frozen config from settings + environment (env wins)."""
        s = store.data

        def env(name: str, fallback: Any) -> Any:
            return os.environ.get(name, fallback)

        # Resolve providers from store
        raw_providers = s.get("providers", [])
        providers = tuple(
            ProviderConfig.from_dict(p)
            for p in raw_providers
            if isinstance(p, dict)
        )
        active_id = str(s.get("active_provider_id", ""))

        # Resolve from active provider, with env var overrides
        api_key = os.environ.get("OPENAI_API_KEY") or ""
        model = os.environ.get("LUMEN_MODEL") or ""
        base_url = os.environ.get("LUMEN_BASE_URL", "").strip()
        if not (api_key or model or base_url):
            # No env overrides — use active provider values
            api_key = store._resolve_api_key()
            model = store._resolve_model()
            base_url = store._resolve_base_url()
        else:
            # Env vars set — use them, falling back to active provider
            if not api_key:
                api_key = store._resolve_api_key()
            if not model:
                model = store._resolve_model() or DEFAULT_MODEL
            if not base_url:
                base_url = store._resolve_base_url()

        workspace = Path(os.path.expanduser(str(env("LUMEN_WORKSPACE", s["workspace"])))).resolve()
        sdir = settings_dir()

        return cls(
            api_key=api_key,
            model=model,
            base_url=base_url,
            workspace_dir=workspace,
            settings_dir=sdir,
            sessions_db_path=sdir / SESSIONS_DB_FILENAME,
            host=str(env("LUMEN_HOST", "127.0.0.1")),
            port=int(env("LUMEN_PORT", 8765)),
            max_turns=int(env("LUMEN_MAX_TURNS", s["max_turns"])),
            enable_tracing=_as_bool(env("LUMEN_ENABLE_TRACING", s["enable_tracing"])),
            log_level=str(env("LUMEN_LOG_LEVEL", "INFO")).upper(),
            providers=providers,
            active_provider_id=active_id,
        )

    @property
    def active_provider(self) -> ProviderConfig | None:
        """Return the active :class:`ProviderConfig`, or *None*."""
        for p in self.providers:
            if p.id == self.active_provider_id:
                return p
        return None

    def with_overrides(self, **changes: Any) -> AppConfig:
        """Return a copy with selected fields replaced (config stays immutable)."""
        return replace(self, **changes)

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    def redacted(self) -> dict[str, Any]:
        """A log-safe dict — never includes the raw API key."""
        return {
            "model": self.model,
            "base_url": self.base_url or "openai-default",
            "workspace_dir": str(self.workspace_dir),
            "host": self.host,
            "port": self.port,
            "max_turns": self.max_turns,
            "enable_tracing": self.enable_tracing,
            "api_key_set": self.has_api_key,
            "active_provider": self.active_provider.name if self.active_provider else None,
            "providers_count": len(self.providers),
        }


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "AppConfig",
    "ProviderConfig",
    "SettingsStore",
    "settings_dir",
    "DEFAULT_MODEL",
]
