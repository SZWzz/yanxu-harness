# Harness 扩展 E0–E3 交付报告

日期：2026-10-20。状态：**实现在独立复核后修复了 EXT-R1–R6，待独立复验**。
独立复核（同日）发现 4 项 P1、2 项 P2；修复记录见 §8，原始复现见
[独立复核与修复交接](extensions-independent-review.zh-CN.md)。下文 §3 的原始证据保留，
不作为最终验收通过结论。
包版本：**`0.3.0a1`**（未升 `0.4.0a1`，未上传包索引，未推 tag，未合 main，未切换宿主 wheel）。
源码分支：`codex/extensions-implementation`（从 `main@077d91b` 新建，方案文档以 `f66d112` 带入）。

## 1. 交付摘要

| 阶段 | 提交 | 交付 |
| --- | --- | --- |
| E0 | `bf5492c` | 严格清单解析、能力/代次/ID、限界、不可变冻结、纯预检、canonical 计划摘要、只读诊断、ADR |
| E1 | `825f924` | 私有暂存 + 一次发布、只读 registry、组合 SkillProvider、惰性 MCP 工厂、失败全体回滚 |
| E2 | `62a4561` | owner 生命周期、逆序有界幂等关闭、撤权守卫、`ToolGateway` 通用 guard、部署摘要进入实际 `config` 比较、全部恢复分支、SQLite/跨进程/A‑B‑C 证据 |
| E3 | 本报告提交 | 两个参考包、examples 13/14、合同辅助、独立探针、base/extra/wheel/sdist 门禁、文档与逐 AC 证据 |

新增公开入口（`yanxu_harness.extensions`）：`parse_manifest`、`plan_extensions`、
`describe_plan`、`open_extensions`、`mcp_tool_factory`、`ExtensionManifest`、
`ContributionDeclarations`、`ExtensionBinding`、`ExtensionContext`、
`ExtensionContribution`、`SkillContribution`、`ExtensionFactory`、`ExtensionLimits`、
`ExtensionState`、`CloseReport`、`AssemblyPlan`、`ExtensionDeployment`
（`AssemblySnapshot` 别名）、`FrozenToolRegistry`、`CompositeSkillProvider` 及扩展错误。
`yanxu_harness.testing` 新增 `check_extension_deployment_contract`；
`yanxu_harness.errors` 新增 `DeploymentRefused`。

## 2. 环境与基线

- 源码：`codex/extensions-implementation`，基线 `main@077d91b` + 方案文档 `f66d112`。
- 版本：`0.3.0a1`（`pyproject.toml` 与 `__version__` 一致）。
- 解释器：`.venv/bin/python` / `.venv-mcp/bin/python` = Python **3.12.13**，macOS（darwin）。
- 运行时导入来源：编辑安装指向 `src/yanxu_harness/`；门禁环境断言从
  `site-packages/yanxu_harness/` 导入。
- Python 3.10：本机无真实解释器（`python3.10` 不存在，pyenv 仅 3.12.13）。
- CodeGraph：247 文件 / 7,851 节点 / 30,062 边；`codegraph sync` 最新；已核对
  `ToolGateway.invoke` 与 `DefaultHarness.fingerprints` 的影响面。
- 未使用真实凭据、付费模型、真实业务资料；示例和测试的模型输出均由脚本化
  `stream_fn` 提供，Harness、Gateway、SQLite、审批、进程、MCP 连接和远端效果是真实的。

## 3. 逐 EXT-AC01–40 证据

命令缩写：

- `EXT` = `.venv/bin/python -m pytest -q tests/extensions`
- `EXTRA` = `.venv-mcp/bin/python -m pytest -q tests/extensions tests/mcp`
- `PROBE` = `.venv/bin/python scripts/probe_extensions_acceptance.py --report build/extensions-acceptance.json`
- `GATE` = `.venv/bin/python scripts/extensions_gate.py --workdir build/extensions-gate --report build/extensions-gate/report.json`

