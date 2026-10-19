# MCP Client SDK 与协议基线（MCP00 ADR）

日期：2026-10-19（Asia/Shanghai）。基线：分支 `codex/mcp-client`，起点 `6ff45ba`（`main`），版本保持 `0.2.0a1`。
状态：MCP00 已完成；本文只记录**实际核对过**的事实、证据命令与决策，不描述尚未实现的能力。

## 1. 决策摘要

| 决策 | 取值 | 依据 |
| --- | --- | --- |
| SDK 版本 | `mcp==2.3.0`（extra 内精确 pin） | 计划第 3 节的首个候选；本机实测通过两代协议与两种传输 |
| 直接校验器 | `jsonschema>=4.20.0` | 适配器自己调用 JSON Schema 校验；SDK 亦要求 `>=4.20.0` |
| Python 下界 | `>=3.10`（不变） | SDK 包元数据 `requires_python = ">=3.10"`；基础包仍是标准库 |
| 协议版本 | `2025-11-25`、`2026-07-28`；其余显式拒绝 | SDK 的 `LATEST_HANDSHAKE_VERSION` / `MODERN_PROTOCOL_VERSIONS` |
| 传输 | stdio、Streamable HTTP | 计划第 1 节范围 |
| 调用入口 | **公开低层 `ClientSession`**（`initialize()` / `discover()` / `call_tool`） | 高层 `Client.call_tool` 在 HEADER_MISMATCH 后会重发同一个业务调用 |
| 连接协商 | 显式 era，不用 `mode="auto"` | `negotiate_auto` 含探测重试与回退，不是「一次准入一次 tools/call」需要的边界 |
| 资源上限 | SDK 自带 + 适配器自补（见 §4） | SDK 的 stdio 帧与 HTTP JSON 体没有上限 |

## 2. 为什么不用高层 `Client.call_tool`（一手证据）

`mcp==2.3.0` 的 `Client.call_tool` 文档写明：在 `2026-07-28` 连接上，如果服务端以
`HEADER_MISMATCH`（-32020）拒绝，客户端会**重新拉取工具列表并把原调用再发一次**。真实
HTTP 夹具上的出站 JSON-RPC 序列：

```text
Client.call_tool("headerbound", {"tenant": "acme"})
  -> ['tools/call', 'tools/list', 'tools/call']      # 同一个业务调用在协议上出现两次
  -> 服务端 facts: server.call_tool.start 只有 1 条   # 第一次被 -32020 拦下，没有执行

ClientSession.call_tool(...)（未列工具、无 Mcp-Param-* 头）
  -> ['tools/call']                                   # 一次请求，服务端拒绝，客户端不再补发
```

对写工具，重发就是重复副作用。因此适配器的执行路径只用 `ClientSession`；`Client` 的
输入补全驱动、响应缓存、in-process server 等便利特性一律不进适配器。

第二条相关行为：`ClientSession.call_tool` 成功后会在需要时调用 `validate_tool_result`，
而它**可能触发一次 `tools/list`**（该工具的输出 schema 尚未缓存时）。所以发现阶段必须先
`list_tools()`，让调用路径只出现 `tools/call`；测试对协议请求的计数因此是分方法统计的。

## 3. 实际核对过的 SDK 事实（`mcp==2.3.0`）

| 事实 | 值 / 行为 | 核对方式 |
| --- | --- | --- |
| 已知协议版本 | `2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25, 2026-07-28` | `mcp_types/version.py` |
| 握手代 | `HANDSHAKE_PROTOCOL_VERSIONS`，最新 `2025-11-25` | 同上 + 真实 `initialize()` 实测 |
| 现代代 | `MODERN_PROTOCOL_VERSIONS = ("2026-07-28",)`，走 `server/discover` | 同上 + 真实 `discover()` 实测 |
| 未知版本 | `is_version_at_least("2099-01-01", ...) is False`（保守） | `tests/mcp/test_contract.py` |
| stdio 传输 | `stdio_client(StdioServerParameters)`：只继承 `HOME/LOGNAME/PATH/SHELL/TERM/USER` 加上显式 `env`；stdout=协议、stderr 分离；退出有界（2s 正常终止 + 2s SIGKILL） | 源码 + 实测 |
| HTTP 传输 | `streamable_http_client(url, http_client=..., terminate_on_close=..., max_sse_event_size=...)`；重定向仅限同 origin（同 scheme/host/port，或同 host 的 http→https）；`max_sse_event_size` 默认 1 MiB | 源码 |
| POST 失败 | 4xx/5xx 不重发：映射为 JSON-RPC error 或协议错误 | 源码（`StreamableHTTPTransport.post_writer`） |
| 超时 | `DISCOVER_TIMEOUT_SECONDS = 10.0`；SDK 默认 http client：connect/write/pool 30s、read 300s | 源码 |
| 单次调用 | `ClientSession.call_tool` 只发一次 `tools/call`（两代、两种传输均实测为 1） | `tests/mcp/test_contract.py` |
| 流上的异常 | 读流里的 `Exception` 项**不会**让挂起请求失败：`JsonRpcDispatcher._dispatch` 在没有 `on_stream_exception` 时只记一条 debug 日志 | 源码 + MCP02 实测：发异常后请求一直等到期限 |
| 连接结束 | 读流 EOF 才是唤醒挂起请求的路径（`_fan_out_closed` → `CONNECTION_CLOSED`） | 源码 + 真实子进程退出实测 |
| anyio 版本 | 该环境的 anyio 没有 `anyio.subprocess` 模块；`open_process` 的 stdin/stdout/stderr 默认就是 PIPE | 真实的 stdio 启动失败与签名核对 |

## 4. 适配器必须自己补的边界（SDK 不提供）

