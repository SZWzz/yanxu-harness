# MCP Client 独立验收：四项阻断缺口

日期：2026-10-19（Asia/Shanghai）。核对提交：`48b8fd1aa488de63abf7d2c16c5315623a05effb`，分支 `codex/mcp-client`。检查开始时工作树干净。依据：[实施计划](mcp-client-implementation-plan.zh-CN.md)、[DeepSeek 交付报告](mcp-client-delivery-report.zh-CN.md)。

**结论：本次独立验收未通过。已复现三项 P1、一项 P2；保留现有实现与已通过测试，先修复这些缺口，再独立复跑和决定合并/升级。**

本次只添加复现脚本、本报告和对应快照清单，不修改生产代码、不弱化原测试。未调用付费 Provider、未使用真实凭据、未访问 Yanxu 资料或更换其发行物。

## 1. 实际运行结果

环境：Python 3.12.13 / macOS arm64，现有专用 `.venv-mcp`，SDK `mcp==2.3.0`。

| 检查 | 独立结果 | 说明 |
| --- | --- | --- |
| `.venv-mcp/bin/python -m pytest -q tests/mcp` | **77 passed，26.22s，exit 0** | 现有专项套件实际通过 |
| 原 `scripts/probe_mcp_acceptance.py` | **8 场景 ok，exit 0** | 两代两传输、导入隔离与原 unknown 场景通过 |
| 新 [独立缺口探针](../scripts/probe_mcp_independent_review.py) | **0 passed / 4 failed，exit 1** | 四项均为实际行为复现，不是缺模块/收集失败 |
| 原始场景重复核对 | 同样复现四项 | 首次临时探针与仓库内正式探针各执行一次 |

脚本使用真实 loopback HTTP、真实 stdio 子进程、真实 Harness/Gateway、审批和 SQLite；模型回答由脚本提供。合成凭据只报告“是否出现”，输出不包含任何真实秘密。每次临时目录保存故障事实，`--report` 另写摘要。

未重跑完整安装 gate、全套旧回归、跨平台和 Python 3.10；在已确认阻断问题关闭前，不把额外绿色总数用作本轮通过结论。

## 2. MCP-R1 / P1：`$dynamicRef` 绕过禁止远端 Schema 读取