| AC | 阶段 | 实际节点/命令 | 结果与证据 |
| --- | --- | --- | --- |
| 01 | E0 | `tests/extensions/test_e0_manifest.py`（`test_duplicate_json_keys_are_refused_at_any_depth`、`test_non_finite_json_constants_are_refused`、`test_malformed_fields_are_refused`、`test_unknown_api_generations_are_refused`、`test_the_document_size_depth_and_node_limits_are_enforced`）；`EXT` | PASS。重复 key/NaN/Infinity/未知字段/类型/ID/API/深度/节点/字节均拒绝；`parser` 先于 schema 校验执行边界检查。 |
| 02 | E0 | `test_unknown_required_capabilities_are_refused`、`test_the_same_extension_id_is_refused_even_with_the_same_version`、`test_a_factory_outside_the_manifests_is_not_even_asked_for_a_method`；`EXT` | PASS。未知能力/代次拒绝；同 ID 不同或相同版本都不自动选优；无关工厂的 `open` 属性不被读取，不触发 import/安装/网络。 |
| 03 | E0 | `test_plan_identity_ignores_mapping_and_set_order`、`test_plan_identity_preserves_declaration_order_and_version`、`test_plan_identity_changes_when_binding_revisions_change`；`EXT` | PASS。对象/集合顺序不影响摘要；声明数组顺序、版本、binding revision 改变摘要。 |
| 04 | E0/E1 | `test_mapping_input_is_copied_not_retained`、`test_a_callable_schema_is_evaluated_once_and_frozen`、`test_the_published_registry_is_frozen_and_the_deployment_is_one_snapshot`；`EXT` | PASS。清单输入、工厂返回 schema 的后续修改、callable schema 的后续返回值都不改变冻结定义；registry/schema 不可变。 |
| 05 | E0 | `test_a_missing_factory_or_binding_is_refused`、`test_a_bound_contribution_must_be_declared_by_the_manifest`、`test_a_manifest_requirement_must_be_granted`、`test_a_binding_cannot_grant_authority_the_manifest_did_not_require`；`EXT` | PASS。缺 binding/factory、需求未满足、binding 越权授予、清单自称授权（未知字段）都拒绝。 |
| 06 | E0 | `test_planning_calls_no_factory_and_keeps_the_host_registry_unchanged`、`test_parsing_planning_and_describing_perform_no_process_or_network_io`；`EXT` | PASS。解析/预检/describe 零工厂打开、零 socket/Popen；宿主 `ToolRegistry` 未变。 |
| 07 | E0/E1 | `test_aggregate_limits_cannot_be_bypassed_by_per_item_slices`、`test_the_report_limit_is_enforced_instead_of_truncating`、`test_metadata_budget_is_checked_at_publication`；`EXT` | PASS。扩展/工具/skill/报告/metadata/深度/节点总量上限生效；超限拒绝而非截断。 |
| 08 | E0 | `test_a_cold_import_of_the_extensions_package_pulls_no_optional_dependency`；`EXT` + `tests/test_import_fence.py` | PASS。冷进程导入全部 `extensions` 子模块，无 `mcp`/`mcp_types`/`anyio`/`httpx2`/`jsonschema`/`pydantic`/`yaml` 泄漏；基础包零运行依赖不变。 |
| 09 | E1 | `test_a_combined_tool_set_keeps_every_execution_semantic`；`EXT` | PASS。本地+扩展工具仍经真实 `ToolGateway`；validator/schema/effects/preview/顺序/`+ext.` 版本/handler 完整。 |
| 10 | E1 | `test_a_tool_name_may_not_be_shared_with_a_host_or_another_extension`、`test_a_skill_key_may_not_be_shared_with_a_host_or_another_extension`、`test_a_host_skill_source_collides_at_open_instead_of_overwriting`；`EXT` | PASS。扩展间/扩展与手工工具名冲突、skill 同键冲突均报双方来源，无覆盖。 |
| 11 | E1 | `test_a_missing_dependency_or_cycle_rolls_the_whole_batch_back`、`test_a_dynamic_contribution_conflict_is_refused_and_rolled_back`；`EXT` | PASS。最终依赖缺项/环拒绝；动态（打开时才知道的）贡献冲突同样整批回滚，无半发布。 |
| 12 | E1 | `test_the_second_factory_failing_cleans_the_first_and_itself`；`EXT`、`PROBE` | PASS。第二个工厂已取得资源后失败：失败项与已成功项都关闭，资源计数归零，宿主 registry 未变。 |
| 13 | E1 | `test_a_contribution_the_host_did_not_bind_is_refused`、`test_a_bound_contribution_must_be_supplied`；`EXT` | PASS。未绑定的贡献不可发布/发现/执行；bound 但未贡献拒绝；能力声明与实际贡献不符拒绝。 |
| 14 | E1 | `test_the_composite_passes_the_existing_skill_provider_contract`、`test_metadata_is_listed_once_and_bodies_and_resources_stay_lazy`、`test_resource_paths_are_confined`；`EXT` | PASS。四接口合同通过；metadata 只列一次、正文/资源按需读取；路径保护与来源指纹有效。 |
| 15 | E1 | `test_a_global_or_other_scope_does_not_leak_across_namespaces`、`test_skill_metadata_cannot_register_a_tool_or_create_an_approval`；`EXT` | PASS。其他 scope/global 回退不跨 namespace；skill hints 不注册工具、不产生批准。 |
| 16 | E1 | `test_a_write_tool_still_previews_and_waits_for_approval`、`test_a_reference_write_is_previewed_approved_and_then_revoked`；`EXT` | PASS。本地写工具先同步 preview/审批再 admit/execute；MCP 写工具的同类证据见 AC33；delegated 后端路径未另建账本。 |
| 17 | E1 | `test_without_the_extra_the_failure_happens_only_when_the_pack_is_opened`、`test_a_local_pack_still_opens_when_no_mcp_pack_is_bound`；base `EXT` + `tests/extensions/test_e1_mcp.py` | PASS。base 环境未绑定/未打开 MCP 时不导入 SDK/不起进程/不联网；显式打开缺 extra 时失败并给安装提示；本地包仍可用。 |
| 18 | E1/E3 | `test_an_adapter_guard_is_combined_not_overwritten`、`test_mcp_extension_roundtrip_over_two_protocols_and_two_transports`、`test_mcp_errors_catalog_drift_and_unknown_keep_their_semantics`；`EXTRA` | PASS。两代协议 × 两种传输真实 `tools/call`；别名/schema/one-shot/中立结果/目录守卫保留；错误、审批、目录漂移、UNKNOWN 各有真实集成证据。 |
| 19 | E2 | `test_the_owner_task_can_close_and_close_is_idempotent`、`test_another_task_cannot_close_or_reopen_the_deployment`、`test_another_event_loop_cannot_close_the_deployment`；`EXT` | PASS。同 owner 开/关成功；跨 task、跨真实 loop 调用被 `ExtensionOwnerMismatch` 拒绝，原 state 不变，随后正确关闭无残留。 |
| 20 | E2 | `test_startup_timeout_and_cancellation_release_the_partial_resource`、`test_close_is_reverse_bounded_and_continues_after_one_failure`；`EXT` | PASS（复核 R5 后加固）。startup 超时/取消回滚；关闭逆序、有界、幂等；一项 close 抛错仍清理其余项；失败工厂的 close 结果合并进 `CloseReport`。 |
| 21 | E2 | `test_revocation_refuses_gateway_and_bridge_calls_before_any_intent`、`test_an_in_flight_call_keeps_its_unknown_receipt_after_revocation`、`test_a_revocation_during_an_approved_read_cannot_admit`、`test_a_direct_gateway_call_must_match_the_deployment_scope`、`test_a_published_local_tool_is_revoked_with_its_deployment`；`EXT` | PASS（复核 R1/R2/R3 后加固）。撤权/关闭后旧 tool/skill/Harness 引用失效；审批 await 后重查、部署 scope 校验、local_tools guard 均拒绝；实际 bridge/直接 Gateway/MCP 新调用零 intent/远端效果；在途调用保留真实 UNKNOWN receipt。 |
| 22 | E2 | `test_closing_one_namespace_does_not_affect_another`、`test_a_borrowed_mcp_manager_is_not_closed_by_the_deployment`；`EXT` | PASS。A scope 关闭不影响 B；借用 MCP manager 只由实际 owner 关闭。 |
| 23 | E2 | `test_an_original_adapter_guard_is_still_enforced_and_not_overwritten`、`test_a_successful_catalog_refresh_makes_the_old_extension_tool_stale`、`test_revocation_refuses_gateway_and_bridge_calls_before_any_intent`；`EXT`、`EXTRA` | PASS。扩展守卫与原 MCP/目录守卫都有效且组合；目录失效/撤权后的旧引用不能绕过；`ToolGateway.invoke` 在准入前执行通用 guard。 |
| 24 | E2 | `test_an_extension_version_change_is_refused_by_a_real_sqlite_resume`；`EXT` | PASS。真实 SQLite Harness 审批后暂停；`2.0.0` 恢复以 `arrangement_drift`（含 `config`）拒绝，零工具、零后续模型请求；同版本重建可完成且 handler 只运行一次。 |
| 25 | E2 | `test_each_recovery_branch_checks_the_deployment_before_dispatch`、`test_a_running_orphan_checks_the_deployment_before_recovering_a_partial_batch`、`test_an_extension_version_change_is_refused_by_a_real_sqlite_resume`；`EXT` | PASS。SUSPENDED、child_result、RUNNING orphan、partial-batch、committed recovery 都在派发前检查 binding；真实 crash fixture 下 revoked binding 拒绝且 A/B 不重跑。 |
| 26 | E2 | `test_the_deployment_digest_joins_the_compared_config_without_hiding_the_old_dimensions`、`test_original_model_prompt_and_host_config_drift_is_still_detected`、`test_a_host_config_value_is_an_input_not_an_override`、`test_manual_deployments_keep_their_exact_fingerprint_shape`；`EXT` | PASS（复核 R4 后加固）。扩展摘要进入被比较的 `config`；宿主 `config` 是合成输入不能覆盖结果；真实 SQLite 下模型/扩展漂移拒绝且零效果，相同配置可恢复；手工部署指纹形状逐字不变。 |
| 27 | E2 | `test_the_same_deployment_resumes_in_a_new_real_process`；`EXT`、`PROBE` | PASS。真新进程重建相同逻辑部署，摘要一致；连接/对象地址变化不误判；已结算 receipt 不重跑 handler，远端效果恰好一次。 |
| 28 | E2 | `test_an_in_flight_call_keeps_its_unknown_receipt_after_revocation`；`EXT` | PASS。在途写后撤权且终态未证明仍为 UNKNOWN；审批/回执/账本事实保留，无自动重放，零新 intent。 |
| 29 | E2 | `test_a_b_c_converge_through_a_real_extension_deployment`；`EXT` | PASS。真扩展部署子进程 A 结算、B 写入后退出、C 未发；恢复先 UNKNOWN 屏障，证据收敛后 C 恰好一次，A/B 不重发，账本 A/B/C 均 succeeded。 |
| 30 | E2/E3 | `test_manual_deployments_keep_their_exact_fingerprint_shape`、`test_manual_examples_still_run`、`test_an_old_run_record_without_an_execution_snapshot_still_reads`；`EXT` + full suite | PASS。无扩展手工部署无指纹/行为回归；旧记录可读；旧未完成调用不伪造扩展等价基线。 |
| 31 | E2 | `test_a_child_gets_only_the_explicit_extension_tool_subset`、`test_a_child_cannot_widen_the_extension_tool_set_or_its_budget`；`EXT` | PASS。真 `ChildCoordinator` 只授显式子集；越权工具/超预算/超 workspace 拒绝；父子 budget 不增加；relation/authorization 不含 factory、resource ref 值或秘密。 |
| 32 | E3 | `test_the_reference_pack_passes_the_extension_contract`、`test_example_13_runs_and_assembles_the_pack_without_core_changes`、`test_mcp_extension_roundtrip_over_two_protocols_and_two_transports`；`EXT`、`EXTRA` | PASS。标准库 tools+skills 与可选 MCP tools+skill 两个参考包、examples 13/14 真实可执行；未改 Core 循环/Gateway/账本。 |
| 33 | E3 | `test_mcp_extension_roundtrip_over_two_protocols_and_two_transports`、`test_mcp_errors_catalog_drift_and_unknown_keep_their_semantics`、`test_a_successful_catalog_refresh_makes_the_old_extension_tool_stale`；`EXTRA`（93 MCP + 扩展） | PASS。两代协议 × 两种传输真实调用；错误/审批/目录漂移/UNKNOWN 都有集成证据；无孤儿进程（manager 关闭 + fixture 退出）。 |
| 34 | E3 | `PROBE`（`scripts/probe_extensions_acceptance.py`） | PASS，退出码 0。覆盖冲突原子性、部分启动回滚、撤权旧引用、版本漂移拒绝、相同部署跨进程恢复、诊断零启动/秘密 canary。 |
| 35 | E3 | `test_a_nested_secret_is_absent_from_reports_messages_events_and_records`、`test_a_factory_exception_message_is_not_copied_into_the_public_error`、`test_a_factory_harness_error_canary_is_not_copied`、`test_close_report_does_not_copy_an_unbounded_exception_name`、`test_the_report_limit_is_a_publication_bound_not_a_truncation`；`EXT` | PASS（复核 R6 后加固）。资源引用秘密不进入公开报告/模型/事件/持久记录；工厂异常原文与 close 异常原文不复制；失败 code 有界；报告超限发布失败而非截断。 |
| 36 | E3 | `test_the_package_version_and_persisted_versions_are_unchanged`、`test_an_old_run_record_without_an_execution_snapshot_still_reads`、`test_an_unknown_run_record_version_is_still_refused`、`test_the_manual_tool_and_harness_surface_is_still_present`、`test_manual_examples_still_run`；`EXT` + full suite | PASS。版本/持久化格式未改；旧/未知版本读法保持；手工 API/示例回归；`SNAPSHOT.json` 与包内容检查通过。 |
| 37 | E3 | `GATE`（base/extra/wheel/sdist） | PASS，退出码 0。base 与 sdist 从树外 `site-packages` 导入；base 扩展节点 109 passed/6 skipped（MCP 缺 extra 单列）；extra 114 passed/0 skipped（deselect 仅 base 负例）+ 93 MCP passed/0 skipped；两环境跑 example 13/14；独立探针 exit 0。报告：`build/extensions-gate/report.json`。 |
| 38 | E3 | `python3.10` 真实解释器 | **BLOCKED**。本机无 Python 3.10；未用静态解析/锁文件替代。E3 因此保持“候选/部分验证”，不宣称全平台全解释器完成。 |
| 39 | E3 | `.venv/bin/python -m pytest -q tests`；`.venv-mcp/bin/python -m pytest -q tests/mcp`；`scripts/probe_mcp_independent_review.py`；`scripts/probe_mcp_failed_refresh.py` | PASS。完整套件见 §4；MCP 专项 93 passed/0 skipped；R1–R4 探针 `passed: 4, failed: 0`、R5 `passed: true`；无新增失败。 |
| 40 | E3 | 本报告、`docs/extensions.zh-CN.md`、`docs/extensions-adr.zh-CN.md`、E0–E3 进度、README 中英文、contract/adapters/recovery/verification、`SNAPSHOT.json`；`EXT` + full suite | PASS（除 AC38 BLOCKED）。实际节点、命令、退出码、结果、跳过与未执行范围已记录；历史 `0.3.0a1`/MCP 数字保留原版本，不混算。 |

