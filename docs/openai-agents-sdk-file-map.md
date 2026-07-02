# OpenAI Agents Python SDK 使用关系与文件说明

本文档说明本仓库中除 `openai-agents-python/` 外各文件的作用，以及它们如何直接或间接利用 OpenAI Agents Python SDK。

`openai-agents-python/` 如果出现在本地工作区中，应理解为上游 SDK 源码或临时参考目录。本项目真正依赖的是 `pyproject.toml` 中的 `openai-agents>=0.17,<1` 包，而不是把 SDK 源码当作应用代码维护。

## 总体架构

Lumen 是一个桌面工作代理应用：

1. `lumen.app` 启动 FastAPI 服务，并在桌面窗口中打开前端页面。
2. `lumen.server` 提供 REST API 和 `/api/chat/stream` SSE 流式聊天接口。
3. `lumen.agent.factory` 使用 OpenAI Agents Python SDK 创建 `Agent`，把本地工具、MCP 服务器、输入 guardrail 和模型配置组装进去。
4. `lumen.agent.runner` 调用 SDK 的 `Runner.run_streamed()`，把 SDK 原始事件转换为前端可消费的稳定事件协议。
5. `lumen.tools` 中的普通 Python 函数通过 SDK 的 `function_tool()` 包装为 Agent 工具。
6. `lumen.agent.sessions` 使用 SDK 的 `SQLiteSession` 保存会话历史。
7. `lumen.mcp` 使用 SDK 的 MCP server 类和 `MCPServerManager` 接入外部工具服务器。
8. `lumen.web` 前端只消费后端 API，不直接调用 OpenAI 或 Agents SDK。

## 根目录文件

| 文件 | 作用 | 与 OpenAI Agents Python SDK 的关系 |
| --- | --- | --- |
| `README.md` | 项目介绍、功能亮点、架构图、启动方式、配置项和安全模型说明。 | 明确说明项目基于 OpenAI Agents SDK，概述 agent loop、streaming、sessions、guardrails、MCP wiring、tracing 来自 SDK。 |
| `pyproject.toml` | Python 项目元数据、依赖、命令入口、ruff/pytest/mypy 配置。 | 声明核心依赖 `openai-agents>=0.17,<1` 和 `openai>=2.36.0,<3`；定义 `lumen = "lumen.app:main"` 作为启动入口。 |
| `uv.lock` | `uv` 生成的依赖锁文件，固定依赖版本以保证可复现安装。 | 锁定 `openai-agents` 及其传递依赖版本，确保 SDK 行为在安装时稳定。 |
| `.env.example` | 环境变量模板，包含 OpenAI API key、模型、工作区、端口、追踪等配置示例。 | 提供 `OPENAI_API_KEY`、`LUMEN_MODEL`、`LUMEN_ENABLE_TRACING` 等运行 SDK 所需配置。 |
| `mcp.json.example` | MCP 服务器配置示例，展示 stdio、SSE、远程服务器、环境变量展开、allow/block 工具列表。 | 配置会被 `lumen.mcp` 转换为 SDK 的 MCP server 对象，并挂载到 `Agent.mcp_servers`。 |
| `.gitignore` | 忽略密钥、本地环境、缓存、SQLite 数据库、运行目录和上游 SDK 源码目录。 | 明确忽略 `openai-agents-python/`，避免把外部 SDK 源码误提交为应用代码。 |
| `AGENTS.md` | 给代码代理的仓库级工作指导，强调先读代码再调试。 | 不直接使用 SDK；用于维护者或代码代理理解仓库协作规则。 |
| `CLAUDE.md` | 给 Claude Code 类工具的仓库级工作指导，内容与 `AGENTS.md` 类似。 | 不直接使用 SDK；用于辅助代码维护流程。 |
| `LICENSE` | MIT 许可证。 | 与 SDK 无直接关系。 |

## `lumen/` 应用包

