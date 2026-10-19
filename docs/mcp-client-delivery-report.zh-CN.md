# MCP Client 交付报告

日期：2026-10-19（Asia/Shanghai）。分支：`codex/mcp-client`（起点 `6ff45ba`）。
版本：**保持 `0.2.0a1`**（未升级、未构建新发行物、未上传、未推 tag）。
状态：实施完成，独立复核的四项阻断（MCP-R1–R4）已修复，停在可再次独立复核状态。

本报告按 [实施计划](mcp-client-implementation-plan.zh-CN.md) 的退出口径写：先给实际交付与
真实证据（命令、退出码、测试节点），再给未支持范围与未执行项。失败历史留在
[进度记录](mcp-client-progress.zh-CN.md)，SDK/协议锁定结论留在 [MCP00 ADR](mcp-client-sdk-baseline.zh-CN.md)。

## 1. 交付内容与提交

| 阶段 | 提交 | 主要落点 |
| --- | --- | --- |
| MCP00 | `c64eb9d` | 依赖锁定（`constraints/`）、真实 MCP 服务夹具、两代两传输合同测试、one-shot 与高层重发证据 |
| MCP01 | `448581e` | 中立 `ToolExecutionResult`、归一函数、`RunContext.for_operation`、两个账本后端复用同一归一 |
| MCP02 | `e180360` | `integrations/mcp/`（config/sdk/transports/client/types/errors）、有界 stdio、连接隔离与并发/期限/关闭 |
| MCP03 | `3626c1f`、`6201cce` | Streamable HTTP（有界响应体、凭据边界）、HTTP 故障矩阵、四格齐全 |
| MCP04 | `845e243` | `schema.py`、`catalog.py`、`tools.py`：真校验、别名、冻结版本、漂移判定 |
| MCP05 | `affb669` | `results.py`、`adapter.py`：Gateway 真执行、审批、结果语义、期限 |
| MCP06 | `e7ab531` | 真实故障窗口、unknown 屏障、证据提升、`diagnostics.py` |
| MCP07 | `5a8a968` | 两个离线示例、独立探针、示例回归 |
| MCP08 | 本提交 | 使用文档、门禁脚本、README/CHANGELOG、本报告 |

与计划建议名称的差异（都按计划允许的方式记录）：

* 计划建议 `client.py`、`transports.py`、`catalog.py`、`tools.py`、`results.py`、`sdk.py`
  全部采用；额外新增 `errors.py`、`types.py`、`http.py`、`adapter.py`、`schema.py`、
  `diagnostics.py`（拆分理由见各文件首段）。
* 计划建议 `constraints/mcp-py310.txt`：实际用 `constraints/mcp.txt` + 两个带 sha256 的
  lock 文件（`mcp-lock-macos-arm64-py310.txt` / `-py312.txt`），同一份 pin 服务两代 Python。
* 计划建议的 `ClientSession` 低层入口、显式 era（不用 `mode="auto"`）按计划执行。

### 1.1 独立复核修复（MCP-R1–R4）

[独立验收报告](mcp-client-independent-review.zh-CN.md) 在 `48b8fd1` 上复现了四项阻断；本分支已逐项修复并把行为断言接入默认专项测试：

| 缺口 | 修复 | 默认测试 |
| --- | --- | --- |
| R1 `$dynamicRef` 可触发远端 schema 读取 | 扫描 `$ref`/`$dynamicRef`/`$recursiveRef`（输入与输出 schema 都查），并给实际 validator 绑定禁止 retrieval 的 `referencing.Registry` 第二道防线 | `test_dynamic_and_recursive_refs_are_refused_without_a_fetch`、`test_an_output_schema_with_a_remote_reference_is_refused` |
| R2 目录已刷新但旧适配器/旧审批仍可派发 | 连接管理器记录每个 scope 当前可信目录；`discover_catalog` 发布、重连/显式失效能让旧快照失效；适配器在每次 invoke 前复核冻结绑定；`ToolSpec` 新增 `dispatch_guard`，Harness 在 resume 派发前拒绝并保留原等待 | `test_observed_catalog_drift_refuses_resume_with_zero_dispatch`（真实暂停→真实重新发现→审批→resume，零远端/零模型） |
| R3 远端错误回显的凭据进入账本与模型结果 | `SecretRedactor` 在适配边界清洗远端文本、嵌套结构化值、异常信息和最终 neutral result；值来自宿主 headers/env，含 scheme 拆分与 base64/URL/JSON 转义形式 | `test_remote_credential_echo_is_redacted_everywhere`（真实 HTTP header 回显，成功/失败/nested 哨兵） |
| R4 绑定工具缺失仍返回 complete 空目录 | `build_snapshot` 在发现完成后核对 bindings 全集，缺失即明确拒绝，并区分“目录完整”与“绑定组装完整” | `test_a_bound_tool_missing_from_a_complete_directory_is_refused`、`test_an_intent_submission_failure_sends_no_tool_request` |

