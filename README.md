# Lumen ✦

**A beautiful desktop work agent, built on the [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/).**

Lumen is a native desktop app that puts a capable agent on your machine to handle
everyday busywork — analysing spreadsheets, building PowerPoint decks, tidying your
desktop, and working with documents and PDFs. Responses stream live, and every
action runs through a sandboxed, safety-first tool layer.

<p align="center"><em>PyWebView shell · FastAPI + SSE streaming · OpenAI Agents SDK · refined light/dark UI</em></p>

---

## Highlights

- **Streaming, tool-using agent.** Token-by-token responses with an inline timeline
  of tool calls (arguments in, results out) via Server-Sent Events.
- **Real capabilities, not chat.** Core **filesystem + shell** tools (Claude Code-style)
  plus optional **skill** shortcuts (data, PPT, documents), and **MCP** for external services.
  - **Filesystem** — read, write, edit, glob, grep, list (anywhere in home, sandboxed).
  - **Shell** — run commands (git, builds, scripts) with safety guardrails.
  - **Skills** — inspect/chart CSV, generate `.pptx`, read PDF/Word (convenience layer).
  - **MCP** — connect stdio/SSE/HTTP MCP servers from `~/.lumen/mcp.json`.
- **Production-grade architecture.** Immutable config, tool **registry**, input **guardrail**,
  persistent **sessions** (SQLite), structured logging, cancellation, and a filesystem **sandbox**.
- **Safe by default.** Work is scoped to the selected project folder; shell blocks sudo/disk wipe;
  MCP supports per-server tool allow/block lists.

## Architecture

```
lumen/
├── app.py              # Desktop entry: starts FastAPI + opens the PyWebView window
├── config.py           # SettingsStore (persisted) + AppConfig (immutable, env-resolved)
├── workspace.py        # Filesystem sandbox (home read/write + output zone)
├── mcp/                # MCP server config loader + MCPServerManager lifecycle
├── logging_setup.py    # Centralised logging
├── tools/              # Built-in capability layer
│   ├── registry.py     # register_tool(), guarded errors, ToolSpec metadata
│   ├── fs_tools.py     # read / write / edit / glob / grep / list (Claude Code-style)
│   ├── shell_tools.py  # run_command (sandboxed bash)
│   ├── data_tools.py   # pandas + matplotlib skill shortcuts
│   ├── pptx_tools.py   # python-pptx deck builder
│   └── document_tools.py # pypdf + python-docx skill shortcuts
├── agent/              # Agent core
│   ├── factory.py      # build_agent(): instructions + tools + MCP + guardrail
│   ├── guardrails.py   # fast deterministic input safety check
│   ├── sessions.py     # SQLiteSession + a title index for the sidebar
│   └── runner.py       # run_streamed() → normalized event protocol
├── server/             # FastAPI: REST + the SSE /api/chat/stream endpoint
└── web/                # Refined-minimal UI (HTML/CSS + ES-module JS)
```

The agent loop, streaming, sessions, guardrails, MCP wiring, and tracing come from the
OpenAI Agents SDK; Lumen adds the desktop shell, fs/shell tools, skill shortcuts, and UI.

## Getting started

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
# 1. Install dependencies
uv sync

# 2. Provide your OpenAI key (or paste it into the in-app Settings panel)
cp .env.example .env && $EDITOR .env   # set OPENAI_API_KEY

# 3. Launch the desktop app
uv run lumen
```

Prefer a browser, or on a headless box? Run the server only and open the URL it prints:

```bash
uv run lumen --server
```

## Configuration

Settings resolve with precedence **environment > Settings panel > default**, and are
stored in `~/.lumen/settings.json` (the API key file is written `0600`).

| Setting | Env var | Default |
|---|---|---|
| API key | `OPENAI_API_KEY` | — (required) |
| Model | `LUMEN_MODEL` | `gpt-4.1` |
| Project workspace | `LUMEN_WORKSPACE` | `~/Lumen` |
| Max steps / task | `LUMEN_MAX_TURNS` | `24` |
| Send traces to OpenAI | `LUMEN_ENABLE_TRACING` | off |

## MCP servers

Copy the example config and enable the servers you need:

```bash
cp mcp.json.example ~/.lumen/mcp.json
$EDITOR ~/.lumen/mcp.json   # set enabled:true, fill env vars
```

Restart Lumen (or change any setting in the UI to trigger reload). Check status:

```bash
curl -s http://127.0.0.1:8765/api/mcp | python3 -m json.tool
```

Supports **stdio**, **SSE**, and **streamable-http** transports. Use `allowedTools` /
`blockedTools` per server (OpenClaw-style). Env vars in values can use `${VAR}` syntax.

## Safety model

- File tools and shell commands work inside the selected project folder. Paths outside
  that folder require user review; system and sensitive paths remain off-limits.
- Shell blocks sudo, disk wipe, and piping remote scripts to bash.
- MCP servers are opt-in via `~/.lumen/mcp.json`; failed servers are skipped gracefully.
- An input guardrail blocks clearly dangerous requests before the model runs.

## Development

```bash
uv run pytest        # smoke tests for tools, sandbox, agent build, and the API
uv run ruff check .  # lint
```

## License

MIT