| 文件 | 作用 | 与 OpenAI Agents Python SDK 的关系 |
| --- | --- | --- |
| `lumen/__init__.py` | 定义应用版本、名称和标语。 | 不直接调用 SDK；这些元信息会显示在桌面窗口和健康信息中。 |
| `lumen/app.py` | 桌面入口：加载 `.env`，创建 `AppState`，启动 FastAPI/uvicorn，桌面模式下打开 PyWebView 窗口。 | 间接使用 SDK：`AppState.create()` 会解析配置、构建 SDK `Agent` 并设置模型客户端。 |
| `lumen/config.py` | 管理持久化设置与不可变运行时配置，包括 provider、API key、base URL、模型、工作区、会话数据库路径。 | 为 SDK 提供模型名、API key、OpenAI-compatible base URL、max turns 和 tracing 开关；`AppConfig` 会传给 agent 构建和 run config。 |
| `lumen/workspace.py` | 中央文件系统沙箱，限制读写和 shell 操作在用户选择的项目目录内。 | SDK 工具函数执行前都通过这里校验路径；这是项目为 SDK function tools 增加的安全边界。 |
| `lumen/logging_setup.py` | 统一日志格式和日志级别，降低第三方库噪声。 | 间接支持 SDK 调试；同时避免在日志里暴露密钥。 |

## `lumen/agent/` Agent 核心

| 文件 | 作用 | 与 OpenAI Agents Python SDK 的关系 |
| --- | --- | --- |
| `lumen/agent/__init__.py` | 统一导出 agent factory、guardrail、runner 和 session manager。 | 方便其他模块从一个入口导入 SDK 相关封装。 |
| `lumen/agent/factory.py` | 构建 Lumen Agent：生成系统指令、工具目录说明，挂载内置工具、MCP server 和输入 guardrail。 | 直接使用 `agents.Agent`、`ModelSettings`；设置 `parallel_tool_calls=True`，并把 `tools`、`mcp_servers`、`input_guardrails` 交给 SDK。 |
| `lumen/agent/guardrails.py` | 用确定性正则规则拦截危险输入，如 `sudo`、磁盘擦除、读取密钥等。 | 直接使用 SDK 的 `@input_guardrail`、`GuardrailFunctionOutput`、`RunContextWrapper`。该 guardrail 被挂到 `Agent.input_guardrails`。 |
| `lumen/agent/runner.py` | 驱动一次流式 agent 运行，把 SDK 事件规范化为前端事件：token、reasoning、tool call、tool output、error、done。 | 直接调用 `Runner.run_streamed()`；使用 `RunConfig`、`Session`；处理 `InputGuardrailTripwireTriggered`、`MaxTurnsExceeded`、`AgentsException`；解析 `openai.types.responses` 流式事件。 |
| `lumen/agent/sessions.py` | 会话管理：创建、列出、重命名、删除会话；维护 sidebar 用 JSON 索引；修复中断后的工具调用历史。 | 直接使用 SDK 的 `SQLiteSession` 保存和读取 agent 对话历史。 |
| `lumen/agent/traces.py` | 应用层模型运行追踪，把一次请求的输入、输出、工具调用、usage、错误写成 JSONL。 | 接收 SDK `Agent` 和 runner 中的 SDK 原始事件；作为 SDK tracing 之外的业务友好追踪文件。 |
| `lumen/agent/sdk_traces.py` | 本地导出 SDK 原生 tracing 数据到 JSONL，可选同时发送到 OpenAI。 | 直接使用 `agents.tracing` 的 `set_trace_processors`、`BatchTraceProcessor`、`TracingExporter`、`Trace`、`Span`、`flush_traces`。 |

## `lumen/server/` HTTP 服务

| 文件 | 作用 | 与 OpenAI Agents Python SDK 的关系 |
| --- | --- | --- |
| `lumen/server/__init__.py` | 导出 `create_app` 和 `AppState`。 | 间接暴露包含 SDK Agent 的服务状态。 |
| `lumen/server/app_state.py` | 保存运行时状态：设置、配置、session manager、agent、MCP 连接；支持设置变更后的 reload。 | 直接使用 SDK 的 `Agent`、`set_default_openai_key`、`set_default_openai_client`、`set_default_openai_api`、`MCPServerManager`；对 OpenAI-compatible provider 设置自定义 `AsyncOpenAI` 客户端。 |
| `lumen/server/api.py` | FastAPI 路由：健康检查、工具列表、MCP 状态、设置、会话、工作区文件预览、SSE 聊天流。 | `/api/chat/stream` 调用 `stream_agent_run()`，后者执行 SDK runner；API 层把 SDK 运行结果通过 SSE 转给前端。 |
| `lumen/server/schemas.py` | Pydantic 请求模型：聊天、设置更新、重命名、打开文件。 | 不直接使用 SDK；为调用 SDK runner 前的数据校验提供边界。 |