## 4. 最终运行结果

下表是独立复核前的实施方原始运行；修复后的最终数字见 §8。

| 检查 | 命令 | 结果 | 退出码 |
| --- | --- | --- | --- |
| 扩展基础套件 | `EXT` | **109 passed, 6 skipped**（6 = 缺 MCP extra 的 MCP 集成节点，单列） | 0 |
| 扩展 + MCP extra | `EXTRA` | **207 passed, 1 skipped**（1 = base-only 缺 extra 负例） | 0 |
| 独立探针 | `PROBE` | `ok: true`，5 组场景全部通过 | 0 |
| 安装门禁 | `GATE` | `ok: true`；base/extra/sdist 全部通过；Python 3.10 BLOCKED | 0 |
| MCP R1–R4 | `scripts/probe_mcp_independent_review.py --report /tmp/ext-mcp-r1-r4.json` | `passed: 4, failed: 0` | 0 |
| MCP R5 | `scripts/probe_mcp_failed_refresh.py --report /tmp/ext-mcp-r5.json` | `passed: true` | 0 |
| 完整回归 | `.venv/bin/python -m pytest -q tests` | 见 §4.1 | 0 |

### 4.1 完整回归

`1432 passed, 19 skipped`（刷新 `SNAPSHOT.json` 后）。19 个 skip 是既有的平台/可选
依赖 skip，不是扩展验收项；扩展 MCP 节点在 extra 环境零 skip。

