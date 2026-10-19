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

## MCP04 — bounded discovery、Schema、名称映射、CatalogSnapshot、绑定与漂移

状态：**已完成**（2026-10-19）。

### 交付的模块

| 文件 | 职责 |
| --- | --- |
| `integrations/mcp/schema.py` | `RemoteToolSchema`：真 jsonschema 校验（默认 2020-12、显式 draft-07、同文档 `$ref`）；远程 `$ref`、未知 dialect、非法/超深/超节点/超字节 schema 在注册前拒绝；参数深度/节点/字节有界；验证返回原样 mapping（不补 default、不转类型） |
| `integrations/mcp/catalog.py` | `local_alias`（`mcp.<server_slug>.<tool_slug>.<digest12>`≤ 64，摘要覆盖 server_id + 原始名）、`McpToolBinding`（宿主授权；写工具无 preview 直接拒绝）、`CatalogSnapshot`（不可变、只含宿主绑定项、其余为有原因的 exclusion）、带版本号的 `definition_version`、`freeze_binding` / `check_binding` 漂移判定 |
| `integrations/mcp/tools.py` | `ToolDefinition` 构造函数：真 validator、写工具同步本地 preview（不发网络）、`identity_shaped_fields` 供宿主拒绝身份形字段 |
| `manager.list_tools` | 改为返回 `McpToolDirectory`（descriptors + pages_read + complete）；分页用 SDK 公开 `PaginatedRequestParams` |

### 命令与结果

```bash
.venv-mcp/bin/python -m pytest -q tests/mcp/test_schema.py tests/mcp/test_catalog.py   # exit 0
.venv-mcp/bin/python -m pytest -q tests/mcp                                            # exit 0, 53 passed
.venv/bin/python -m pytest -q tests                                                    # exit 0, 1304 passed, 6 skipped
```

### 关键 RED / 失败历史

* 目录可发现不等于可暴露：一条断言要求“只含宿主绑定项且有原因排除其余”，首版只断言了
  条目存在；改成逐项列名字，避免“差不多就行”。
* 漂移测试首版把「policy 变更」拿旧 snapshot 去比，结果正确地通过了；真实恢复流程是
  「用当前绑定重新冻结目录」，测试改成重建 snapshot 再比。
* 定义层把 validator 的所有失败归一成 `ToolArgsInvalid`（既有契约），测试改按这层语义断言。
* 非法 schema 的「节点超限」用例先撞上字节上限；改为放宽字节上限后单独验节点。
* 相对导入层级写错（`..tools` → `...tools`），被导入即报错扬出。

### 说明（不掩盖差异）

* 2026-07-28 上 SDK 会在 `tools/list` 后过滤掉 `x-mcp-header` 声明非法的工具；
  适配器看到的就是过滤后的列表，无法自行枚举被丢弃项来解释原因（ADR §3 已记录）。
* 漂移拒绝已可独立调用；把它接到「原 Run 恢复前拒绝」的整条链路上属于 MCP05/MCP06。

## MCP05 — core_tool/Gateway 真实执行、审批、结果 codec、错误与期限

状态：**已完成**（2026-10-19）。

### 交付的模块

| 文件 | 职责 |
| --- | --- |
| `integrations/mcp/results.py` | 结果 codec：成功 → SUCCEEDED（文本 + 结构化内容的确定性 TextContent 表示）；远端 `isError` → **FAILED** 且错误正文进 detail（模型看得到），完整内容留在 receipt；非 terminal/不支持媒体 → 明确 unsupported（mutating UNKNOWN / 只读 FAILED），绝不当空成功；模型视图有界（8000 字符）、结构化渲染 4000 字符 |
| `integrations/mcp/adapter.py` | `McpToolAdapter`：生成定义、单次远端调用、期限取「配置上限 ∩ 宿主 Run 剩余期限（`remaining_deadline_seconds` label）」、把错误分为「可证明未派发」与「可能已到达」，调用事实进 receipt details |
| 传输错误分类 | 出站帧超限改为可证明未派发（`McpNotSent`）；连接在派发前已知死亡也是未派发 |

