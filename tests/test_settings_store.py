from __future__ import annotations

from pathlib import Path

from lumen.config import SettingsStore


def test_provider_update_preserves_saved_key_when_public_view_posts_back(tmp_path: Path):
    store = SettingsStore(tmp_path / "settings.json")
    store.update(
        {
            "providers": [
                {
                    "id": "deepseek",
                    "name": "DeepSeek",
                    "api_key": "sk-original",
                    "base_url": "https://api.deepseek.com",
                    "models": ["deepseek-chat"],
                    "default_model": "deepseek-chat",
                }
            ],
            "active_provider_id": "deepseek",
        }
    )

    store.update(
        {
            "providers": [
                {
                    "id": "deepseek",
                    "name": "DeepSeek",
                    "api_key_set": True,
                    "base_url": "https://api.deepseek.com",
                    "models": ["deepseek-chat", "deepseek-reasoner"],
                    "default_model": "deepseek-reasoner",
                }
            ],
            "active_provider_id": "deepseek",
        }
    )

    data = store.data["providers"][0]
    assert data["api_key"] == "sk-original"
    assert data["default_model"] == "deepseek-reasoner"
    assert data["models"] == ["deepseek-chat", "deepseek-reasoner"]


def test_provider_update_replaces_key_when_new_key_is_submitted(tmp_path: Path):
    store = SettingsStore(tmp_path / "settings.json")
    store.update(
        {
            "providers": [
                {
                    "id": "openai",
                    "name": "OpenAI",
                    "api_key": "sk-old",
                    "models": ["gpt-4.1"],
                    "default_model": "gpt-4.1",
                }
            ],
            "active_provider_id": "openai",
        }
    )

    store.update(
        {
            "providers": [
                {
                    "id": "openai",
                    "name": "OpenAI",
                    "api_key": "sk-new",
                    "api_key_set": True,
                    "models": ["gpt-4.1"],
                    "default_model": "gpt-4.1",
                }
            ]
        }
    )

    assert store.data["providers"][0]["api_key"] == "sk-new"