## 5. 交付物与复核入口

- 代码：`src/yanxu_harness/extensions/`（types/manifest/canonical/plan/assembly/registry/skills/lifecycle/mcp/errors）、
  `src/yanxu_harness/testing/extensions.py`；`DefaultHarness` 可选 `deployment_binding`；
  `ToolGateway.invoke` 通用 dispatch guard；`errors.DeploymentRefused`。
- 示例：`examples/13_extension_tools_skills.py`、`examples/14_extension_mcp_recovery.py`；
  参考包：`examples/extensions/reference_pack.py`、`examples/extensions/mcp_reference_pack.py`。
- 测试：`tests/extensions/`（E0/E1/E2/E3，含 MCP 两协议×两传输、A/B/C、跨进程、子任务）。
- 探针/门禁：`scripts/probe_extensions_acceptance.py`、`scripts/extensions_gate.py`。
- 文档：`docs/extensions.zh-CN.md`、`docs/extensions-adr.zh-CN.md`、本报告；
  README 中英文、contract/adapters/recovery/verification 与 `SNAPSHOT.json` 已更新。

## 6. 已知边界与未执行范围

- **Python 3.10（AC38）BLOCKED**：本机无解释器，未实跑；不因此缩小 `requires-python`。
- 未在 Linux/Windows 重新执行扩展门禁；本次证据是 macOS + Python 3.12.13。
- 未上传包索引、未推 tag、未合 main、未切换宿主 wheel；本报告是**验证候选**，不是
  已发布或已被宿主采用的声明。
