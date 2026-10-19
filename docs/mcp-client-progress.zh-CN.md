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

状态：进行中。

（待填：RED 断言、实现、回归命令与结果。）

## MCP02–MCP08

状态：未开始。
