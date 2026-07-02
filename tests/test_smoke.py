"""Smoke tests that exercise the real code paths without calling the OpenAI API."""

from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from openai.types.responses import (
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseFunctionToolCall,
    ResponseOutputItemAddedEvent,
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseTextDeltaEvent,
)

from lumen.agent import build_agent
from lumen.agent.runner import stream_agent_run
from lumen.agent.traces import ModelTrace
from lumen.config import AppConfig, SettingsStore
from lumen.mcp import load_mcp_specs
from lumen.server import AppState, create_app
from lumen.server.api import _batched_sse
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
from lumen.workspace import ApprovalRequired, WorkspaceError, workspace


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


def _config_for_workspace(directory: Path, tmp_path: Path) -> AppConfig:
    store = SettingsStore(tmp_path / f"settings-{uuid.uuid4().hex}.json")
    store.update({"workspace": str(directory)})
    settings_dir = tmp_path / f"lumen-settings-{uuid.uuid4().hex}"
    settings_dir.mkdir(parents=True, exist_ok=True)
    return replace(
        AppConfig.resolve(store),
        settings_dir=settings_dir,
        sessions_db_path=settings_dir / "sessions.sqlite3",
    )


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


def test_sandbox_requires_approval_outside_project(ws):
    outside = ws.parent / f"{ws.name}_outside.txt"
    outside.write_text("nope", encoding="utf-8")
    try:
        with pytest.raises(ApprovalRequired):
            workspace.resolve_read(str(outside))
        with pytest.raises(ApprovalRequired):
            workspace.resolve_write(str(outside))
    finally:
        outside.unlink(missing_ok=True)


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


def test_shell_requires_approval_for_paths_outside_project(ws):
    with pytest.raises(ApprovalRequired):
        run_command("cat ../outside.txt", cwd=str(ws))


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


def test_workspace_file_browser_api(ws, tmp_path: Path, monkeypatch):
    store = SettingsStore(tmp_path / "settings.json")
    store.update({"workspace": str(ws)})
    state = AppState(store, AppConfig.resolve(store))
    (ws / "note.txt").write_text("hello preview", encoding="utf-8")
    (ws / "nested").mkdir()
    (ws / "nested" / "data.json").write_text('{"ok": true}', encoding="utf-8")
    opened: list[Path] = []
    monkeypatch.setattr("lumen.server.api._os_open", opened.append)

    with TestClient(create_app(state)) as client:
        files = client.get("/api/workspace/files").json()
        paths = {item["path"] for item in files["files"]}
        assert "note.txt" in paths
        assert "nested/data.json" in paths

        preview = client.get("/api/workspace/preview", params={"path": "note.txt"}).json()
        assert preview["kind"] == "text"
        assert preview["text"] == "hello preview"

        raw = client.get("/api/workspace/raw", params={"path": "note.txt"})
        assert raw.status_code == 200
        assert raw.text == "hello preview"

        opened_file = client.post("/api/open", json={"path": "note.txt"}).json()
        assert opened_file["path"] == str(ws / "note.txt")
        assert opened == [ws / "note.txt"]


async def test_batched_sse_flushes_before_next_event():
    import asyncio

    async def slow_events() -> AsyncIterator[dict[str, str]]:
        yield {"type": "reasoning", "delta": "first"}
        await asyncio.sleep(0.05)
        yield {"type": "token", "delta": "second"}

    frames = _batched_sse(slow_events(), batch_window=0.01, max_batch=8)
    first = await asyncio.wait_for(anext(frames), timeout=0.03)
    assert '"first"' in first

    second = await asyncio.wait_for(anext(frames), timeout=0.08)
    assert '"second"' in second


async def test_agent_stream_forwards_reasoning_delta(ws, tmp_path: Path, monkeypatch):
    from lumen.agent import runner

    class FakeResult:
        is_complete = True
        context_wrapper = SimpleNamespace(
            usage=SimpleNamespace(input_tokens=0, output_tokens=0, total_tokens=0)
        )

        async def stream_events(self):
            yield SimpleNamespace(
                type="raw_response_event",
                data=ResponseReasoningSummaryTextDeltaEvent(
                    delta="thinking",
                    item_id="item_1",
                    output_index=0,
                    sequence_number=1,
                    summary_index=0,
                    type="response.reasoning_summary_text.delta",
                ),
            )
            yield SimpleNamespace(
                type="run_item_stream_event",
                item=SimpleNamespace(
                    type="reasoning_item",
                    raw_item=SimpleNamespace(
                        id="item_1",
                        summary=[SimpleNamespace(text="thinking")],
                    ),
                ),
            )

    monkeypatch.setattr(runner.Runner, "run_streamed", lambda *args, **kwargs: FakeResult())

    events = [
        event async for event in stream_agent_run(
            agent=SimpleNamespace(),
            user_input="hi",
            session=SimpleNamespace(),
            config=_config_for_workspace(ws, tmp_path),
        )
    ]
    assert events.count({"type": "reasoning", "delta": "thinking"}) == 1