位置：[schema.py 的引用检查](../src/yanxu_harness/integrations/mcp/schema.py#L71)、[校验器构造](../src/yanxu_harness/integrations/mcp/schema.py#L161)。

`_remote_refs()` 仅检查键名 `$ref`；2020-12 的 `$dynamicRef` 未检查，构造 jsonschema validator 时也没有绑定“禁止外部读取”的 registry。一个合法注册的远端输入 schema 可以因此在后续校验时让宿主发起额外请求。

真实复现：注册 `{"type":"object","$dynamicRef":"http://127.0.0.1:<port>/unwanted-schema-fetch"}`，再校验空参数。独立 HTTP 监听器实际收到 **1 次 GET**，注册和验证均成功。

这违反计划 §§5.2、9 / AC09 的“远端引用禁止网络解析”，也使目录中的服务端文本能够决定宿主额外访问的地址。只修 `$ref` 字面过滤不足以建立该边界。

修复要求：

- 按支持 dialect 检查实际可用的引用机制（包括 `$dynamicRef`），不把会产生外部解析的 schema 注册为可执行。
- 为实际 validator 配置显式、禁止外部 retrieval 的引用注册表，形成独立防线；输入和输出 schema 路径都核对，不能只改字符串检查。
- 增加 loopback 请求计数断言：注册即明确拒绝、额外网络读取 0；合法本地引用仍通过。
- 不使用 monkeypatch 全局网络或关掉 Schema 校验来绕过问题。

## 3. MCP-R2 / P1：目录变化已被发现，旧冻结定义与审批仍可继续派发

位置：[adapter.py 的真实调用路径](../src/yanxu_harness/integrations/mcp/adapter.py#L66)、[catalog.py 的发现路径](../src/yanxu_harness/integrations/mcp/catalog.py#L385)。

`check_binding()` 虽然能比较两个版本，但 `McpToolAdapter.invoke()` 并不调用它，也没有检查 manager 的当前目录/快照代次。`discover_catalog()` 得到新目录后只返回新对象，没有令旧适配器失效。真实调用仍沿着旧 `entry.remote_name` 直接执行。

真实复现：

1. stdio 服务暴露写工具；真实 DefaultHarness + SQLite 运行请求工具，暂停等待可信审批。
2. 服务端工具说明改变；宿主再次执行真实 `discover_catalog()`，确认新旧 `entry.version` **不同**。
3. 批准原预览，以新 attempt 调用原 Run 的 `resume()`。

实际结果：

```text
first_status = suspended
version_changed = true
resumed_status = completed
remote_calls = 1
provider_calls_after_resume = 1
```

期望：观察到漂移后旧绑定在派发前拒绝；远端调用与后续模型请求均为 0，原等待保留。这里没有要求消除“发现与远端执行之间”的不可控竞态；目录变化已经被客户端实际读到，却仍未影响执行权。

现有 `test_drift_refuses_before_any_remote_call` 手动调用纯比较函数，不能证明实际 Harness 恢复接线。若宿主主动丢弃旧适配器并重建全部定义，既有 arrangement 可以发现漂移，但不能以此替代适配器承诺的快照失效机制。

修复要求：

- 将冻结目录、实际连接及宿主绑定建立可核对的关联；目录刷新/失效对旧适配器可见。
- 在真实执行与恢复派发边界比较已提交定义和当前可信事实；不能在 handler 中静默换成新版本，或将拒绝转为普通工具失败后继续问模型。
- 原配置、授权 scope、工具缺失与版本变化都应覆盖；保留原 operation、审批、transcript 与预算。
- 增加上述真实暂停→发现新版本→审批→resume 场景，不仅验证 `check_binding()`。

## 4. MCP-R3 / P1：远端错误回显凭据进入持久账本和模型结果

位置：[results.py 的错误结果归一](../src/yanxu_harness/integrations/mcp/results.py#L86)、[adapter.py 的结果接线](../src/yanxu_harness/integrations/mcp/adapter.py#L90)。

HTTP 凭据由 `McpServerConfig.headers` 提供。远端完整 `isError=true` 回答中的文本直接成为 `detail`、`details.error_text` 和 receipt.result；这些字段进入 SQLite，并由 bridge 形成模型可见错误。现有有界切片及诊断白名单没有对这条持久/模型路径做已知凭据值脱敏。

真实复现：loopback MCP 服务读取请求里的**合成** Authorization header，并在合法 terminal 工具错误中回显它。经真实 Gateway 与 SQLite 后：

```text
status = failed
canary_in_projected_result = true
canary_in_durable_receipt = true
```

这不是诊断函数是否漏字段的问题；凭据已经进入账本与下次请求可用的工具内容。交付文档“已知凭据值在持久/公开面都会脱敏”的结论因此不成立。错误结果虽正确标为 failed，内容边界仍须修复。

修复要求：

- 在已知宿主凭据仍可识别的适配边界，统一清洗远端正文、结构化数据与异常信息，再交给中立结果、持久化、事件和模型投影。
- 处理嵌套 JSON、完整 header 值及凭据值本身；凭据不得写进通用 facts 供后处理扫描。
- 已有错误状态与可用非秘密正文必须保留，不以删除全部错误输出或仅增加诊断黑名单代替修复。
- 增加真实 HTTP header 回显、成功/失败正文、异常和嵌套结果的合成哨兵断言。

## 5. MCP-R4 / P2：明确要求绑定的工具缺失，仍返回 complete 空目录

位置：[catalog.py 的绑定组装](../src/yanxu_harness/integrations/mcp/catalog.py#L300)。

`build_snapshot()` 只遍历已发现描述符，不核对 bindings 中还有哪些工具没有出现。服务端缺少宿主明确要求的工具时，`discover_catalog()` 返回 `complete=True`、`entries=()`，而非装配失败。`build_definitions(only=...)` 能检查显式子集，但默认 `adapter.definitions()` 路径不传 only，不能补上该保护。

真实复现：服务端只列出 write，宿主显式绑定 missing：

```text
complete = true
entries = 0
raised = false
```

这违反计划 §5.2“宿主显式请求绑定的工具被排除时，装配失败”，也会让启动阶段悄悄缺能力，而调用方仍认为已完成装配。

修复要求：发现完成后核对必需 bindings 全集，列出缺失项并明确拒绝；区分“服务目录完整”与“宿主绑定组装完整”。非法工具被 SDK 过滤后的缺失也不能被忽略。合法的额外未绑定工具仍可保留 exclusion，不扩权。

## 6. 验收记录还需要补齐的证据

以下不是额外新增产品范围，而是原计划明确列出的故障窗口。交付报告中的节点不能支持目前写出的完整“通过”：

| 原 AC | 当前引用的证据 | 缺少的验证 |
| --- | --- | --- |
| AC12 | 手动 `check_binding()` | 真实 Run 暂停、刷新目录后 resume 的零派发，见 MCP-R2 |
| AC19 | 既有一般 Gateway 准入和 MCP 审批未决 | MCP intent/提交失败的实际零请求 |
| AC21 | 正常结束后，第二子进程只读 unknown receipt | client 在 intent 后真实退出，新进程接管 open 行 |
| AC22 | 对完整 receipt 调用两次 gateway.review | receipt 已提交、Run 未更新时的真实进程退出与 Harness 恢复 |
| AC25 | ledger promote 幂等 | promote 后、Run 前退出，恢复原 Run |
| AC26 | 普通工具 partial-batch 旧测试 + MCP 单调用 unknown | MCP A/B/C 批次跨进程恢复，证据后 C 一次 |

复用既有 Core 和账本是正确做法；验收仍需证明 MCP 新边界实际接通。修复时把这些场景接入默认专项测试，保留独立脚本作交叉验证。Python 3.10 实跑仍为环境阻塞，不能在同一 AC 上写完整通过后只在括号里排除硬退出条件。

## 7. 修复和复验顺序

1. 将 MCP-R1–R4 的行为断言加入正式测试，保留本探针，在修复前记录四项 RED。
2. 修复 Schema 外部解析、真实漂移守卫、凭据结果清洗和绑定完整性，逐项提交、逐项 GREEN。
3. 补齐 §6 中原计划要求的真实故障窗口，不通过删条件或修改原期望关闭。
4. 复跑现有 77 项专项、原 8 场景和新探针，更新交付矩阵与未执行范围。
5. 再运行完整回归及基础/extra/wheel/sdist 安装 gate，刷新 SNAPSHOT，提交可独立复核结果。

复现命令（退出码 1 表示仍有缺口）：

```bash
.venv-mcp/bin/python scripts/probe_mcp_independent_review.py --report /tmp/mcp-independent-review.json
.venv-mcp/bin/python -m pytest -q tests/mcp
.venv-mcp/bin/python scripts/probe_mcp_acceptance.py --report /tmp/mcp-original-acceptance.json
```

独立探针 4 项均使用真实本地边界，只将模型输出脚本化；所有业务文件和凭据哨兵都是临时构造。源码定位以本报告提交基线为准；修复后行号可能变化，以符号与默认测试为准。

## 8. 修复后独立复验（2026-10-19，88cd5f9）

复验基线为 `88cd5f956568b6c20abe740640bd426852d9aa65`，包含修复提交
`d364632`。原四项探针 **4 passed / 0 failed**，`tests/mcp` **90 passed / 0 skipped**，
原验收探针 **8 场景全部通过**。新增正式测试覆盖了真实 MCP intent 提交失败、
intent 后进程退出、receipt 后 Run 提交前退出、promote 后退出及 A/B/C 部分批次恢复。
这些结果确认原四个最小复现场景已修复。

全量回归当次为 **1384 passed / 11 skipped / 1 failed**（102.41 秒）。唯一失败是
复核期间新增下述探针后、尚未刷新 `SNAPSHOT.json` 时触发的清单检查；随后已用导出脚本
刷新清单，并单独重跑该检查 **1 passed**。其余默认测试未报失败。此记录区分当次全量
运行与后续单项复验，避免将两次输出混写成一次全绿运行。

但继续检查 R2 与 R4 的组合路径，仍发现以下 P1；当前不能给出整体验收通过结论。

### MCP-R5 / P1：刷新发现绑定工具消失后报错，但旧目录仍可派发

位置：`src/yanxu_harness/integrations/mcp/catalog.py::discover_catalog`，
基线行 534–547，尤其 `build_snapshot()` 到 `publish_catalog()` 之间。

`discover_catalog()` 先读远端目录，再构建快照，最后发布。新修复会在绑定工具缺失时
从 `build_snapshot()` 抛出 `McpConfigurationError`，但没有将之前发布的目录标为失效。
因此宿主虽然已经观察到该工具消失，旧适配器的 `catalog_check()` 仍返回成功。
审批前暂停的原 Harness 随后可以继续派发旧工具。这里没有后台刷新或发现/执行竞态：
目录读取和拒绝均已完成，随后才批准并恢复原运行。

真实 stdio + SQLite + 审批复现步骤：

1. 发现并绑定写工具 `write`，原 Harness 请求调用，进入 `suspended` 等待审批。
2. 同一服务将完整 `tools/list` 改为空，但仍接受 `tools/call`（宿主不能依靠远端替它执行目录守卫）。
3. 宿主显式调用 `discover_catalog()`，正确收到缺失 `write` 的配置错误。
4. 批准旧等待项，使用原 Harness、原适配器恢复。

当次结果：

```json
{
  "refresh_refused": true,
  "old_adapter_catalog_check_ok": true,
  "resumed_status": "completed",
  "durable_status": "completed",
  "remote_calls": 1,
  "provider_calls_after_resume": 1,
  "passed": false
}
```

可复跑入口（目前应退出 1；修复后应退出 0）：

```bash
.venv-mcp/bin/python scripts/probe_mcp_failed_refresh.py --report /tmp/mcp-failed-refresh.json
```

原始结果见 `/tmp/mcp-failed-refresh-recheck-20261019.json`；该 JSON 的
`evidence_root` 保留临时 peer、控制文件、远端调用日志及两个 SQLite 数据库。
其他复验结果分别在 `/tmp/mcp-independent-recheck-20261019.json` 和
`/tmp/mcp-acceptance-recheck-20261019.json`。这些 `/tmp` 文件仅是本机证据，
可移植的复现入口是仓库内脚本。

修复要求：对已经观察到的工具消失、绑定 Schema 无法接受等刷新失败，必须让该连接
作用域的旧目录失效；成功完成新快照发布后才能恢复可派发状态。不能只在调用方捕获异常
后中止当前刷新，而留下旧适配器仍可用。补入正式回归，断言原运行保留暂停和交互、
远端调用为 0、后续模型请求为 0，并验证别的授权作用域不受影响。

本次未重跑 wheel/sdist 安装 gate：已存在可复现的运行时阻断项，安装门禁不能关闭它。
Python 3.10 实跑仍是交付报告声明的环境阻塞，本次没有将其改记为通过。

## 9. MCP-R5 修复与复验（2026-10-19）

修复位于 `catalog.py::discover_catalog`：刷新是「观察 + 冻结 + 发布」的一次事务。
目录读取失败、绑定工具缺失、绑定 Schema 不可接受或其他装配失败时，该调用在**抛出前**
把当前连接作用域已发布的目录标为失效；适配器的 `catalog_check()` 因此返回
`invalidated`，直接 `invoke` 与 Harness 的 resume 派发守卫都会在任何字节发出前拒绝。
失效原因只记录错误类型，不记录可能携带远端 schema 文本的异常正文，也不新增持久面。
失效按连接作用域（服务、授权 scope、配置 revision、协议、传输）隔离，别的授权 scope
不受影响；只有一次成功发布的新快照才恢复可派发状态。调用方即使忽略
`discover_catalog()` 的错误，旧适配器也已经不可派发。

正式回归（默认 `tests/mcp`）：

* `test_a_refused_refresh_invalidates_only_that_scope`：夹具在 `tools/list` 隐藏
  `echo` 但仍接受 `tools/call`；刷新拒绝后旧适配器的 `catalog_check()` 为
  `invalidated`、直接调用抛 `McpDriftRefusal`，另一个授权 scope 仍可派发；目录恢复并
  成功发布后旧适配器重新可派发。服务端实际收到且仅收到两次合法调用。
* `test_a_refused_refresh_keeps_the_paused_run_and_its_interaction`：真实 stdio +
  SQLite + 审批，原 Run 暂停后工具从目录消失，刷新拒绝；批准原预览并 resume 抛
  `ResumeRefused(reason_code="binding_drift")`，远端调用 0、后续模型请求 0、原等待与
  `pending_interaction` 原样保留；目录恢复后同一审批可完成，恰好 1 次远端调用。

复跑结果（Python 3.12.13 / macOS arm64，`.venv-mcp`，工作树 = `88cd5f9` + 本次修复）：

| 检查 | 结果 |
| --- | --- |
| `scripts/probe_mcp_failed_refresh.py` | **exit 0**；`old_adapter_catalog_check_ok=false`、`resumed_status=refused`、`durable_status=suspended`、`remote_calls=0`、`provider_calls_after_resume=0`；修复前同一脚本 exit 1 |
| `tests/mcp` | **92 passed / 0 skipped** |
| 全量 `pytest -q` | **1387 passed / 11 skipped**（102.89s，清单刷新后） |
| R1–R4 探针 | **4 passed / 0 failed** |
| 原验收探针 | **8 场景全部通过** |
| `scripts/mcp_gate.py`（离线 wheelhouse） | `base` / `extra_wheel` / `extra_sdist` **ok**；门内 `tests/mcp` **92 passed**、复制出的全套 **1362 passed / 36 skipped**、两个示例 exit 0；Python 3.10 仍为环境阻塞，不计通过 |

剩余未执行项不变：Python 3.10 实跑、远程 CI、其他平台与真实 Provider。`/tmp` 里的
历史证据仍是本机文件；可移植复现入口是仓库内两个探针与默认测试。

## 10. MCP-R5 独立复核结论（2026-10-20）

复核对象为 `codex/mcp-client` 工作树：HEAD 仍为 `88cd5f9`，R5 修复尚未提交。
此次独立重跑结果支持关闭 R5；本轮未发现新的阻断缺陷。范围为本机 Python 3.12.13
下的实现和回归，Python 3.10 实跑等剩余验证仍按交付报告记录。

| 独立检查 | 当次结果 |
| --- | --- |
| R5 原复现脚本 | **exit 0**；旧适配器检查拒绝、resume 抛 `ResumeRefused`、持久化 Run 保持 `suspended`，远端调用和后续模型请求均为 **0** |
| R1–R4 原探针 | **4 passed / 0 failed** |
| 原验收探针 | **8 场景全部通过** |
| 默认 MCP 专项 | **92 passed / 0 skipped**（37.74s） |
| 全量 `pytest -q tests` | **1387 passed / 11 skipped / 0 failed**（109.01s） |
| 补充本地真实服务检查 | 绑定 Schema 编译失败（`McpSchemaError`）、目录读取失败（`McpProtocolError`）均使旧适配器失效并拒绝直接调用，远端调用 **0** |

已审读新增两条正式 R5 回归：它们覆盖授权 scope 隔离、暂停与原交互保留，以及成功
重新发现后复用原审批完成调用。未削弱原 R5 探针的行为断言。

此轮未重新构建或执行安装 gate。已读取交付者的 `build/mcp-gate-r5c/report.json`
（基础、wheel、sdist 三门通过；Python 3.10 仍阻塞），并独立逐文件比对现有产物：
wheel 的 **107 个源码文件**、sdist 的 **128 个源码及 MCP 测试文件**与当前工作树相同。
这项比对确认报告中的现有产物包含当前代码，不将交付者的门禁输出记为本轮独立重跑。

本机复核 JSON：`/tmp/mcp-r5-recheck-20261020.json`、
`/tmp/mcp-original-four-recheck-20261020.json`、`/tmp/mcp-acceptance-recheck-20261020.json`、
`/tmp/mcp-schema-refresh-recheck-20261020.json`、`/tmp/mcp-artifact-comparison-20261020.json`。
关键运行时代码 `catalog.py` 的 SHA-256 为
`c6da242c2811b98e3e10ea5221ac651bd8dc12e5658f8d201baa920239f396bc`。