- 未执行真实模型质量评测；示例/测试的模型回答由脚本提供。
- 不提供任意 Python 沙箱、插件市场、目录扫描、远程安装、依赖求解器或第二份
  调度/审批/操作账本。
- `ExtensionDeployment` 关闭后的工厂不自动重新打开；同一逻辑部署的新进程/新尝试
  需要宿主显式重建 plan 与 factory。
- 关闭对不合作同步代码不能证明硬中断；`CloseReport.unconfirmed` 如实标记。
- 已知键/秘密清洗由扩展层投影、MCP 清洗和宿主投影共同保证；不宣称能阻止任意
  受信任工厂自行打印秘密。

## 7. 复核结论

E0–E3 的代码、测试、示例、探针、安装门禁与文档均已交付。扩展身份进入真实恢复比较，
撤权覆盖直接 Gateway 与 bridge，部分启动/动态冲突整批回滚，相同部署可跨进程恢复，
不同扩展版本/绑定 revision 会拒绝。除 AC38 因缺少 Python 3.10 解释器保持 BLOCKED 外，
EXT-AC01–37、39、40 均有可复执行的节点与证据。后续是否升 `0.4.0a1`、合 main、
正式发行或切换宿主 wheel，属于独立复核后的决策，不在本次实现范围内。