async def test_agent_stream_forwards_raw_tool_call_before_run_item(ws, tmp_path: Path, monkeypatch):
    from lumen.agent import runner

    class FakeResult:
        is_complete = True
        context_wrapper = SimpleNamespace(
            usage=SimpleNamespace(input_tokens=0, output_tokens=0, total_tokens=0)
        )

        async def stream_events(self):
            yield SimpleNamespace(
                type="raw_response_event",
                data=ResponseOutputItemAddedEvent(
                    item=ResponseFunctionToolCall(
                        arguments="",
                        call_id="call_1",
                        id="fc_1",
                        name="twitter-news",
                        type="function_call",
                    ),
                    output_index=0,
                    sequence_number=1,
                    type="response.output_item.added",
                ),
            )
            yield SimpleNamespace(
                type="raw_response_event",
                data=ResponseFunctionCallArgumentsDeltaEvent(
                    delta='{"query"',
                    item_id="fc_1",
                    output_index=0,
                    sequence_number=2,
                    type="response.function_call_arguments.delta",
                ),
            )
            yield SimpleNamespace(
                type="raw_response_event",
                data=ResponseFunctionCallArgumentsDoneEvent(
                    arguments='{"query": "x"}',
                    item_id="fc_1",
                    name="twitter-news",
                    output_index=0,
                    sequence_number=3,
                    type="response.function_call_arguments.done",
                ),
            )
            yield SimpleNamespace(
                type="run_item_stream_event",
                item=SimpleNamespace(
                    type="tool_call_item",
                    raw_item=SimpleNamespace(
                        arguments='{"query": "x"}',
                        call_id="call_1",
                        id="fc_1",
                        name="twitter-news",
                    ),
                ),
            )

    monkeypatch.setattr(runner.Runner, "run_streamed", lambda *args, **kwargs: FakeResult())

    events = [
        event async for event in stream_agent_run(
            agent=SimpleNamespace(),
            user_input="hi",
            session=SimpleNamespace(),
            config=_config_for_workspace(ws, tmp_path),
        )
    ]

    tool_calls = [event for event in events if event["type"] == "tool_call"]
    assert tool_calls == [
        {
            "type": "tool_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "twitter-news",
            "arguments": "",
        }
    ]
    assert {
        "type": "tool_call_update",
        "id": "fc_1",
        "arguments": '{"query"',
    } in events
    assert {
        "type": "tool_call_update",
        "id": "fc_1",
        "name": "twitter-news",
        "arguments": '{"query": "x"}',
    } in events


async def test_agent_stream_writes_json_trace(ws, tmp_path: Path, monkeypatch):
    from lumen.agent import runner

    class FakeResult:
        is_complete = True
        context_wrapper = SimpleNamespace(
            usage=SimpleNamespace(input_tokens=3, output_tokens=4, total_tokens=7)
        )

        async def stream_events(self):
            yield SimpleNamespace(
                type="raw_response_event",
                data=ResponseTextDeltaEvent(
                    content_index=0,
                    delta="hello",
                    item_id="msg_1",
                    logprobs=[],
                    output_index=0,
                    sequence_number=1,
                    type="response.output_text.delta",
                ),
            )
            yield SimpleNamespace(
                type="raw_response_event",
                data=ResponseTextDeltaEvent(
                    content_index=0,
                    delta=" world",
                    item_id="msg_1",
                    logprobs=[],
                    output_index=0,
                    sequence_number=2,
                    type="response.output_text.delta",
                ),
            )

    monkeypatch.setattr(runner.Runner, "run_streamed", lambda *args, **kwargs: FakeResult())

    config = _config_for_workspace(ws, tmp_path)
    events = [
        event async for event in stream_agent_run(
            agent=SimpleNamespace(name="Lumen", tools=[]),
            user_input="trace me",
            session=SimpleNamespace(),
            config=config,
            session_id="session_1",
        )
    ]

    trace_dir = config.settings_dir / "model-traces"
    trace_files = list(trace_dir.glob("*.jsonl"))
    assert len(trace_files) == 1
    assert not (ws / "model-traces").exists()
    lines = trace_files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    trace = json.loads(lines[0])
    assert trace["status"] == "completed"
    assert trace["session_id"] == "session_1"
    assert trace["request"]["user_input"] == "trace me"
    assert trace["usage"] == {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}
    assert "events" not in trace
    assert trace["raw_event_counts"] == {"raw_response_event": 2}
    assert trace["response"]["assistant"] == "hello world"
    assert events[-1]["type"] == "done"
    assert events[-1]["trace_path"] == str(trace_files[0])


