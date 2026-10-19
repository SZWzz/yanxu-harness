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

## MCP02 — manager、stdio 生命周期、限流/期限/关闭、懒加载

状态：**已完成**（2026-10-19）。

### 交付的模块

| 文件 | 职责 |
| --- | --- |
| `integrations/mcp/config.py` | `McpServerConfig` / `McpLimits` / `SUPPORTED_PROTOCOLS`；协议、端点、命令、env、上限全部在构造时校验；连接 scope = `(server_id, 授权 scope, 配置 revision, 协议, 传输)` |
| `integrations/mcp/sdk.py` | 唯一 import SDK 的地方；`load_sdk()` 缺依赖时报 `McpExtraMissing` 并给出 `pip install 'yanxu-harness[mcp]'` |
| `integrations/mcp/transports.py` | 自写的有界 stdio 传输（公开 `TransportStreams` 契约）：单帧字节上限、stderr 有界尾巴、进程组终止（stdin→grace→SIGTERM→SIGKILL） |
| `integrations/mcp/http.py` | SDK `streamable_http_client` + 自定义 httpx2 transport（响应体字节上限、`Content-Length` 预检、不跟随重定向、凭据只进 header）；MCP03 验收 |
| `integrations/mcp/client.py` | `McpClientManager`：连接复用/隔离、两代握手与协商核对、有界发现、单次调用、并发槽、期限、失效与有界关闭 |
| `integrations/mcp/types.py` | 中立数据：`McpConnection` / `McpToolDescriptor` / `McpCallResult` / `McpCallFacts` |

### 命令与结果

```bash
.venv-mcp/bin/python -m pytest -q tests/mcp           # exit 0, 21 passed（contract 5 + stdio 16）
.venv/bin/python -m pytest -q tests                   # exit 0, 1303 passed, 3 skipped（含 1 个 MCP 显式 skip）
.venv/bin/python -m pytest -q tests/test_import_fence.py tests/test_python_floor.py   # exit 0
```

### 关键 RED / 失败历史

* **SDK 不把读流里的异常当作请求失败**：自写传输把致命错误的异常对象从读流交给
  session，结果挂起请求一直等到期限（真实现象：`TimeoutError`，错误信息为空）。
  修复：致命错误改为「停止读取 + `on_fatal` 回调」，由 EOF 唤醒挂起请求，原因由
  manager 记录并在归类时优先使用。这是 MCP00 ADR 里补记的一条 SDK 事实。
* **anyio 版本差异**：该环境没有 `anyio.subprocess` 模块，`open_process` 的
  stdin/stdout/stderr 默认就是 PIPE。
* **异常组**：传输错误从 task group 里出来时被包在 `ExceptionGroup` 中，导致
  「协商出的版本不是 pin 的版本」被误报成连接失败；新增 `_root_error` 递归取根因。
* **3.10 下界**：`_root_error` 首版用了 `BaseExceptionGroup`（3.11+），被
  `tests/test_python_floor.py` 如实拦下；改为 duck-typing（`exceptions` 属性）。

### 未完成（留给后续阶段）

* Streamable HTTP 的一手验收（401/403/404/重定向/断流/凭据哨兵）→ MCP03，代码已就位但按阶段纪律尚未宣称支持。

## MCP03 — Streamable HTTP、预置凭据、两代协议、one-shot 与断流

状态：**已完成**（2026-10-19）。

### 交付内容

* `integrations/mcp/http.py`：SDK 公开 `streamable_http_client` + 自定义 httpx2
  transport（响应体字节上限、`Content-Length` 预检、不跟随重定向、`trust_env=False`）；
  header 只作为线上凭据，不进任何持久/公开面。
* 真实 loopback 服务：SDK 夹具（两代协议四格）+ `raw_http_server.py`（401/403/404/500/
  跨 origin 307/断流），每个 HTTP 请求写一条 facts（方法、路径、JSON-RPC method、状态）。
* `tests/mcp/test_http.py`：10 条，覆盖两代 HTTP 往返、凭据不外泄、错误不重发、
  跨 origin 重定向不跟随、断流不重发、以及 **2026 HEADER_MISMATCH 不触发补发**。

### 命令与结果

```bash
.venv-mcp/bin/python -m pytest -q tests/mcp/test_http.py   # exit 0, 10 passed
.venv-mcp/bin/python -m pytest -q tests/mcp                # exit 0, 31 passed
```

### 关键 RED / 失败历史

* 自定义响应流不是 `httpx2.AsyncByteStream` 的子类，httpx2 在消费响应时直接断言失败；
  改为在函数内定义子类（同时保持模块导入不拉 httpx2）。
* 断流测试首版把请求数写成 2；真实序列是 `initialize` → `notifications/initialized`
  → `tools/call`，断言改为逐项列出，不猜计数。

### 四格状态（MCP-AC03 依据）

| 传输 \ 协议 | 2025-11-25 | 2026-07-28 |
| --- | --- | --- |
| stdio | ✅ 真实子进程 | ✅ `server/discover` |
| Streamable HTTP | ✅ loopback | ✅ loopback + `server/discover` |

## MCP04–MCP08

状态：未开始。