1. **stdio 单帧上限**：SDK 的 stdout 读取按 `\n` 切分并把不完整行累加在无上限字符串里，
   一个不说话只灌数据的服务端可以吃光内存。适配器在传输之上包一层有界帧读取，超限即协议
   错误并判连接失效（不是截断后继续用）。
1b. **失败可见性**：因为读流里的异常不会失败挂起请求（§3 末行），致命错误必须“停止读取”
   触发 EOF，并把原因交给 manager 的回调（`on_fatal`）；否则连接会以「还活着」的样子挂着。
2. **HTTP JSON 响应体上限**：非 SSE 响应由 SDK 全量 `aread()`。适配器传入自定义
   `httpx2.AsyncClient`，在自定义 transport 层按字节上限截断并抛错。
3. **目录上限**：页数、工具数、目录累计字节、单 schema 字节（计划 §4.1 的默认值）。
4. **期限与并发**：每次调用的实际期限取 host 配置与 Run 剩余期限的较小值；并发槽等待也计入期限。
5. **结果归一**：SDK 的 Pydantic 类型（`CallToolResult`）不得进入持久记录；只保留中立结果。

## 5. 依赖锁定与可重现

```text
constraints/mcp.txt                            # 直接 pin：mcp==2.3.0, jsonschema==4.26.0
constraints/mcp-lock-macos-arm64-py312.txt     # 28 个 wheel 的 sha256 闭包（本机主环境）
constraints/mcp-lock-macos-arm64-py310.txt     # 28 个 wheel 的 sha256 闭包（Python 3.10 解析）
```

* 生成命令写在每个 lock 文件头部；校验命令是
  `pip install --require-hashes --no-deps -r constraints/mcp-lock-macos-arm64-py312.txt`，
  已在全新 venv 中实测通过（exit 0，`mcp 2.3.0` 可导入）。
* 3.10 与 3.12 的解析差异只有两项：`rpds-py`（0.30.0 / 2026.9.1）与
  `starlette`（1.7.0 / 1.9.0），其余 26 项版本一致。差异来自 CPython 版本标记与
  `starlette>=0.48.0; python_version >= "3.14"` 之外的解析顺序，记录在案而不是抹平。
* 本机**没有 Python 3.10 解释器**（`~/.pyenv/versions` 只有 3.12.13，系统另有 3.11.15）。
  因此 AC02 的「3.10 实跑」是**环境阻塞**，本轮只能提供 3.10 的解析闭包与静态下界门
  （`tests/test_python_floor.py`）；这不是「已验证 3.10」。

## 6. 独立环境与 import 隔离

* 专用 venv：`.venv-mcp`（3.12.13，`mcp==2.3.0` + pytest）。项目 `.venv` 未被改动，
  其中没有 `mcp`/`mcp_types`/`starlette`/`httpx2` 中的任何一个。
* 在**已装 SDK** 的 `.venv-mcp` 中冷启动遍历导入全部 91 个子模块：

  ```text
  cold import with SDK installed pulled in: []
  ```

  即：装了可选依赖不会因导入而泄漏到基础运行路径。该检查在 MCP02 引入 `sdk.py`
  懒加载后必须仍然成立，`tests/test_import_fence.py` 已把 `mcp`、`mcp_types` 加入
  禁止顶层导入清单。

* 测试目录 `tests/mcp/` 与 SDK 包同名：目录里没有 `__init__.py`，因此 `import mcp` 会
  把测试目录当成命名空间包而“成功”。基座 venv 里必须显式 skip（`support.harness.sdk_available()`），
  MCP 门禁则把 skip 计为失败，不允许用静默 skip 冒充通过。

## 7. 证据复现（MCP00 当次运行）

```bash
# 1. 独立环境与锁定依赖
python3 -m venv .venv-mcp
.venv-mcp/bin/pip install "mcp==2.3.0" pytest pytest-asyncio
.venv-mcp/bin/pip download --only-binary=:all: -r constraints/mcp.txt -d /tmp/mcp-wheelhouse-312
.venv-mcp/bin/python -m pytest -q tests/mcp/test_contract.py     # 5 passed

# 2. 基座环境不受影响（显式 skip，不是错误）
.venv/bin/python -m pytest -q tests/mcp                          # 1 skipped

# 3. 全新 venv 校验哈希闭包
python3 -m venv /tmp/mcp-verify-venv
/tmp/mcp-verify-venv/bin/pip install --require-hashes --no-deps -r constraints/mcp-lock-macos-arm64-py312.txt
```

`tests/mcp/test_contract.py` 的 5 条断言对应：SDK 协议注册表、stdio 两代一次调用、
远端 `isError` 仍是结果而不是异常、HTTP 上高层入口的重发行为。

## 8. 与计划建议的差异与未执行项

| 计划建议 | 实际 | 原因 |
| --- | --- | --- |
| 建议文件 `sdk.py` 等 | MCP00 只落了夹具与合同测试；适配器模块在 MCP02 起落地 | 先证明运行时事实，再写适配层 |
| `mode="auto"` 协商 | 不使用；显式选择 era 并核对协商结果 | auto 含重试/回退，边界不可控 |
| `constraints/mcp-py310.txt` | 采用 `constraints/mcp.txt` + 两个带哈希的 lock 文件 | 同一份 pin 可同时服务两代 Python |

未执行（如实记录，不冒充已完成）：

* Python 3.10 实跑（本机无解释器）。
* HTTP 429/5xx、重定向、凭据边界的一手验证 → MCP03；其源码结论已在 §3 记录，但未实测。
* 服务端强制 `Mcp-Param-*` 校验之外的其他 2026 特性（sampling/elicitation/tasks）→ 本轮明确不支持。