同时把 §6 指出的故障窗口接入 MCP 默认专项测试：`test_fault_windows.py` 用真实子进程在精确 commit/settle 边界退出，覆盖 intent 后退出、receipt 落账、promote 后/Run 前退出、A 已结算/B 未知/C 未发的跨进程恢复。

## 2. 锁定表、四格矩阵与 one-shot 证据

| 项 | 值 | 证据 |
| --- | --- | --- |
| SDK | `mcp==2.3.0`（extra 精确 pin）+ `jsonschema>=4.20.0` | `pyproject.toml`；`constraints/mcp.txt`；lock 文件 28 个 wheel 的 sha256 |
| 协议 | `2025-11-25`（握手代）、`2026-07-28`（现代代）；其余拒绝 | `SUPPORTED_PROTOCOLS`；`tests/mcp/test_contract.py`；探针 `unknown_protocol_refused` |
| 传输 | stdio、Streamable HTTP | 四格见下 |
| Python | 声明 `>=3.10` 未变；实跑 3.12.13（3.10 解释器本机不存在） | `tests/test_python_floor.py` + 探针 `python_floor` 阻塞记录 |
| one-shot | 一次准入最多一次 `tools/call` | 出站 tap + 服务端 facts 双层计数（stdio/HTTP 各协议）；探针 `grid:*` 每格 `server_side_calls=1` |

四格（全部真实服务，非替身）：

| 传输 \ 协议 | 2025-11-25 | 2026-07-28 |
| --- | --- | --- |
| stdio | `test_stdio_connects_lists_and_calls_under_the_pinned_revision` | `test_stdio_modern_revision_is_negotiated_not_assumed` |
| Streamable HTTP | `test_http_legacy_round_trip_sends_one_post_per_tool_call` | `test_http_modern_round_trip_sends_one_post_per_tool_call` |

高层入口为什么被拒：`mcp==2.3.0` 的 `Client.call_tool` 在 `HEADER_MISMATCH`（-32020）后
会重取目录并**重发同一个业务调用**，真实观测序列为 `tools/call → tools/list → tools/call`
（`tests/mcp/test_contract.py::test_high_level_client_resends_the_same_call_after_header_mismatch`）；
适配器改用公开低层 `ClientSession`，同一场景在 HTTP 上仍是 1 次调用、0 次补取目录
（`tests/mcp/test_http.py::test_a_header_mismatch_does_not_trigger_a_resend`）。

## 3. MCP-AC01–32

状态口径：**通过** = 有真实 test node/命令与当次结果；**部分** = 只覆盖了其中一部分；
**阻塞** = 环境不具备，已记录而不是跳过。

