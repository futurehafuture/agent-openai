"""Smoke tests that exercise the real code paths without calling the OpenAI API."""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lumen.agent import build_agent
from lumen.config import AppConfig, SettingsStore
from lumen.mcp import load_mcp_specs
from lumen.server import AppState, create_app
from lumen.tools import get_tool_specs
from lumen.tools.data_tools import (
    aggregate_dataset,
    chart_dataset,
    inspect_dataset,
    summarize_dataset,
)
from lumen.tools.document_tools import convert_document, document_info, read_document
from lumen.tools.fs_tools import edit_file, glob_files, grep_files, read_file, write_file
from lumen.tools.pptx_tools import create_presentation
from lumen.tools.shell_tools import run_command
from lumen.workspace import WorkspaceError, workspace


@pytest.fixture()
def ws():
    """A workspace rooted under $HOME (the sandbox forbids paths outside home)."""
    root = Path.home() / f".lumen_test_{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    workspace.configure(root)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _write_csv(directory: Path) -> Path:
    path = directory / "sales.csv"
    path.write_text(
        "region,product,units,revenue\n"
        "West,Widget,10,100\nEast,Widget,5,50\nWest,Gadget,7,210\nEast,Gadget,3,90\n",
        encoding="utf-8",
    )
    return path


def test_registry_has_core_and_skill_tools():
    specs = get_tool_specs()
    names = {s.name for s in specs}
    assert "read_file" in names
    assert "run_command" in names
    assert "inspect_dataset" in names
    assert len(specs) >= 18
    cats = {s.category for s in specs}
    assert "fs" in cats and "shell" in cats and "skills" in cats


def test_sandbox_blocks_system_and_sensitive_paths(ws):
    assert workspace.resolve_output("ok.txt").parent == ws
    with pytest.raises(WorkspaceError):
        workspace.resolve_read("/etc/passwd")
    with pytest.raises(WorkspaceError):
        workspace.resolve_read("~/.ssh/id_rsa")
    with pytest.raises(WorkspaceError):
        workspace.resolve_output("../escape.txt")


def test_fs_tools_read_write_edit(ws):
    write_file(str(ws / "note.txt"), "hello\nworld\n")
    assert "hello" in read_file(str(ws / "note.txt"))
    edit_file(str(ws / "note.txt"), "world", "Lumen")
    assert "Lumen" in read_file(str(ws / "note.txt"))


def test_fs_tools_glob_grep(ws):
    (ws / "a.py").write_text("def foo(): pass\n")
    (ws / "b.txt").write_text("needle here\n")
    assert "a.py" in glob_files("*.py", str(ws))
    assert "needle" in grep_files("needle", str(ws))


def test_shell_tool_runs_in_workspace(ws):
    out = run_command("echo hello-lumen", cwd=str(ws))
    assert "hello-lumen" in out


def test_shell_blocks_sudo():
    with pytest.raises(ValueError, match="sudo"):
        run_command("sudo ls")


def test_data_tools(ws):
    _write_csv(ws)
    assert "4 rows" in inspect_dataset("sales.csv")
    assert "revenue" in summarize_dataset("sales.csv")
    agg = aggregate_dataset("sales.csv", group_by=["region"], value_column="revenue", agg="sum")
    assert "West" in agg and "East" in agg
    out = chart_dataset("sales.csv", kind="bar", x="product", y="revenue", title="Rev")
    png = next(ws.glob("charts/*.png"), None)
    assert png is not None and png.exists()
    assert "Chart saved" in out


def test_pptx_tool(ws):
    slides = '[{"title": "Results", "bullets": ["Up 12%", "Churn down"]}, {"title": "Next"}]'
    out = create_presentation("Quarterly Review", slides, subtitle="Q3", theme="light")
    deck = next(ws.glob("*.pptx"), None)
    assert deck is not None and deck.exists()
    assert "3-slide deck" in out


def test_document_tools(ws):
    (ws / "note.md").write_text("# Title\n\nHello world.\n- one\n- two\n", encoding="utf-8")
    assert "Hello world" in read_document("note.md")
    assert "words" in document_info("note.md")
    convert_document("note.md", to="docx")
    assert next(ws.glob("*.docx"), None) is not None


def test_agent_builds_with_tools():
    config = AppConfig.resolve(SettingsStore())
    agent = build_agent(config)
    assert agent.name == "Lumen"
    assert len(agent.tools) >= 18
    assert agent.input_guardrails


def test_mcp_config_parsing(tmp_path: Path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps(
            {
                "servers": {
                    "demo": {
                        "transport": "stdio",
                        "command": "echo",
                        "args": ["mcp"],
                        "enabled": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    specs = load_mcp_specs(cfg)
    assert len(specs) == 1
    assert specs[0].name == "demo"


def test_http_api_surface():
    with TestClient(create_app(AppState.create())) as client:
        health = client.get("/api/health").json()
        assert health["status"] == "ok"
        assert "builtin_tools" in health
        tools = client.get("/api/tools").json()
        assert any(t["name"] == "read_file" for t in tools)
        assert "model" in client.get("/api/settings").json()
        assert "config_path" in client.get("/api/mcp").json()
        created = client.post("/api/sessions").json()
        assert "id" in created
        assert any(s["id"] == created["id"] for s in client.get("/api/sessions").json())
        client.delete(f"/api/sessions/{created['id']}")