### 命令与结果

```bash
.venv-mcp/bin/python -m pytest -q tests/mcp   # exit 0, 62 passed
.venv/bin/python -m pytest -q tests           # exit 0, 1304 passed, 7 skipped
```

### 状态矩阵（真实 Gateway + SQLite + MCP 子进程）

| 场景 | 结果 | 证据 |
| --- | --- | --- |
| 成功 | EXECUTED / succeeded；结果进入下一个模型请求 | 真 `DefaultHarness` 两轮脚本 provider + SQLite 行 + 服务端 facts=1 |
| 远端 terminal 错误 | FAILED / failed，模型看到错误正文；不重试 | receipt `remote_is_error` + 服务端调用数 1 |
| 不支持媒体 | FAILED（只读）明确报不支持，非空成功 | `unsupported_kind=media_or_shape` |
| 写工具未审批 | AWAITING_APPROVAL，远端调用 0；批准后一次 | 业务计数器 = 1 |
| 派发后超时 | UNKNOWN + `unknown` 挂起，不自动重放 | 服务端已收到该次调用（facts=1） |
| 派发前取消 | CANCELLED，零远端调用 | facts 为空 |
| 连接已死 | FAILED + `not_sent=true`，零调用 | facts 为空 |

### 关键 RED / 失败历史

* 审批测试首版自己拼 `ApprovalBinding`；实际契约是 store 的 `decide(binding_key, ...)`，
  测试沿用了真实接口。
* “连接已死”用例共用了同一个 facts 文件，把前一个部署的 `die` 计数当成了本部署的调用；
  改成独立目录，避免证据互相污染。
* 新增 `image` 工具后两条目录断言（排除集与总数）如实变红并更新。

## MCP06 — 故障恢复、业务证据提升、facts 与只读诊断

状态：**已完成**（2026-10-19）。

### 交付内容

* 夹具新增 `bump_then_die`：**先写业务记录再退出**，复现「效果已发生、回包丢了」的真实窗口；
* 适配器把目录身份（alias / tool_version / remote_tool / policy_revision / effects）与
  连接事实（connection_id / generation / transport / protocol / dispatched / duration /
  result_source / not_sent）一起放进 receipt details；
* `integrations/mcp/diagnostics.py`：`safe_call_facts`（白名单 + 有界，错误正文/参数/结果
  一律不投影）与 `safe_config_facts`（连 command/url/argv 都不给，env/header 只给名字与计数）；
  纯函数，不连接、不起进程。

### 命令与结果

```bash
.venv-mcp/bin/python -m pytest -q tests/mcp/test_recovery.py tests/mcp/test_diagnostics.py   # exit 0
.venv-mcp/bin/python -m pytest -q tests/mcp   # exit 0, 72 passed
.venv/bin/python -m pytest -q tests           # exit 0, 1304 passed, 9 skipped
```

### 证据（每条都对着真实产物）

| 断言 | 证据 |
| --- | --- |
| 真实故障窗口 | 远端业务文件在回包前已写入 1 次；服务端 facts 有 `business_written`；账本行 `unknown`、result 为 NULL |
| unknown 屏障 | 同 binding 再 `review` → UNKNOWN；再 `invoke` → UNKNOWN；服务端 `tools/call` 仍为 1 |
| 第二进程不把 open 当 absent | 新进程以只读方式打开同一 SQLite，读到 `unknown` |
| 回执复用 | 已结算的调用连 `review` 两次 → REPLAYED，handler 不再运行，服务端调用数 1 |
| 证据提升 | `promote(state=succeeded, source=…)` → 账本 `succeeded` + `reconciled`；随后 review → REPLAYED |
| 提升不可重复 | 对已结算行再次 promote → `ContractError`（账本不允许重新打开已证明的事实） |
| 秘密哨兵 | 诊断投影里找不到 `diag-secret-sentinel`，也没有 command/argv/url |
| 诊断只读 | 导入 `diagnostics` 的子进程里没有 `mcp`/`mcp_types`/`anyio`/`httpx2`/`jsonschema` |