| AC | 状态 | 证据（test node / 命令） |
| --- | --- | --- |
| 01 基础安装无 MCP 依赖，遍历导入不连接/不起进程/不加载 SDK | 通过 | `tests/test_import_fence.py`（`mcp`/`mcp_types`/`anyio`/`jsonschema` 均在禁止清单，仅懒加载豁免）；门禁 `base` 的 cold-import `"leaked": []` |
| 02 extra 依赖可重现，Python 下限未抬高 | 通过（3.10 实跑阻塞） | `constraints/mcp-lock-*.txt`（`pip install --require-hashes` 在全新 venv 通过）；门禁 `extra`/`sdist` 离线 `--no-index` 安装；3.10 见 §7 |
| 03 两协议两传输各有成功路径，未知版本拒绝 | 通过 | §2 四格；`test_contract.py`；探针四格 + `unknown_protocol_refused` |
| 04 宿主给 executable/argv/cwd/env；stdio 分离；退出有界 | 通过 | `test_stdio_reports_bounded_stderr`（stderr 尾巴 ≤1024）、`test_closing_the_manager_leaves_no_child_process`（`os.kill(pid,0)`）、`test_examples_leave_no_server_process_behind` |
| 05 HTTP 凭据有效且不外泄；越界重定向/降级拒绝 | 通过 | `test_headers_carry_the_credential_and_nothing_leaks_it`（哨兵扫描 4 处）、`test_a_rejected_credential_never_appears_in_the_error`、`test_a_cross_origin_redirect_is_refused_and_not_followed`（目标 origin 0 请求） |
| 06 分页完整；循环 cursor/重复名/超限不死循环不伪装完整 | 通过 | `test_pagination_is_bounded_and_complete`、`test_a_repeated_cursor_is_refused_instead_of_looping`；`McpToolDirectory.complete/pages_read` |
| 07 原名大小写/截断/跨服务同名映射稳定无覆盖 | 通过 | `test_local_aliases_are_stable_lowercase_and_distinct`（大小写、超长名、≤64、正则）；`build_snapshot` 的 alias 冲突拒绝 |
| 08 JSON Schema 真验证且不改参数；非法参数零调用 | 通过 | `tests/mcp/test_schema.py` 全部正负例；`test_invalid_arguments_never_reach_the_server`（服务端 facts 为空） |
| 09 远程 ref / 未知 dialect / 过深超大 schema 明确拒绝 | 通过（x-mcp-header 见 §7） | `test_a_remote_ref_is_refused_without_being_resolved`、`test_dynamic_and_recursive_refs_are_refused_without_a_fetch`（loopback 计数 0）、`test_an_output_schema_with_a_remote_reference_is_refused`、`test_an_unknown_dialect_is_refused`、`test_over_complex_schemas_are_refused`、`test_an_unresolvable_local_ref_is_refused`、`test_a_malformed_schema_is_refused`；validator 绑定禁止 retrieval 的 registry |
| 10 目录不等于授权；远端 hints/instructions 不升级权限 | 通过 | `test_the_catalog_contains_only_host_bound_tools`（未绑定项逐条进 exclusions）；`McpToolBinding` 强制效果分类，写工具强制 preview |
| 11 未批准写工具零远端调用，批准后一次 | 通过 | `test_a_write_tool_waits_for_approval_then_runs_exactly_once`（审批前 facts 空，批准后 1；业务计数器 1） |
| 12 服务/定义/schema/效果/授权/协议漂移在恢复派发前拒绝 | 通过 | `test_drift_refuses_before_any_remote_call`（missing/revoked/changed/policy/protocol/scope 六种 code，facts 为空）；`test_observed_catalog_drift_refuses_resume_with_zero_dispatch`（真实 DefaultHarness 暂停→真实 `discover_catalog()` 看到新版本→批准原预览→resume 抛 `ResumeRefused`，远端 0、模型请求 0、原等待保留）；适配器直接 invoke 同样拒绝 |
| 13 一次准入最多一次 `tools/call`（含 HeaderMismatch/4xx/5xx/断流） | 通过 | `test_high_level_client_resends_...`、`test_a_header_mismatch_does_not_trigger_a_resend`、`test_http_error_statuses_are_reported_without_a_second_request`（401/403/404/500 各 1 次 POST）、`test_a_truncated_answer_is_not_resent` |
| 14 Text/structured-only/数组/scalar/null/空成功可持久并进入下一模型请求 | 通过 | `test_structured_payloads_of_any_shape_render_deterministically`（数组/scalar/null/文本顺序）、`test_an_empty_success_is_still_a_real_success`、`test_the_result_enters_the_next_model_request`（真 `DefaultHarness` 两轮 provider） |
| 15 完整远端错误是失败结果，不暗示回滚 | 通过 | `test_a_terminal_remote_error_is_failed_and_says_so` + SQLite 行 `failed`；detail 含远端正文 |
| 16 错误/非 terminal/不支持媒体不静默变空成功，不触发 sampling/URI 读取 | 通过 | `test_an_unsupported_media_answer_is_explicit_never_an_empty_success`；codec 的 `interaction_required` 分支（`requests_more`） |
| 17 消息/目录/stderr/并发/槽位/总期限有界 | 通过 | `test_an_oversize_frame_fails_the_connection_instead_of_being_truncated`（边界在解码前按字节生效）、`test_concurrency_slots_bound_real_overlap`（服务端 peak=1）、`test_a_call_deadline_includes_waiting_for_a_slot`（排队调用未派发）、HTTP 响应体上限（`http.py` + `Content-Length` 预检） |
| 18 operation/run/action/call 身份贯穿，跨 turn 同 call id 不碰撞 | 通过 | `tests/test_tools_execution_results.py::test_the_handler_sees_the_gateway_derived_operation_id_and_nothing_else_changes`；`tests/test_tools_gateway.py` 的 binding/`model_action_id` 用例；`test_the_receipt_keeps_the_frozen_definition_identity` |
| 19 原 intent 提交失败不发送工具请求 | 通过 | `test_an_intent_submission_failure_sends_no_tool_request`（真实 MCP 部署，注入 `admit` 失败，服务端 `tools/call` facts 为空）；通用 Gateway 准入用例仍覆盖 binding conflict |
| 20 业务已写、回包丢失 → UNKNOWN，不自动重发 | 通过 | `test_the_unknown_window_is_real_and_stays_a_barrier`；探针 `unknown_window`（business_file=1、ledger=unknown、server_side_calls=1） |
| 21 client 在 intent 后退出，第二进程不把 open 当 absent | 通过 | `test_a_process_that_dies_after_intent_leaves_an_open_row`：真实 MCP gateway 在 `admit` 后 `os._exit(9)`，服务端零 `tools/call`；新进程只读 `unsettled()` 看到 `running` 的 `project-a:s1:r1:call-1` |
| 22 receipt 已落、Run 未更新时恢复复用回执且不调用 handler | 通过 | `test_ac22_a_settled_mcp_receipt_survives_a_run_commit_crash`：真实 MCP `echo` 成功后，在 harness 写 tool-result 的 commit 内 `os._exit(9)`；新进程恢复 completed，服务端 `tools/call` 仍为 1，账本仍 1 条 succeeded |
| 23 取消/期限传播，零后续派发；已发生效果保留 | 通过 | `test_cancellation_before_dispatch_is_a_control_signal_with_zero_calls`；`test_a_timeout_after_dispatch_is_unknown_and_is_not_replayed`（服务端已收到，状态 unknown） |
| 24 late response/旧 attempt/lease 失效不覆盖新事实 | 通过 | 复用既有 harness 故障窗口与租约用例：`tests/test_runtime_recovery.py`、`tests/test_tasks_runner_recovery.py`、`tests/fault_windows.py`；MCP 未新增第二份 lease |
| 25 宿主证据收敛 unknown；提升后/Run 前退出仍可恢复 | 通过 | `test_ac25_a_promoted_mcp_receipt_is_reused_after_a_process_dies`：真实 MCP 未知窗口→新进程 `gateway.promote` 后 `os._exit(9)`（Run 仍 suspended）→再一进程恢复 completed；`tools/call` 仍 1、业务计数 1；通用 promotion 幂等用例保留 |
| 26 A 已结算、B 未知、C 未发；恢复先阻塞、证据后 C 一次 | 通过 | `test_ac26_an_mcp_partial_batch_barriers_then_runs_c_once_after_evidence`：真实 MCP 顺序 A/B/C，B 的 `settle(unknown)` 后子进程退出；C 无 intent、无 `tools/call`；新进程 resume 以 reconciliation 阻塞；promote B 后 C 恰好一次，A/B 不重发 |
| 27 并发调用取消一项不破坏另一项；scope 不串 | 通过 | `test_closing_one_connection_does_not_disturb_another`、`test_two_authorization_scopes_never_share_a_process`（两个 pid、两个 connection_id） |
| 28 host-owned ledger 仍唯一；普通工具/审批/上下文不退化 | 通过 | `tests/test_tools_execution_results.py::test_a_host_owned_backend_records_exactly_one_intent_and_one_receipt`；`tests/test_tools_execution.py`（普通返回值语义不变）；全量套件回归 |
| 29 child 只用委派子集，未知结果不刷新父子额度 | 通过 | `tests/mcp/test_tasks.py::test_the_child_gets_only_the_delegated_subset_and_unknown_changes_nothing`：真 `ChildCoordinator`（scope 外工具 `NOT_PERMITTED`、超 cap grant `BUDGET_REFUSED`，拒绝不写任何记录）、真 `TaskBudget`/`BudgetLedger`（grant=2.0，cap=10.0）、真 `DispatchBudget` + 真 `DefaultHarness`；子集外别名在 registry 里不存在；unknown 窗口子运行 SUSPENDED、子账本 `unknown`、远端业务已写；父子 deadline 不变、只多 1 条 dispatch reservation、同 dispatch identity 重复预留幂等；等待审批的子运行不留 intent/receipt |
| 30 诊断只读、零网络；秘密/私帧/原始配置不公开 | 通过 | `tests/mcp/test_diagnostics.py` 四条：白名单有界、哨兵扫描、导入隔离（无 SDK）、无连接入口 |
| 31 两个离线示例与独立探针可运行（真实服务） | 通过 | `tests/mcp/test_examples.py`；`examples/11_mcp_stdio.py`、`examples/12_mcp_http_recovery.py` 退出码 0；探针 8 场景全 ok |
| 32 基础/extra 双环境、wheel/sdist 树外安装、清单 | 通过 | 当次专项 `tests/mcp` **90 passed / 0 skipped**；全套 **1385 passed / 0 failed**（本机 checkout）；`tests/test_build_scripts.py`（SNAPSHOT 清单已刷新）。安装门禁数字以 §4 当次复跑为准 |