## `lumen/tools/` 内置工具层

| 文件 | 作用 | 与 OpenAI Agents Python SDK 的关系 |
| --- | --- | --- |
| `lumen/tools/__init__.py` | 导入所有工具模块以触发注册，并导出工具注册表 API。 | `factory.py` 通过 `get_all_tools()` 取得 SDK `FunctionTool` 列表并挂载到 Agent。 |
| `lumen/tools/registry.py` | 工具注册中心：把普通函数包装成 SDK `FunctionTool`，保存工具元数据，并把可恢复异常转成人类可读消息。 | 直接使用 `agents.function_tool` 和 `FunctionTool`；这是本项目把 Python 函数接入 SDK tool calling 的核心适配层。 |
| `lumen/tools/fs_tools.py` | 文件系统工具：读文件、写文件、精确编辑、glob、grep、列目录。 | 各函数经 `register_tool()` 注册为 SDK 工具；Agent 可通过 tool call 安全操作项目文件。 |
| `lumen/tools/shell_tools.py` | shell 执行工具：在工作区内运行命令，并阻止 sudo、危险删除、磁盘写入、远程脚本管道执行等。 | 注册为 SDK function tool，让 Agent 能执行构建、git、脚本等命令。 |
| `lumen/tools/data_tools.py` | 数据处理工具：读取 CSV/Excel/JSON/Parquet，检查、汇总、查询、聚合、画图、清洗、转换。 | 注册为 SDK function tools，给 Agent 提供结构化数据分析能力；输出可被后续工具如 PPT 生成复用。 |
| `lumen/tools/pptx_tools.py` | PowerPoint 生成工具：根据 JSON slide outline 创建 `.pptx`。 | 注册为 SDK function tool，使 Agent 可以把分析结果或用户输入转成演示文稿。 |
| `lumen/tools/document_tools.py` | 文档/PDF 工具：读取 PDF/DOCX/文本，提取 PDF 页，文档信息，格式转换，文档搜索。 | 注册为 SDK function tools，让 Agent 能把文档内容读入上下文并进行总结、检索和转换。 |
| `lumen/tools/_format.py` | Markdown 表格、DataFrame 预览、字节大小等格式化辅助函数。 | 不直接使用 SDK；帮助工具输出稳定、适合 Agent 和前端显示的文本。 |

## `lumen/mcp/` MCP 接入

| 文件 | 作用 | 与 OpenAI Agents Python SDK 的关系 |
| --- | --- | --- |
| `lumen/mcp/__init__.py` | 读取 `~/.lumen/mcp.json`，展开环境变量，构建 stdio/SSE/streamable-http MCP server，支持 allowed/blocked tool filter，连接 MCP manager。 | 直接使用 SDK 的 `MCPServerStdio`、`MCPServerSse`、`MCPServerStreamableHttp`、`MCPServerManager`；连接后由 `factory.py` 挂到 Agent。 |

## `lumen/web/` 前端

| 文件 | 作用 | 与 OpenAI Agents Python SDK 的关系 |
| --- | --- | --- |
| `lumen/web/index.html` | 应用静态页面、启动动画、侧栏、聊天区、工作区文件面板、设置弹窗和脚本入口。 | 不直接使用 SDK；它通过后端 API 操作由 SDK 驱动的 Agent。 |
| `lumen/web/styles.css` | 前端样式系统，包括明暗主题、布局、聊天消息、工具卡片、文件预览、设置弹窗和响应式布局。 | 不直接使用 SDK；负责展示 SDK runner 产生的流式 token 和 tool call 状态。 |
| `lumen/web/js/api.js` | 浏览器端 API 客户端，封装 REST 请求和 `/api/chat/stream` SSE 解析。 | 不直接使用 SDK；它消费后端从 SDK 事件转换出来的 SSE payload。 |
| `lumen/web/js/app.js` | 前端控制器：初始化应用、加载健康状态/工具/设置/会话，驱动聊天流、会话切换、设置保存、工作区预览。 | 不直接使用 SDK；根据 `token`、`tool_call`、`tool_output`、`reasoning` 等事件更新 UI。 |
| `lumen/web/js/ui.js` | DOM 渲染辅助：消息、工具卡、会话列表、工具面板、toast、artifact 打开按钮。 | 不直接使用 SDK；将 SDK 工具调用的规范化事件渲染为用户可见的执行时间线。 |
| `lumen/web/js/markdown.js` | 轻量 Markdown 渲染器，支持工具输出和模型回复的常见 Markdown 子集。 | 不直接使用 SDK；负责显示模型和工具返回文本。 |