## 8. 独立复核修复（EXT-R1–R6）

独立复核在 `6cd2b41` 上复现 4 项 P1、2 项 P2。修复如下，完整复现和验收门槛见
[独立复核与修复交接](extensions-independent-review.zh-CN.md)：

| 项 | 修复 | 回归证据 |
| --- | --- | --- |
| R1 审批 await 后撤权仍执行 | `ToolGateway.invoke` 在审批 `get/supersede/put` 返回后、构造 intent 前再次执行同一 dispatch guard；已准入调用不重查，保留 receipt/UNKNOWN。 | `test_a_revocation_during_an_approved_read_cannot_admit`、`test_a_revocation_during_a_new_approval_write_cannot_admit`；独立探针 `approval_wait_revocation`。 |
| R2 直接 Gateway 绕过部署 scope | 组合 guard 增加部署 namespace/授权 scope 校验；scope 只从宿主 `RunContext` 读取，缺失/不匹配拒绝；MCP 原目录 guard 仍在其后执行。 | `test_a_direct_gateway_call_must_match_the_deployment_scope`、`test_mcp_gateway_refuses_a_foreign_scope_before_the_remote_call`；独立探针 `gateway_scope`/`mcp_gateway_scope`。 |
| R3 `local_tools` 无撤权守卫 | `_freeze_local` 与扩展工具共用部署 guard（保留宿主原 version）。 | `test_a_published_local_tool_is_revoked_with_its_deployment`。 |
| R4 宿主 config 覆盖合成指纹 | 扩展路径合成后重新写入 `config`，宿主 `config` 只作为 `config_payload["host"]` 输入；无扩展路径不变。 | `test_a_host_config_value_is_an_input_not_an_override`（真实 SQLite，模型漂移拒绝、相同配置恢复）；独立探针 `config_override_resume`。 |
| R5 失败工厂清理未进 CloseReport | 失败工厂 close 结果与先前工厂结果合并进唯一报告；成功/失败/超时/缺 close 均记录。 | `test_a_failed_startup_close_failure_is_in_the_public_report` 等 5 个回归；独立探针 `failed_startup_cleanup`。 |
| R6 CloseReport 复制异常原文 | 公开失败项只保留有界 ID 和异常类型/稳定 code；只有包内 `public_safe` 错误可保留固定提示。 | `test_a_factory_harness_error_canary_is_not_copied`、`test_a_factory_extension_error_canary_is_not_copied_either`、`test_close_report_does_not_copy_an_unbounded_exception_name`、`test_close_report_redaction`。 |