## 4. 安装门禁（`scripts/mcp_gate.py`）

```bash
.venv/bin/python scripts/mcp_gate.py --workdir /tmp/mcp-gate --report /tmp/mcp-gate-report.json
```

<!-- GATE-RESULTS -->

当次输出（复跑于 MCP-R1–R4 修复后，Python 3.12.13 / macOS arm64，`/tmp/mcp-gate-final-report.json`）：

| 门 | 结果 | 关键数字 |
| --- | --- | --- |
| `base`（无 SDK 的 tree-external 安装） | **ok** | 安装 exit 0；冷启动遍历 106 个子模块 `"leaked": []`；`sdk_available()=false` 且 `load_sdk()` 报 `McpExtraMissing`（带 `pip install 'yanxu-harness[mcp]'`）；基础示例 exit 0 |
| `extra_wheel`（`yanxu_harness-0.2.0a1-py3-none-any.whl[mcp]`） | **ok** | 安装 exit 0 且来自 site-packages；`tests/mcp` **90 passed / 0 skipped**（含 MCP-R1–R4 回归与 AC21/22/25/26 跨进程窗口）；复制出的全套 **1360 passed / 0 failed / 36 skipped**；探针 exit 0；两个示例 exit 0 |
| `extra_sdist`（`yanxu_harness-0.2.0a1.tar.gz[mcp]`） | **ok** | 同上，均来自 sdist 安装的 site-packages |
| `python_floor` | **阻塞**（不计入门禁结论） | 本机没有 Python 3.10 解释器；已提供 3.10 解析闭包与静态下界门，不写成通过 |