### 关键 RED / 失败历史

* 首版 review 用的 invocation 丢了 `session_id`，于是被正确判为**另一个**操作（absent）。
  这正是 operation key 该有的行为，测试改为带上完整身份。
* 「重复 promote」首版写成幂等，实际账本语义是**拒绝**（已证明的事实不重开）；按真实契约收紧断言。
* 夹具每加一个工具，目录断言就会如实变红（11 → 12 → 13 与排除集）；每次都更新而不是改成集合包含。

## MCP07 — 固定场景、独立探针、两个离线示例

状态：**已完成**（2026-10-19）。

### 交付内容

| 产物 | 内容 |
| --- | --- |
| `examples/11_mcp_stdio.py` | 自带一个真 MCP stdio 服务端（临时文件 + 子进程），发现 → 冻结目录（读/写绑定）→ 真 Gateway + SQLite → 一次读调用与一次写调用 |
| `examples/12_mcp_http_recovery.py` | 自带一个真 loopback Streamable HTTP 服务端；写工具**先写业务记录再退出**，演示 UNKNOWN → 业务证据 → promote → 回执复用 |
| `scripts/probe_mcp_acceptance.py` | 独立探针（不依赖 pytest 与仓库夹具）：四格 + 服务端计数 + 未知窗口 + 未知协议拒绝 + 导入隔离；`--report` 写 JSON，退出码即结论 |
| `tests/mcp/test_examples.py` | 把两个示例当子进程真跑，断言输出行与「无残留服务进程」 |

### 命令与结果

```bash
.venv-mcp/bin/python examples/11_mcp_stdio.py           # exit 0
.venv-mcp/bin/python examples/12_mcp_http_recovery.py   # exit 0
.venv-mcp/bin/python scripts/probe_mcp_acceptance.py --report /tmp/mcp-probe.json   # exit 0
.venv-mcp/bin/python -m pytest -q tests/mcp             # exit 0, 75 passed
```

探针当次输出（摘要）：

```text
ok   sdk_facts {mcp: 2.3.0, mcp_types: 2.3.0, jsonschema: 4.26.0, latest_handshake: 2025-11-25, modern: [2026-07-28]}
ok   import_isolation {leaked: []}
ok   unknown_protocol_refused {reason: not one of the pinned revisions}
ok   grid:stdio:2025-11-25 {server_side_calls: 1}
ok   grid:stdio:2026-07-28 {server_side_calls: 1}
ok   grid:http:2025-11-25 {server_side_calls: 1}
ok   grid:http:2026-07-28 {server_side_calls: 1}
ok   unknown_window {business_file: 1, ledger_state: unknown, review: unknown, second_attempt: unknown, server_side_calls: 1}
```

### 关键 RED / 失败历史

* 示例首版按别名后缀选参数（`name.endswith(".echo")`），而别名带摘要后缀；改为按
  目录里的 `remote_name` 选，这也是「别名不是远端名字」的一个小现场。
* 示例的「无残留进程」断言首版顺手比较了全部进程集合，被无关进程干扰；收窄为
  只查示例服务端进程名。

## MCP08

状态：**已完成**（2026-10-19）。

### 交付内容

| 产物 | 内容 |
| --- | --- |
| `docs/mcp-client.zh-CN.md` | 使用说明：最短路径、配置与上限、发现/命名/漂移、结果与失败语义、恢复、诊断、示例、已知边界 |
| `scripts/mcp_gate.py` | 三条安装门禁：base（无 SDK）/ extra（wheel）/ extra（sdist）；树外安装、`tests/mcp` 传参要求 **0 skip**、探针与两个示例、导入隔离 |
| `docs/mcp-client-delivery-report.zh-CN.md` | 交付报告：提交清单、锁定表、四格、MCP-AC01–32、兼容性变化、门禁输出、未支持范围与复核入口 |
| README（中/英）、CHANGELOG、`docs/adapters.md`、`docs/contract.md`、`docs/recovery.md`、`docs/verification.md`、`docs/current-harness-architecture.zh-CN.md` | 可选 extra 的安装/边界/依赖方向/恢复语义/验证记录入口 |