修复后的实际运行（macOS / Python 3.12.13）：

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 扩展基础 | `.venv/bin/python -m pytest -q tests/extensions` | **122 passed, 7 skipped**, exit 0 |
| 扩展 + MCP | `.venv-mcp/bin/python -m pytest -q tests/extensions tests/mcp` | **221 passed, 1 skipped**, exit 0 |
| 独立复核探针 base | `.venv/bin/python scripts/probe_extensions_independent_review.py` | **6/6 passed**, exit 0 |
| 独立复核探针 MCP | `.venv-mcp/bin/python scripts/probe_extensions_independent_review.py --with-mcp` | **7/7 passed**, exit 0 |
| 完整回归 | `.venv/bin/python -m pytest -q tests` | **1445 passed, 20 skipped**, exit 0 |
| 安装门禁 | `scripts/extensions_gate.py` | `ok: true`；base/sdist 122 passed/7 skipped，extra 128 passed/0 skipped + 93 MCP passed/0 skipped；Python 3.10 BLOCKED |
| 原验收探针 / MCP R1–R5 | 既有命令 | exit 0；MCP R1–R4 4/4、R5 true |

AC38 仍是 **BLOCKED**（无 Python 3.10 解释器）。本轮未升版本、未上传、未推 tag、
未合 main、未切换宿主 wheel；R1–R6 的修复状态为“已修复，待独立复核”，不自行宣布
独立验收通过。