门禁同时验证了两件容易做假的事：

* MCP 环境的 `tests/mcp` 必须是 **0 skipped** —— 缺 extra 造成的 skip 计为门禁失败，
  不能拿「全绿」蒙过去；
* 安装来源是 site-packages（`installed_from` 字段），不是源码树里的 editable 安装。

门禁过程中捕到的真 bug（留在修复历史里）：基座冷进程里 `sdk_available()` 用了
`importlib.util.find_spec` 而模块只 `import importlib`——在检出目录中因为 pytest 先导入过
`importlib.util` 而侥幸通过。这正是「基础环境 + 冷启动」这条门存在的意义。

## 5. 兼容性变化

| 变化 | 影响 | 证据 |
| --- | --- | --- |
| 新增 `ToolExecutionResult` / `receipt_from_handler_result` / `storable_value` | 普通 handler 返回值语义不变（仍是成功）；新类型是可选的显式表达 | `tests/test_tools_execution_results.py` 14 条；全量 1304 passed |
| `SqliteExecutionBackend._jsonable` 改为委托 `storable_value` | 同一份「可存储」定义，行为等价（JSON 原生类型新增了递归分支） | 全量套件 + `test_the_sqlite_backend_stores_and_reads_back_a_neutral_result` |
| `RunContext.for_operation` + Gateway 在 execute 前盖章 | 既有自定义 context 原样透传；host-owned 后端不双记 | `test_a_host_owned_context_shape_is_passed_through_untouched` |
| 新增可选 extra `mcp` 与 `integrations/mcp/` 包 | 基础安装的依赖与导入路径不变（懒加载有测试牙齿） | `tests/test_import_fence.py`、`tests/test_python_floor.py` |
| 新增测试族 `tests/mcp/` | 基座环境显式 skip 一条（原因写明）；MCP 门禁把 skip 计为失败 | `tests/mcp/conftest.py`、`scripts/mcp_gate.py` 的 `skipped == 0` 断言 |
| `build_snapshot` 对“宿主绑定但服务未提供”的工具改为明确拒绝 | 以前会返回 `complete=True` 的空目录；现在启动即失败并列出缺失项。合法的额外未绑定工具仍只进 exclusions，不扩权 | `test_a_bound_tool_missing_from_a_complete_directory_is_refused` |
| `ToolDefinition`/`ToolSpec` 新增可选 `dispatch_guard` | 默认 `None`，普通工具行为与此前完全一致；只有直播绑定的适配器（MCP）会填 | `tests/test_tools_policy.py`、`tests/mcp/test_independent_review_regressions.py` |
| 远端 `isError` 正文在进入账本/模型前被已知凭据清洗 | 错误仍为 `failed`、可用非秘密正文仍保留；清洗后文本含 `[REDACTED]` | `test_remote_credential_echo_is_redacted_everywhere` |