### 关键 RED / 失败历史

* 基座门禁捕到一个**真 bug**：`sdk_available()` 在冷进程里用 `importlib.util.find_spec`，
  而模块只 `import importlib`；在检出目录里因为 pytest 先导入过 `importlib.util` 而侥幸通过。
  修复为显式 `import importlib.util`——这正是「基础环境冷启动」这条门禁存在的理由。
* 树外复制目录没有 `pyproject.toml`，`asyncio_mode = auto` 丢失，54 条 async 测试全部误报失败；
  门禁改为在副本里写一份最小 `pytest.ini`（只含这一条设置）。
* 首版报告的 `ok` 把「Python 3.10 无解释器」这个**阻塞记录**当成了门禁失败；
  改为单独打印 `BLOCKED python_floor`，不计入门禁结论。

### 命令与结果

```bash
.venv-mcp/bin/python -m pytest -q tests/mcp     # exit 0, 77 passed（含 AC29 的任务集成用例）
.venv/bin/python -m pytest -q tests             # exit 0, 1304 passed
.venv/bin/python scripts/mcp_gate.py --workdir /tmp/mcp-gate --report /tmp/mcp-gate-report.json
```

### 门禁后补强（针对独立复核意见）

独立复核指出 AC29 只有子集构造的弱证据。补上
`tests/mcp/test_tasks.py::test_the_child_gets_only_the_delegated_subset_and_unknown_changes_nothing`：
真 `ChildCoordinator`（scope 外工具拒绝、超 cap grant 拒绝且不写记录）、真 `TaskBudget`/`BudgetLedger`
与 `DispatchBudget`、真 `DefaultHarness` 子运行，并且 unknown 窗口子运行结束后父子 deadline 不变、
只多一条 dispatch reservation、同 dispatch identity 重复预留幂等；等待审批的子运行不留 intent/receipt。

门禁结果见交付报告 §4；版本仍为 `0.2.0a1`，未构建新发行物、未上传、未推 tag。

## MCP09 — 独立复核四项阻断（MCP-R1–R4）与其故障窗口

状态：**已完成**（修复后复跑通过；仍待独立复核者确认）。

起点：独立验收报告 [mcp-client-independent-review.zh-CN.md](mcp-client-independent-review.zh-CN.md)
在 `48b8fd1` 上以 `scripts/probe_mcp_independent_review.py` 复现 0 passed / 4 failed。本次先保留
该探针与报告不动，把四项行为断言接进默认专项测试，再逐项修复。

### 做了什么

