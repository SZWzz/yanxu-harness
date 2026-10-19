# MCP Client 使用说明（可选 extra）

适用版本：`0.3.0a1`。这是**可选**适配器：基础安装仍然是零第三方依赖、Python 3.10+；
只有显式安装 extra 才会引入 MCP SDK，而且 SDK 只在真正启用时导入。

```bash
pip install "/path/to/yanxu_harness-0.3.0a1-py3-none-any.whl[mcp]"
```

首版范围：MCP **Tools**，传输 stdio 与 Streamable HTTP，协议 `2025-11-25` 与
`2026-07-28`。其他协议版本由配置层直接拒绝，不会「试一下」。

不做：Resources/Prompts 主动读取、自动 OAuth 登录与 token 刷新、自动安装/启动服务、
sampling/elicitation、MCP Tasks、工具调用自动重试、图像/音频/blob 处理（返回明确的不支持
结果）、第二份操作账本或第二个模型循环。

## 1. 最短路径

```python
import sys
from yanxu_harness.integrations.mcp import (
    McpClientManager, McpServerConfig, McpToolAdapter, McpToolBinding,
    McpTransport, discover_catalog, local_alias,
)
from yanxu_harness.storage import SqliteExecutionBackend
from yanxu_harness.tools import (
    ConservativePolicy, GatewayBindings, InMemoryApprovalStore,
    ToolEffect, ToolGateway, ToolRegistry, core_tool,
)

config = McpServerConfig(
    server_id="notes",                        # 宿主本地稳定 id
    transport=McpTransport.STDIO,
    protocol="2025-11-25",                    # 必须显式 pin
    authorization_scope="notes-v3",           # 凭据/授权 scope revision
    command=sys.executable, args=("notes_mcp.py",), env={"NOTES_DB": "/srv/notes.db"},
    # limits=McpLimits(call_timeout=20.0),   # 需要时显式调整
)

bindings = {
    "notes_search": McpToolBinding("notes_search", effects=frozenset({ToolEffect.READ})),
    "notes_write": McpToolBinding(
        "notes_write",
        effects=frozenset({ToolEffect.WRITE}),
        preview=lambda ctx, args: {"path": args["path"], "action": "replace"},   # 写工具必须有
    ),
}

async def main():
    async with McpClientManager() as manager:
        connection = await manager.connect(config)
        snapshot = await discover_catalog(manager, connection, config=config, bindings=bindings)
        adapter = McpToolAdapter(
            manager=manager, connection=connection, config=config, snapshot=snapshot
        )
        gateway = ToolGateway(
            registry=ToolRegistry(list(adapter.definitions())),
            policy=ConservativePolicy(),                      # 写工具需要审批
            approvals=InMemoryApprovalStore(),
            backend=SqliteExecutionBackend("/var/lib/app/operations.sqlite"),
        )
        for definition in adapter.definitions():
            spec = core_tool(definition, GatewayBindings(gateway=gateway, context=run_context))
            # spec 就是交给 DefaultHarness(tools=[...]) 的东西
```

要点：`McpClientManager` 由宿主在跑 Run 之前打开、在**同一个事件循环**里关闭；
工具定义由 `discover_catalog` + 显式绑定产生；执行走既有 `ToolGateway`，因此可见性、
参数校验、审批、intent、receipt 和恢复语义与本地工具完全一致。

## 2. 配置与边界

| 字段 | 说明 |
| --- | --- |
| `server_id` | 宿主本地稳定 id（小写）；`serverInfo.name` 不是身份，也不能覆盖它 |
| `transport` | `stdio` 或 `streamable_http` |
| `protocol` | 只接受 `2025-11-25`、`2026-07-28`；协商结果与 pin 不一致即拒绝 |
| `authorization_scope` / `config_revision` | 连接按 `(server_id, scope, revision, protocol, transport)` 隔离；跨 scope 不共享连接 |
| `command`/`args`/`cwd`/`env` | stdio：不使用 shell；只继承 `HOME/LOGNAME/PATH/SHELL/TERM/USER` 加显式 env；服务端 stderr 另管（有界尾巴） |
| `url`/`headers` | HTTP：优先 https；loopback http 需要 `allow_loopback_http=True`；URL 内不得带凭据或 query；header 只上线，不进日志/持久面 |
| `limits` | 连接/发现/调用/关闭期限，并发槽，页数/工具数/目录字节/schema 字节/单帧字节/stderr 字节；全部必须为正且有限 |