## 6. 未支持范围与已知边界

* 未实现：Resources/Prompts 主动读取、自动 OAuth/令牌刷新、自动安装服务端、
  sampling/elicitation/MCP Tasks、远端媒体（图像/音频/blob）处理、自动工具重试。
* **2026-07-28 的 `x-mcp-header` 过滤**：SDK 在 `tools/list` 后丢弃声明非法的工具，
  适配器看不到它们，因此只能「不出现」，无法逐项给出排除原因（ADR §3 已记录）。
* **远端 CAS/幂等**：并发保护取决于远端是否真的提供 CAS；适配器不虚构目标版本，
  也不让远端工具自称幂等（`Idempotency.NONE`，replay 永远 not）。
* **平台**：stdio 进程组清理在 macOS 实测；Windows（Job Object）路径未验证。
* **目录刷新与执行窗口**：适配器保证「已观察到的目录变化一定阻断后续派发」，但不声称消除
  “发现与远端执行之间”的不可控竞态；适配器不自行后台轮询目录，刷新由宿主显式调用
  `discover_catalog()` 驱动。
* **凭据清洗范围**：清洗的是宿主自己知道的值（`McpServerConfig.headers`、凭据形状的 env/argv 键，
  及其 scheme 拆分/base64/URL/JSON 转义形式）。远端凭空构造一个宿主从未使用过的秘密无法被识别；
  这是本适配层能做到的上限。
* **Python 3.10**：本机没有 3.10 解释器（只有 3.11.15/3.12.13），AC02 的 3.10 实跑记为
  **环境阻塞**；已提供 3.10 的解析闭包（含 sha256），静态下界门 `tests/test_python_floor.py` 通过。
* 未执行：真实付费 Provider、真实凭据、宿主（Yanxu）固定 wheel 切换、发行物上传与 tag。

## 7. 独立复核入口

```bash
# 1. 聚焦：合同 + 四格 + 目录/Schema + Gateway + 恢复 + 诊断 + 示例
.venv-mcp/bin/python -m pytest -q tests/mcp

# 2. 独立探针（真实服务、四格、未知窗口、导入隔离）
.venv-mcp/bin/python scripts/probe_mcp_acceptance.py --report /tmp/mcp-probe.json

# 2b. 独立复核探针（MCP-R1–R4；exit 1 表示仍有缺口）
.venv-mcp/bin/python scripts/probe_mcp_independent_review.py --report /tmp/mcp-independent-review.json

# 3. 两个离线示例
.venv-mcp/bin/python examples/11_mcp_stdio.py
.venv-mcp/bin/python examples/12_mcp_http_recovery.py

# 4. 基础回归（不需要 extra）
.venv/bin/python -m pytest -q tests
.venv/bin/python -m pytest -q tests/test_import_fence.py tests/test_python_floor.py

# 5. 安装门禁（基础 / extra / sdist，树外安装）
.venv/bin/python scripts/mcp_gate.py --workdir /tmp/mcp-gate --report /tmp/mcp-gate-report.json
```

复核重点建议顺序：先跑 1/2/2b/5，再审读 one-shot（`test_contract.py` 与 `test_http.py` 的
计数断言）、审批与 unknown 屏障（`test_gateway.py`、`test_recovery.py`）、
MCP 新边界的跨进程窗口（`test_fault_windows.py`）、
以及 import fence 的懒加载豁免范围（只给 `sdk.py`/`http.py`/`schema.py`/`client.py`/`transports.py`）。