async def test_model_trace_appends_session_records_to_first_request_jsonl(ws, tmp_path: Path):
    config = _config_for_workspace(ws, tmp_path)

    first = ModelTrace.start(config=config, user_input="first", session_id="session_1")
    assert first.trace_path is not None
    first.record_normalized_event({"type": "token", "delta": "one"})
    first.finish("completed", usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

    second = ModelTrace.start(config=config, user_input="second", session_id="session_1")
    assert second.trace_path == first.trace_path
    second.record_normalized_event({"type": "token", "delta": "two"})
    second.finish("completed", usage={"input_tokens": 2, "output_tokens": 2, "total_tokens": 4})

    path = Path(first.trace_path)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [record["request"]["user_input"] for record in records] == ["first", "second"]
    first_timestamp = records[0]["created_at"].replace(":", "").replace("-", "").replace(".", "")
    assert path.name == f"{first_timestamp}.jsonl"


def test_sdk_traces_are_saved_locally_by_session(ws, tmp_path: Path):
    from agents.tracing import custom_span, trace

    from lumen.agent.sdk_traces import configure_sdk_tracing, flush_sdk_traces

    config = _config_for_workspace(ws, tmp_path)
    request_created_at = "2026-07-02T00:00:00.123456Z"
    configure_sdk_tracing(config, send_to_openai=False)

    with trace(
        "Lumen",
        group_id="session_1",
        metadata={
            "session_id": "session_1",
            "request_created_at": request_created_at,
            "workspace": str(ws),
        },
    ):
        with custom_span("unit-test", {"ok": True}):
            pass

    flush_sdk_traces()

    trace_dir = config.settings_dir / "model-traces" / "sdk"
    trace_files = list(trace_dir.glob("*.jsonl"))
    assert [path.name for path in trace_files] == ["20260702T000000123456Z.jsonl"]
    records = [json.loads(line) for line in trace_files[0].read_text(encoding="utf-8").splitlines()]
    assert [record["session_id"] for record in records] == ["session_1", "session_1"]
    assert records[0]["item"]["object"] == "trace"
    assert records[0]["item"]["group_id"] == "session_1"
    assert records[1]["item"]["object"] == "trace.span"
    assert records[1]["item"]["span_data"]["type"] == "custom"


async def test_model_trace_handles_unpickleable_sdk_fields(ws, tmp_path: Path):
    import asyncio

    @dataclass
    class EventWithFuture:
        type: str
        pending: asyncio.Future

    config = _config_for_workspace(ws, tmp_path)
    trace = ModelTrace.start(config=config, user_input="hi")
    assert trace.trace_path is not None
    assert not Path(trace.trace_path).exists()
    trace.record_raw_event(EventWithFuture(type="raw_response_event", pending=asyncio.Future()))
    assert not Path(trace.trace_path).exists()
    trace.finish("completed", usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})

    payload = json.loads(Path(trace.trace_path).read_text(encoding="utf-8").splitlines()[0])
    assert payload["status"] == "completed"
    assert payload["raw_event_counts"] == {"raw_response_event": 1}


async def test_model_trace_aggregates_deltas_into_complete_response(ws, tmp_path: Path):
    config = _config_for_workspace(ws, tmp_path)
    trace = ModelTrace.start(config=config, user_input="hi")
    trace.record_normalized_event({"type": "reasoning", "delta": "think "})
    trace.record_normalized_event({"type": "reasoning", "delta": "done"})
    trace.record_normalized_event(
        {
            "type": "tool_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "read_file",
            "arguments": "",
        }
    )
    trace.record_normalized_event({"type": "tool_call_update", "id": "fc_1", "arguments": '{"path": "a.txt"}'})
    trace.record_normalized_event(
        {"type": "tool_output", "call_id": "call_1", "name": "read_file", "output": "contents"}
    )
    trace.record_normalized_event({"type": "token", "delta": "answer"})
    trace.finish("completed", usage={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3})

    assert trace.trace_path is not None
    payload = json.loads(Path(trace.trace_path).read_text(encoding="utf-8").splitlines()[0])
    assert "events" not in payload
    assert payload["response"]["reasoning"] == "think done"
    assert payload["response"]["assistant"] == "answer"
    assert payload["response"]["tool_calls"] == [
        {
            "id": "fc_1",
            "call_id": "call_1",
            "name": "read_file",
            "arguments": {"path": "a.txt"},
            "output": "contents",
            "started_at": payload["response"]["tool_calls"][0]["started_at"],
            "finished_at": payload["response"]["tool_calls"][0]["finished_at"],
        }
    ]
