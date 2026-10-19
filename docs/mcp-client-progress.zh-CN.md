# MCP Client 实施进度

本文件按阶段记录**实际做过的事**：命令、退出码、观察结果、失败历史与未执行项。
它不替代 [实施计划](mcp-client-implementation-plan.zh-CN.md) 的退出门禁，也不替代
[MCP00 ADR](mcp-client-sdk-baseline.zh-CN.md) 的锁定结论。绿色数字只在对应命令真实跑过之后记录。

分支：`codex/mcp-client`（起点 `6ff45ba`），版本保持 `0.2.0a1`。

## MCP00 — SDK/协议/one-shot/资源上限验证与夹具

状态：**已完成**（2026-10-19）。

### 做了什么

| 项 | 结果 |
| --- | --- |
| 独立 venv `.venv-mcp`（3.12.13）装 `mcp==2.3.0` | 成功；项目 `.venv` 未改动 |
| 协议注册表核对 | 握手代最新 `2025-11-25`；现代代仅 `2026-07-28`；未知版本保守拒绝 |
| 真实 stdio 服务夹具（`tests/mcp/support/server.py`） | 两代协议共用同一进程、同一套工具；facts JSONL + 业务计数器 |
| 真实 Streamable HTTP 服务夹具 | `Server.streamable_http_app` + uvicorn，端口动态分配 |
| one-shot 证明 | `ClientSession.call_tool` 两代两传输各只发 1 次 `tools/call`（出站 tap + 服务端 facts 双层计数） |
| 高层入口风险证明 | `Client.call_tool` 在 HEADER_MISMATCH 后发送 `tools/call → tools/list → tools/call` |
| 依赖锁定 | `constraints/mcp.txt` + 两个带 sha256 的 lock 文件（各 28 个 wheel）；全新 venv `--require-hashes` 安装通过 |
| import 隔离 | 已装 SDK 的环境冷启动遍历 91 个子模块，未拉入 `mcp`/`mcp_types`/`starlette`/`httpx2` |
| extra 声明 | `pyproject.toml` 新增 `mcp = ["mcp==2.3.0", "jsonschema>=4.20.0"]`；基础依赖仍为空 |

### 命令与结果

```bash
.venv-mcp/bin/python -m pytest -q tests/mcp/test_contract.py   # exit 0, 5 passed
.venv/bin/python -m pytest -q tests/mcp                        # exit 0, 1 skipped（显式原因）
.venv/bin/python -m pytest -q tests/test_import_fence.py tests/test_python_floor.py  # exit 0, 16 passed
```

### RED / 失败历史

* 首版 tap 包装只实现了 `send`，SDK 的 dispatcher 要求 `WriteStream` 的异步上下文协议，
  真实 stdio 连接在 `__aenter__` 处报 `TypeError`。修复：补齐 `__aenter__/__aexit__` 转发。
* `tests/mcp/` 目录名与 SDK 包同名，`import mcp` 被目录（命名空间包）满足，基座 venv 里
  `pytest.importorskip("mcp")` 假通过后在下一行 `mcp_types` 处 collection error。
  修复：用 `find_spec("mcp.client.session")` 判断，基座显式 skip，契约测试模块级实测。
* 首版哈希 lock 生成脚本把 `sha256:` 前缀写了两次（`--hash=sha256:sha256:...`），
  已整文件重生成并复核 `grep -c "sha256:sha256"` 为 0。

### 未执行（如实记录）

* Python 3.10 实跑：本机无 3.10 解释器（仅 3.11.15/3.12.13），记为环境阻塞。
* HTTP 429/5xx/重定向/凭据边界的一手验证：留待 MCP03。

## MCP01 — 中立执行结果与 operation context

状态：**已完成**（2026-10-19）。

### 做了什么

| 项 | 结果 |
| --- | --- |
| 中立 `ToolExecutionResult`（`tools/execution.py`） | `state`（仅 SUCCEEDED/FAILED/UNKNOWN）、可持久化 `result`、`detail`、有界白名单 `details`（32 键 / 4000 字符）、可选 `external_id`；不携带 operation id |
| 归一函数 `receipt_from_handler_result` | 普通返回值仍是 SUCCEEDED（原语义不变）；中立结果按声明落 receipt；operation id 永远取 intent 的 |
| 不可表示的结果 | mutating 工具→UNKNOWN，只读工具→FAILED，在 settle 前裁定，不再变成 settle 期崩溃 |
| `storable_value()` | 把「能进账本」收敛成一份实现，SQLite 的 `_jsonable` 改为委托它 |
| operation context | 新增 `RunContext.for_operation(operation_id)`；Gateway 在执行前用它在 RunContext 上盖上权威 operation id；宿主自定义 context 原样透传 |
| 两个后端 | 内存 `RecordingExecutionBackend` 与 `SqliteExecutionBackend` 复用同一归一函数 |
| 兼容边界 | host-owned backend（`ownership="host"`）仍只有一次 intent / 一次 receipt / 一次 settle |

### 命令与结果

```bash
.venv/bin/python -m pytest -q tests/test_tools_execution_results.py   # exit 0, 14 passed
.venv/bin/python -m pytest -q tests/test_tools_execution.py tests/test_tools_gateway.py tests/test_tools_core_bridge.py
.venv/bin/python -m pytest -q tests                                  # exit 0, 1304 passed, 2 skipped
```

### RED / 失败历史

* 先落类型与归一函数、**不接线**，红测是行为性的：6 条接线断言失败（中立结果被当成功、
  handler 看不到 operation id、SQLite 存取仍是 succeeded），不是 collection ImportError。
* `storable_value` 首版只处理 pydantic/dataclass/set，却忘了 JSON 原生类型——普通字典在
  `json.dumps(default=...)` 里从不会走到这个钩子，直接调用就会误报「不可存」。
  修复：JSON 原生类型原样通过，递归处理 list/mapping。
* 一条测试自身写错：两个工具用了同一个 `call_id`，而派生的 operation key 不含工具名，
  第二次调用被正确识别成 replay。测试改用不同 call id；没有为了变绿而放宽断言。

## MCP02–MCP08

状态：未开始。