## `tests/` 测试

| 文件 | 作用 | 与 OpenAI Agents Python SDK 的关系 |
| --- | --- | --- |
| `tests/test_settings_store.py` | 测试 provider 设置更新时保留或替换 API key、模型列表和默认模型。 | 间接保障 SDK provider 配置正确，不会因为 UI public view 回写丢失密钥。 |
| `tests/test_smoke.py` | 覆盖工具注册、沙箱、文件工具、shell、数据工具、PPT、文档工具、Agent 构建、MCP 配置、HTTP API、SSE batching、runner 事件转发、trace 写入等。 | 直接构建 SDK `Agent`，mock `Runner.run_streamed()`，使用 SDK response event 类型测试流式事件转换；也测试 SDK tracing 本地导出。 |

## 关键 SDK 使用点速查

| SDK 能力 | 项目封装位置 | 说明 |
| --- | --- | --- |
| `Agent` | `lumen/agent/factory.py`, `lumen/server/app_state.py` | 创建 Lumen 主 agent，绑定指令、工具、MCP servers、guardrails。 |
| `Runner.run_streamed()` | `lumen/agent/runner.py` | 执行一次用户请求并获取流式事件。 |
| `FunctionTool` / `function_tool()` | `lumen/tools/registry.py` | 把本地 Python 函数变成 Agent 可调用工具。 |
| `input_guardrail` | `lumen/agent/guardrails.py` | 在模型运行前阻止明显危险请求。 |
| `SQLiteSession` | `lumen/agent/sessions.py` | 持久化会话历史。 |
| `RunConfig` | `lumen/agent/runner.py` | 设置 workflow name、group id、trace metadata。 |
| MCP server classes | `lumen/mcp/__init__.py` | 将本地或远程 MCP server 接入 Agent。 |
| SDK tracing | `lumen/agent/sdk_traces.py` | 将 SDK 原生 trace/span 写入本地 JSONL，可选转发到 OpenAI。 |
| 默认客户端/密钥设置 | `lumen/server/app_state.py` | 根据 provider 配置选择 OpenAI 默认 API key 或 OpenAI-compatible 自定义客户端。 |

## 一次聊天请求的调用链

1. 前端 `lumen/web/js/app.js` 调用 `streamChat()`。
2. `lumen/web/js/api.js` 向 `/api/chat/stream` 发起 POST，并逐帧解析 SSE。
3. `lumen/server/api.py` 创建或更新 session，然后调用 `stream_agent_run()`。
4. `lumen/agent/runner.py` 创建 `RunConfig`，调用 `Runner.run_streamed(agent, input, session, max_turns, run_config)`。
5. SDK 根据 `Agent` 的 instructions、tools、MCP servers 和 guardrails 执行模型循环。
6. 本地工具被调用时，SDK 调用 `lumen/tools/registry.py` 包装后的 `FunctionTool`。
7. runner 将 SDK 原始事件转换成前端事件，API 层通过 SSE 返回。
8. 前端 `AssistantTurn` 渲染 token、reasoning、工具调用和工具结果。
9. `SQLiteSession` 保存对话上下文，trace 模块保存业务追踪和 SDK 原生追踪。

## 维护建议

- 新增工具时，把普通函数放入合适的 `lumen/tools/*_tools.py`，最后调用 `register_tool(fn, category=...)`；确认 `lumen/tools/__init__.py` 会导入该模块。
- 修改 agent 指令或工具选择策略时，优先看 `lumen/agent/factory.py`。
- 修改流式 UI 协议时，需要同时改 `lumen/agent/runner.py`、`lumen/server/api.py` 和 `lumen/web/js/app.js`/`ui.js`。
- 修改 provider 或兼容第三方模型接口时，重点看 `lumen/config.py` 和 `lumen/server/app_state.py`。
- 修改 MCP 配置格式或生命周期时，重点看 `lumen/mcp/__init__.py` 和 `mcp.json.example`。