默认上限（`McpLimits`）：connect 10s、discovery 15s、call 30s、close grace 5s；
每服务并发 4、每授权 scope 8；16 页、256 个工具、目录 2 MiB、单 schema 64 KiB、
单帧 1 MiB、stderr 64 KiB。宿主可以改大改小，服务端的任何自述都不能。

## 3. 发现、命名与漂移

* 远端工具名**保留原始大小写**；本地别名是 `mcp.<server_slug>.<tool_slug>.<digest12>`
  （总长 ≤ 64，符合工具定义的小写稳定 id 规则）。摘要覆盖 `server_id` + 原始名，
  因此大小写折叠或截断不会让两个工具共用一个别名。
* 目录**不是授权**：`discover_catalog` 只会把宿主在 `bindings` 里点名的工具变成条目，
  其余进 `snapshot.exclusions` 并写明原因。`readOnlyHint`/`destructiveHint`/
  `idempotentHint`、描述和 instructions 都只是数据。
* **目录完整 ≠ 绑定组装完整**：发现完成后会核对 `bindings` 全集；宿主点名了但服务端
  没有提供的工具（包括被 SDK 过滤掉的那些）会让装配直接失败并列出缺失项，不再返回一个
  `complete=True` 的空目录。合法的额外未绑定工具仍只进 exclusions，不扩权。
* **已观察到的目录变化一定阻断派发**：`discover_catalog` 会把本次观察登记为该连接 scope
  的当前可信目录；旧适配器在每次 `invoke` 前都比对自己的冻结快照，重新发现、撤权、工具
  缺失、定义版本变化、配置 revision 变化或连接代次更换都会零调用拒绝。
  `adapter.definitions()` 生成的每个 `ToolDefinition` 还带一个同步、无 I/O 的
  `dispatch_guard`；`DefaultHarness` 在 resume 派发前调用它，漂移时抛
  `ResumeRefused(reason_code="binding_drift")` 而不是把拒绝变成普通工具失败继续问模型。
  适配器不后台轮询目录：刷新由宿主显式调用 `discover_catalog()` 驱动，因此
  “发现与远端执行之间”的不可控竞态仍存在（只是不再会被客户端已读到的变化绕过）。
* 定义版本是一个带版本号的 canonical fingerprint（服务身份、配置/授权 revision、传输、
  协议、原始名、输入/输出 schema、模型可见说明、annotations、绑定的效果/preview/policy）。
  派发前把它写进原 arrangement；恢复时用 `check_binding` 与**原冻结版本**比较，
  缺工具/定义变化/撤权/协议或 scope 改变都会得到带 code 的拒绝（`missing`/`changed`/
  `revoked`/`policy_changed`/`protocol_changed`/`config_revision_changed`/
  `authorization_revoked`），零远端调用、零后续模型请求。
* 参数校验用真的 JSON Schema 校验器：默认 2020-12，显式 draft-07 也接受；支持嵌套
  对象/数组、`required`、`enum`、`additionalProperties`、`oneOf`/`anyOf`、同文档 `$ref`。
  会触发外部解析的引用关键字（`$ref`/`$dynamicRef`/`$recursiveRef`）在注册前拒绝，输入与
  输出 schema 都查；实际 validator 另外绑定一个 retrieve 永远失败的 `referencing.Registry`，
  所以即使扫描漏掉某种形状也只能校验失败，不会让宿主发起请求。未知 dialect、非法或过复杂
  的 schema 同样在注册前拒绝；校验不改参数（不补 default、不转类型、不删键）。

## 4. 结果与失败语义