| 缺口 | 根因 | 修复 | 默认测试 |
| --- | --- | --- | --- |
| R1 `$dynamicRef` 触发远端 schema 读取 | `_remote_refs()` 只看键名 `$ref`；validator 未绑定 registry | 引用扫描覆盖 `$ref`/`$dynamicRef`/`$recursiveRef`（输入**和**输出 schema 共用 `check_schema_document`）；`jsonschema` validator 绑定 `referencing.Registry(retrieve=...)`，retrieve 一律 `NoSuchResource` | `test_dynamic_and_recursive_refs_are_refused_without_a_fetch`（loopback 计数 0）、`test_an_output_schema_with_a_remote_reference_is_refused` |
| R2 已观察到的目录漂移仍可派发 | `invoke()` 不调用 `check_binding()`；`discover_catalog()` 只返回新对象 | 新增 `PublishedCatalog`/`McpCatalogRegistry`：manager 按 connection scope 记录当前可信目录；`discover_catalog` 发布；新连接代次/manager 关闭使旧观察失效；适配器 `catalog_check()` 在每次 `invoke` 前比对冻结绑定；`ToolDefinition`/`ToolSpec` 新增 `dispatch_guard`，`DefaultHarness` 在 resume 派发前调用并以 `ResumeRefused(reason_code="binding_drift")` 拒绝 | `test_observed_catalog_drift_refuses_resume_with_zero_dispatch`（真实暂停→真实重新发现→批准→resume：远端 0、模型请求 0、原等待保留） |
| R3 远端回显凭据进入账本与模型结果 | 错误正文直通 `detail`/`error_text`/receipt.result | `SecretRedactor` 在适配边界清洗远端文本、嵌套结构化值、异常信息和最终 neutral result；来源为宿主 headers/env（凭据形状键）/argv，含 scheme 拆分、base64、URL 编码、JSON 转义形式；不写入通用 facts 供后处理 | `test_remote_credential_echo_is_redacted_everywhere`（真实 loopback HTTP header 回显：成功正文、`isError` 正文、嵌套 structured、账本） |
| R4 绑定工具缺失仍返回 complete 空目录 | `build_snapshot` 只遍历已发现描述符 | 发现后核对 bindings 全集，缺失项列出并 `McpConfigurationError`；「目录完整」与「绑定组装完整」分开表述；被拒的装配不发布为可信目录 | `test_a_bound_tool_missing_from_a_complete_directory_is_refused` |

### 补上的原计划故障窗口（报告 §6）

`tests/mcp/test_fault_windows.py` + `tests/mcp/support/mcp_fault_child.py`：子进程在精确
commit/settle 边界 `os._exit`，父进程只读账本与 Run store 判定。

| AC | 场景 | 断言 |
| --- | --- | --- |
| 19 | 真实 MCP 部署注入 `admit` 失败 | 服务端 `tools/call` facts 为空 |
| 21 | `admit` 提交后 `os._exit(9)` | 新进程只读 `unsettled()` 看到 `running` 行，远端 0 调用 |
| 22 | `echo` 成功后于 tool-result commit 内退出 | 新进程恢复 completed，`tools/call` 仍 1，账本 1 条 succeeded |
| 25 | unknown→`promote` 后、Run 更新前退出 | 再一进程恢复 completed，`tools/call` 仍 1，业务计数 1 |
| 26 | A 成功/B unknown 且 settle 后退出/C 未发 | resume 以 reconciliation 阻塞（C 无 intent）；promote B 后 C 恰好一次 |

### 命令与结果

```bash
.venv-mcp/bin/python -m pytest -q tests/mcp                                      # exit 0, 90 passed
.venv-mcp/bin/python scripts/probe_mcp_independent_review.py --report /tmp/mcp-independent-review.json  # exit 0, 4/4
.venv-mcp/bin/python scripts/probe_mcp_acceptance.py --report /tmp/mcp-original-acceptance.json          # exit 0, 8/8
.venv-mcp/bin/python scripts/mcp_gate.py --workdir /tmp/mcp-gate-final --report /tmp/mcp-gate-final-report.json  # exit 0
```

### 关键 RED / 失败历史

* 两条新增 drift 断言首版只覆盖 harness 侧，`adapter.invoke()` 仍会放行；补上「用旧适配器
  条目直接 invoke」后先红后绿，才把「适配器承诺」与「harness 接线」分开固定住。
* 故障子进程最初用「第二次模型请求前退出」，测到的是通用 F05 窗口；改为在
  `SqlitePersistence.commit_transition` 内按 `toolResult` 判定退出，才是 AC22 的 MCP 现场。
* 跨进程恢复会被死进程留下的 lease 拦住（`RunBusy`），测试用显式 `clock` 越过租期，
  不改生产代码。
* `SNAPSHOT.json` 清单门禁先红（5 个新文件未登记），刷新后全绿——这是清单继续生效的证据。