| 观察 | 账本/模型看到的 |
| --- | --- |
| 完整合法 terminal 成功 | `succeeded`；文本按序、结构化内容另有一份确定性文本表示 |
| 远端 `isError=true` | `failed`（**不是**成功回执）；有界错误正文进入模型可见的 detail，完整内容留在 receipt；不自动重试，也不表示业务已回滚 |
| 参数非法 / 未批准 / 被拒绝 | `invalid_args` / `awaiting_approval` / `denied`，远端调用为 0 |
| 已准入、可证明未派发（并发槽等待超时、连接已知死亡、出站帧超限） | `failed` + `not_sent=true` |
| 可能已到达远端（超时、断流、帧超限、无法解析） | `unknown`，Run 挂起等待核对；**绝不自动重放** |
| 非 terminal / 需要更多交互 / 媒体不支持 | 明确的不支持结果：mutating 工具 `unknown`，只读工具 `failed`；不会变成空成功 |
| 取消 | 派发前取消是控制信号（`cancelled`，零调用）；派发后取消按 `unknown` 处理 |

一次准入最多一次 `tools/call`：适配器只使用公开低层 `ClientSession`，不使用在
HEADER_MISMATCH 后会重发的高层入口；这有真实请求计数证据（见交付报告）。

## 5. 恢复

```python
reviewed = await gateway.review(invocation, context)      # 读，不跑
if reviewed is not None and reviewed.status.value == "unknown":
    # 只有业务证据能收敛 unknown；promote 之后 Run 才能继续
    await gateway.promote(invocation, {
        "state": "succeeded",
        "source": "remote ledger: notes#4711",
        "detail": "the note exists with the approved content",
    })
```

* 单次远端调用 → 单条 receipt：断线重连只建立新连接，不授予重发旧操作的权利。
* 重连、ping、服务重新上线、工具自称幂等、`list_tools` 没变化，都**不是**旧写入未执行的证据。
* 已经结算的操作由回执复用，不再触发 handler；`unknown` 必须由业务证据（参数里的业务目标、
  目标版本、业务操作记录或远端回执查询）收敛。

## 6. 诊断与隐私

`yanxu_harness.integrations.mcp.diagnostics` 只提供纯函数：

* `safe_call_facts(receipt_details)`：只投影白名单字段（server_id、远端工具名、定义版本、
  协议、传输、连接代次、耗时、是否派发、结果来源），字符串有界；错误正文、参数、结果一律不投影。
* `safe_config_facts(config)`：只有身份与计数，没有 command/argv/url，也没有 env/header 的值。

诊断不连接、不调用、不起进程；已知凭据值在持久/公开面都会脱敏，服务端 stderr 只保留有界尾巴。

适配器在**自己这一层**还有一道更早的边界：远端返回的文本、嵌套结构化内容和异常信息，在变成
中性结果、账本回执、事件和下一次模型请求之前，会先用宿主自己的 `headers`/凭据形状的 `env`、
`argv` 值做替换（含 `Bearer` 之类 scheme 拆分、base64、URL 编码和 JSON 转义形式），替换成
`[REDACTED]`。错误状态与可用的非秘密正文保留。凭据值不会被写进通用 facts 供后处理再扫。

## 7. 运行示例与探针（不需要真实模型或凭据）

```bash
python examples/11_mcp_stdio.py            # 真 stdio 子进程 + 真 Gateway + SQLite
python examples/12_mcp_http_recovery.py    # loopback HTTP + unknown 窗口 + 证据收敛
python scripts/probe_mcp_acceptance.py --report /tmp/mcp-probe.json
```

## 8. 已知边界

* 远端写入的并发保护取决于远端是否真的提供 CAS；适配器不会凭空承诺目标版本。
* 2026-07-28 上 SDK 会先过滤掉 `x-mcp-header` 声明非法的工具，适配器看到的就是过滤后的
  列表，因此这类工具只会「不出现在目录里」，无法逐条解释原因。
* 只有 macOS/Linux 的 stdio 进程组清理经过实跑；Windows 的 Job Object 路径未验证。
* 未支持：交互式 OAuth、Resources/Prompts、多轮交互（sampling/elicitation/tasks）、
  远端媒体类型、跨平台凭据代理。
* 凭据清洗的范围是「宿主自己知道的值」。远端凭空构造一个宿主从未使用过的秘密无法被识别。
* 目录刷新由宿主显式驱动；客户端不轮询、也不声称消除发现与远端执行之间的竞态。
