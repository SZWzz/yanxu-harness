# Harness 下一阶段实施进度

记录从 2026-10-29 开始的实际实施状态。阶段计划与验收边界见[详细开发实施方案](harness-next-stage-implementation-plan.zh-CN.md)。R0–R3、E0–E3、B0–B4、C0–C3、O0–O2 已执行；本次修复后候选已在 macOS CPython 3.12.13 通过完整六步离线门禁。当前代码在 Python 3.10/3.11、Linux/Windows 的新矩阵仍缺验证，条件阶段 S0 尚未触发。

## R0：工作区与基线

| 项目 | 实际记录 |
| --- | --- |
| 仓库 / 分支 | `/Volumes/etx/coding/yanxu_harness` / `codex/plan-dag-harness` |
| 开工 HEAD | `c034c0e80d6f53e97b44ba2c0fa221e85be190a4` |
| 开工工作树 | 已有 Plan P0.6 源码、测试、示例、文档和 `SNAPSHOT.json` 未提交改动；方案文件和 README 索引改动由用户原工作区带入。开工时完整路径见下文。没有执行 reset、clean 或覆盖操作。 |
| 解释器 / 平台 | 主解释器 `python`：Python 3.12.13，macOS 27.2 arm64；`python3.11` 为 3.11.15；`python3.10` 不存在。 |
| 项目版本 | `pyproject.toml` 与源码运行时版本为 `0.4.0a1`，最低 Python `>=3.10`，基础依赖仅标准库。 |
| 可选 extras | `pydantic`、`yaml`、`mcp`、`dev`；当前机器级 Python 环境很宽且不隔离，MCP SDK 版本与项目声明不一致，因此不能用已安装包清单认证 extra。 |
| 本地验证环境 | 仓库 `.venv` 使用 Python 3.12.13。`python -m pip show yanxu-harness` 的 editable 元数据仍报 `0.1.0a2`，但实际导入 `src/yanxu_harness/__init__.py` 的运行时版本是 `0.4.0a1`；验证以源码版本和实际导入路径为准。 |
| SQLite / 控制 schema | 当前实现常量：Run SQLite schema 1、Task control schema 1、Task/Plan 记录 schema 1。开工基线测试未使用持久数据库；测试临时库由用例创建。 |
| wheelhouse | 开工前已有忽略目录 `build-wheelhouse/`，含嵌套副本和多组不同构建版本的 wheel。未把它当作当前候选的可复现依赖集，未清理或覆盖。 |

开工时已有的变更按来源记录如下。无法仅凭工作树把混合文档的每个 diff hunk 归到唯一作者，因此整体保留，后续提交不应顺带纳入：

- P0.6 已有跟踪文件：`SNAPSHOT.json`、`docs/README.md`、`docs/adapters.md`、`docs/contract.md`、`docs/harness-evolution-roadmap.zh-CN.md`、`docs/plans-progress.zh-CN.md`、`src/yanxu_harness/diagnostics/__init__.py`、`src/yanxu_harness/diagnostics/__main__.py`、`tests/fault_windows.py`、`tests/test_plans_acceptance.py`、`tests/test_plans_replanning.py`、`tests/test_plans_scheduler.py`、`tests/test_plans_store.py`。
- 开工时已有未跟踪文件：`docs/plan-dag-harness-implementation-plan.zh-CN.md`、`docs/plans-acceptance-review.zh-CN.md`、`docs/plans-delivery-report.zh-CN.md`、`docs/plans-integration.zh-CN.md`、`examples/15_plan_dag.py`、`examples/16_plan_recovery.py`、`examples/plan_dag_support.py`、`src/yanxu_harness/diagnostics/plans.py`、`tests/test_diagnostics_plans.py`、`tests/test_examples_plans.py`。
- 用户带入的当前任务材料：`docs/harness-next-stage-implementation-plan.zh-CN.md`，以及 `docs/README.md` 中已经存在的方案索引改动。两者均保留。
- 本次 R1 修改：`src/yanxu_harness/core/transitions.py`、`tests/core/test_core_loop_result.py`、`tests/core/test_core_tool_execution.py`，另新增本进度记录。它们与开工前文件清单分开归属。

状态冲突已识别：P0.6 交付记录描述的是上一轮工作树快照；本方案在编写时标为“待实施”；本进度文件记录当前已开始实施。旧 P0.6 通过数、失败和平台缺口继续归属其历史快照，不覆盖成当前 R1/R3 结论。

## R1：Core 取消失败归因与修复

### 归因

开工时在 Python 3.12.13 上单独运行两条失败用例，结果为 `2 failed`：

```sh
python -m pytest -q \
  tests/core/test_core_loop_result.py::test_cancellation_between_turns_is_reported_as_cancelled \
  tests/core/test_core_tool_execution.py::test_cancellation_before_any_hook_yields_an_aborted_result
```

将发行 tag `v0.4.0a1` 归档到 `/tmp`，使用同一解释器运行相同命令，结果为 `2 passed`。两条旧测试都在发起 Runner 前就取消令牌；旧版因此仍调用 Provider 并产生测试期待的结果，当前候选则按“请求前已取消不得派发”正确地不调用 Provider。旧版通过不能证明当前计划要求的行为，两个测试的取消时点需要修正。

第一条改为在真实模型回答完成、`TurnEnd` 已被 reducer 接受后取消，并断言 Provider 只收到一次请求且真实回答仍保留。另加预取消用例，断言模型请求次数为零、结果没有虚构文本。

第二条改为在真实 Provider 返回工具声明后、`ToolExecutionStart` 时取消；它不配置 `before_tool_call`，从而覆盖无 hook 分支的执行前守卫。该时点复现了一个真实缺陷：工具返回明确的 `Operation aborted` 结果后，Core 仍请求了第二轮模型响应。`_stop_hook_evaluated` 未检查已记录的 `cancel_requested`，将执行推进到了下一次 request preparation。

### 修复与验证

- 在 `_stop_hook_evaluated` 发现取消已记录时，结束为 `CANCELLED`；此前已接收的模型文本和工具观察仍保留，不再准备或派发下一轮模型请求。
- 修复前的修正后工具用例曾以 `stream.calls == 2` 失败；该断言证明失败源于多余模型派发，而非工具 handler 被执行。
- 修复后聚焦用例：`3 passed`（两条修正用例和新增的预取消无派发用例）。
- 相邻 Core 回归：`python -m pytest -q tests/core/test_core_loop_result.py tests/core/test_core_tool_execution.py tests/core/test_core_execution_boundaries.py tests/core/test_core_execution_state.py tests/core/test_core_cancellation.py tests/core/test_core_cancellation_isolation.py` → **102 passed**。
- 全部 Core 测试：`python -m pytest -q tests/core` → **297 passed**。
- DefaultHarness、deadline 与决策路由回归：`python -m pytest -q tests/test_runtime_default_harness.py tests/test_decisions_model.py tests/test_runtime_execution_snapshot.py` → **64 passed**。
- 快照、公开 API、白名单和 import fence：`python -m pytest -q tests/test_build_scripts.py::test_the_committed_manifest_matches_the_current_tree tests/test_public_api.py tests/test_public_snapshot_whitelist.py tests/test_import_fence.py` → **13 passed**。最终文档状态更新后，`python scripts/export_public_snapshot.py --refresh-manifest` 重刷为 383 个文件；随后 `python -m pytest -q tests/test_build_scripts.py` → **17 passed**。
- 完整源码、离线 CI、wheel/sdist 安装以及平台矩阵尚未运行；归入 R3。

O0–O2 完成后的完整套件又暴露出一个边界问题：`test_a_cancelled_run_executes_no_tool_handler` 对初始 User `message_end` 也触发取消，而 Runner 当时把“Provider 未调用”抛成裸 `CancelledError`。现在该用例只在 Assistant 回答结束时取消，继续覆盖真实工具声明后的无执行结算；另有回归覆盖输入事件通知时取消，以及取 API key 期间取消。Reducer 在 `_issue_request` 阻止已经记录的取消进入下一次派发；Runner 在最终 dispatch 检查把竞态取消作为 `CancellationRequested(before_dispatch=True)` 归约，结算尚未发送的 budget reservation，并返回结构化 `CANCELLED`。相关 Core 聚焦回归 **51 passed**；完整套件和最终 CI 结果见下文 R3/验证记录。

## R2：本地探针与独立审查（原范围已完成，预算/Gateway 补充复审已关闭）

独立审查者按用户授权检查了摘要、principal/审批撤销、SQLite claim/pause/replan 竞争、验收证据漂移、跨进程 Gateway unknown、跨 revision 执行次数、普通 Task 隔离及只读诊断，并实际穿过 SQLite、Worker、DefaultHarness 和 Gateway。结论是 **R2 通过当前 P0.6 契约**，没有未披露的阻断项；范围、证据与接受边界见[独立复核报告](harness-next-stage-independent-review.zh-CN.md)。该结论是实现审查，不构成安全认证。以下本地探针作为补充证据。

- 聚焦 Plan 存储、调度、接受、重规划、诊断及 Gateway：`.venv/bin/python -m pytest -q tests/test_plans_contract.py tests/test_plans_store.py tests/test_plans_scheduler.py tests/test_plans_acceptance.py tests/test_plans_replanning.py tests/test_diagnostics_plans.py tests/test_tools_gateway.py` → **169 passed**。其中包括双 SQLite 连接准入竞争、跨进程恢复/unknown、跨 revision 执行次数、普通 worker 对绑定 Task 的拒绝及 evidence drift 探针。
- Plan 示例、DefaultHarness 与恢复路径：`.venv/bin/python -m pytest -q tests/test_examples_plans.py tests/test_runtime_default_harness.py tests/test_runtime_resume.py` → **44 passed**。
- 本地审查发现 `PlanInspector` 只读取当前 revision 的 bindings，但调度准入按 Plan 全 revision 的执行次数和活动绑定数限额检查，因此并发槽已满时会把实际不可准入的 unbound step 报成 `ready`。新增真实 SQLite 只读回归用例先以 `ready != blocked` 失败，再修正诊断读取为 Plan 全量 bindings 并应用同一执行/并发门槛；修后该用例通过。`docs/contract.md` 已补充该视图为 Plan 范围解释、非原子且不能授权派发的契约。
- 新回归：`tests/test_diagnostics_plans.py::test_plan_inspector_marks_unbound_step_blocked_when_concurrency_is_full`；修复前 **1 failed**，修复后 **1 passed**。
- 同一审查还发现 `claim_plan_step` 的同 holder 活跃 claim 幂等重放分支会在重新验证当前 approval/revocation 前返回旧 claim。`tests/test_plans_store.py::test_revoked_approval_blocks_replay_of_an_existing_live_step_claim` 在 Memory 与 SQLite 上修复前 **2 failed**；现该分支重放前重新检查当前 Plan 准入门槛，且未撤销 approval 下的幂等重放仍保留。关联回归共 **6 passed**。

### B2/E2 补充复审：派发阶段、策略竞争与取消清账

用户审阅当前候选时报告四项缺陷：receipt 的 `rejected/cancelled/deferred` 状态会把已进入 handler 的 dispatch 错退额度；策略读取和预留之间的更新可绕过新增 hard 限额；执行绑定在异步预算预留等待期间撤销后仍可能执行 handler；`WorkResult.to_record()` 被放在错误类上。四项均已修复，并各有回归：预算按持久化的 `handler_entered` 阶段结算；预留事务校验完整 owner→digest 集合；预算预留后再次检查执行绑定；`to_record()` 回归到 `WorkResult`。

独立复审随后扩展了取消/恢复边界：post-reservation guard 被取消、reserve 等待期间取消、SQLite admission 后但 handler 前取消、未取得 reservation handle 时 adapter 在已提交 reservation 后失败，以及 receipt 已持久化后预算结算被取消。Gateway 对已知未派发写入 `handler_entered=False` receipt；对 handle 缺失的失败路径按稳定 dispatch identity reconcile；receipt 与预算 settlement/reconciliation 在 shield 下完成后再传播 task cancellation。准入前已取消仍保持无 receipt、无 intent。无预算 reservation 的绑定拒绝 replay 不会在额度耗尽时尝试新预留。

最新独立复审确认原四项与上述边界均已关闭，未发现新的具体问题。预算、Gateway、恢复、调度和公开 API 聚焦回归：`pytest -q tests/test_typed_budget_dispatch.py tests/test_typed_budget_store.py tests/test_tools_gateway.py tests/test_tools_ledger_recovery.py tests/test_runtime_recovery_compat.py tests/test_plans_scheduler.py tests/test_public_api.py` → **182 passed**。当前候选的完整离线 CI 结果见下方追加的最终复验记录；Linux/Windows 与当前修复后的 Python 3.10/3.11 候选仍未在本机重跑。

## R3：当前候选兼容与制品门禁（最终源码已复验）

三个解释器均在 macOS 27.2 arm64 上使用隔离环境，通过 `scripts/ci_check.py --offline` 的六项门禁：安装 `dev,yaml,pydantic` extras、完整源码测试、冷启动 import fence、构建 wheel 与 sdist 并在源码树外安装运行测试和示例、相对 Markdown 链接、许可证来源文件。Python 3.10 与 3.11 使用 `/tmp` 下隔离 venv 及各自 wheelhouse；Python 3.12 使用仓库 `.venv`。所有 wheelhouse 在准备阶段联网下载，门禁本身离线运行。

| Python | 完整套件 | 完整候选 CI | 记录 |
| --- | --- | --- | --- |
| 3.10.18 | `1598 passed, 22 skipped in 100.85s` | `steps: 6; failed: none` | wheel、sdist 树外安装验证和示例均通过；日志 `/tmp/yanxu-harness-r3-ci-310-20261029.log`，制品目录 `/tmp/yanxu-harness-r3-ci-310-20261029`。wheelhouse：`/tmp/yanxu-harness-r3-wheelhouse-310-20261029`。 |
| 3.11.15 | `1598 passed, 22 skipped in 103.25s` | `steps: 6; failed: none` | wheel、sdist 树外安装验证和示例均通过；日志 `/tmp/yanxu-harness-r3-ci-311-venv-20261029.log`，制品目录 `/tmp/yanxu-harness-r3-ci-311-venv-20261029`。wheelhouse：`/tmp/yanxu-harness-r3-wheelhouse-311-20261029`。 |
| 3.12.13 | `1598 passed, 22 skipped in 97.47s` | `steps: 6; failed: none` | wheel、sdist 树外安装验证和示例均通过；日志及制品目录位于 `/tmp/yanxu-harness-r3-ci-20261029-01`。wheelhouse：`/tmp/yanxu-harness-r3-wheelhouse-20261029-01`。 |

O0–O2 交付和 R1 取消边界跟进完成后，针对最终源码树（含 412 项公开快照）重新跑了完整六步离线 CI。三个解释器的源码全套件、cold import fence、wheel/sdist 树外全套件及示例、Markdown 链接和来源文件门禁均通过：

| Python | 完整套件 | 完整候选 CI | 保留制品目录 |
| --- | --- | --- | --- |
| 3.10.18 | `1780 passed, 22 skipped in 115.73s` | `steps: 6; failed: none` | `/tmp/yanxu-harness-final-ci-310-20261029` |
| 3.11.15 | `1780 passed, 22 skipped in 114.39s` | `steps: 6; failed: none` | `/tmp/yanxu-harness-final-ci-311-20261029` |
| 3.12.13 | `1780 passed, 22 skipped in 112.12s` | `steps: 6; failed: none` | `/tmp/yanxu-harness-final-ci-312-20261029` |

上方最初的 1,598 用例数据和下方制品 SHA-256 属于 O0–O2 前的 R3 历史候选；最终源码重验的制品保存在新目录，不用旧摘要冒充新制品。

首个 3.11 尝试把 uv 管理的基础解释器直接交给 CI 安装步骤，触发 PEP 668 且 pytest 不可用；该次不作为验证结果。改用 Python 3.11 venv 后重新执行了完整离线 CI，表中记录的是成功重跑。Python 3.10.18 由 uv 安装到 `/tmp/yanxu-harness-r3-python`，未改动系统默认解释器。

以下 SHA-256 是首轮 R3 候选的历史制品摘要，不代表上表中的最终源码复验制品。首轮制品针对同一旧源码状态构建，文件字节会随 Python 构建工具环境不同而异。

| 构建解释器 | 制品 | 大小（bytes） | SHA-256 |
| --- | --- | ---: | --- |
| 3.10.18 | `yanxu_harness-0.4.0a1-py3-none-any.whl` | 667164 | `4829fd87ef23b52782ebae2e5fc075c5dd8ee6b8c8cbf1094a6952b477b916bd` |
| 3.10.18 | `yanxu_harness-0.4.0a1.tar.gz` | 1629536 | `20ced5ef9914540a3dd10fea1702e5c7144552f1f4b237c081105cfaad207ce0` |
| 3.11.15 | `yanxu_harness-0.4.0a1-py3-none-any.whl` | 667164 | `c1a2148295856a530df24a68afe9cc763332eacd693e20bd49993d5aee93cf81` |
| 3.11.15 | `yanxu_harness-0.4.0a1.tar.gz` | 1629509 | `d3e5ff979a85c19f0051176ab209a19e53bf090b3cfb348dbf498ce0c3288160` |
| 3.12.13 | `yanxu_harness-0.4.0a1-py3-none-any.whl` | 667164 | `b20f63ec55983a90b5838cb38599a5e494c5c76233be598ea45e3edec06f8305` |
| 3.12.13 | `yanxu_harness-0.4.0a1.tar.gz` | 1629596 | `15c51bcca23f82b4104b3a7436ac2855a9e3559cd4e93c76a240298e70c911b1` |

**平台缺口：**本机没有 Python 3.10 Linux、Linux 容器 daemon 或 Windows 运行时。虽然 Docker CLI 存在，`docker info` 无法连接到 Colima socket，因此未启动或安装新运行时。没有触发远程工作流；Linux 和 Windows Python 3.10/3.11/3.12 以及 Windows Python 3.12 都未验证。历史发行版本的矩阵不计作当前候选证据。

R3 结论：最终源码树在当前本地可用的 macOS Python 3.10/3.11/3.12 完整候选门禁通过；上述 Linux/Windows 适用矩阵为明确环境缺口，结论不能外推为跨平台认证。

## E0：执行绑定合同和值类型

- 冻结了 [E0 执行绑定契约](execution-binding-contract.zh-CN.md)：请求字段与有效 Step override 规则、不可变部署描述、resolver/guard 协议、拒绝结果、摘要和能力信任边界。
- 新增 `src/yanxu_harness/plans/bindings.py` 并从 `yanxu_harness.plans` 显式公开：`ExecutionBindingRequest`、`ExecutionBindingReferences`、`ExecutionDeploymentDescriptor`、`ResolvedExecutionBinding`、两个端口、结构化拒绝类型与 `step_binding_id`。
- `ExecutionBindingRequest.from_snapshot` 验证 current revision/definition/envelope 摘要、审批有效/未撤销/未过期、Plan 当前状态和宿主 principal 引用，并以 step-level profile/workspace override 计算有效引用。不会把自由文本、凭据或活动对象写入快照。
- `step_binding_id` 固定为 Plan Store 已有 identity 公式（普通 binding v1、retry binding v2），以便 E1 在控制事务内复核 ID，避免为新功能改写历史 binding 身份。
- 新测试 `tests/test_plan_execution_bindings.py` 先因公开 API 尚不存在而收集失败（ImportError），实现后 **11 passed**。其余覆盖摘要覆盖字段、schema 拒绝、冻结/深拷贝、审批状态、Step override、retry identity 和 Guard 结果约束。
- E0 聚焦 + 公共 API / 快照 / import fence / Python floor：`.venv/bin/python -m pytest -q tests/test_plan_execution_bindings.py tests/test_build_scripts.py tests/test_public_api.py tests/test_public_snapshot_whitelist.py tests/test_import_fence.py tests/test_python_floor.py` → **51 passed**。同一聚焦集合去掉快照/build 后 Python 3.10.18 → **32 passed**；所有新行为保持标准库依赖。
- E0 阶段刷新 `SNAPSHOT.json`：387 个文件，6,165,547 bytes，摘要 `sha256:66e0d083fb3dcb35107d623b4e9490be4a5e300ea78d5b11c877a26cb37bccfc`。E1/E2 后公开 API 又有增加；在后续公开接口收敛后统一刷新当前快照，E0 摘要仅作为阶段历史记录。

E0 的类型与序列化合同已经实现。E1–E3 的绑定持久化、派发防护、Gateway 预览漂移与宿主集成证据如下。

## E1：准入、持久化与恢复接线

- 新增可选 `ExecutionBindingStore` 能力，并在 Memory/SQLite 控制存储实现原子 claim：Plan Step binding、Task、INITIAL 输入、scheduler claim 和已解析的执行绑定在同一控制事务中提交。Plan schema 升至 v3，包含 `plan_execution_bindings`；支持 v1→v2→v3 与 v2→v3，未知 schema 仍拒绝，read-only store 不执行迁移。
- `LocalWorker` 在事务外解析新的 Plan candidate，再以当前 revision、审批、控制版本、可信 principal 和快照摘要执行事务内复核。既有 Task 恢复读取已存绑定，不调用当前 resolver；旧的无绑定 Plan Task 不会被静默升级。
- `RunContext.execution_binding` 只在进程内携带 dispatch guard，不进入序列化。`DefaultHarness` 将 binding digest 加入 Run arrangement；Core model/tool dispatch 与 ToolGateway handler 前分别做末端校验，拒绝配置漂移、未知绑定、缺少 guard 或不支持的 Harness。恢复终态 Run、读取/结算已有 Gateway receipt 不要求再次派发。
- 恢复边界测试覆盖 pause、replan、approval revoke 后同 Task 重新 claim；不同 namespace、绑定事务失败、schema 未知、已有未绑定 Task、部署漂移零 Provider 调用、撤权后终态 Run 结算，以及两个 SQLite 连接对控制关闭竞争。新绑定只准入新 Task，恢复不会重算或扩大批准范围。
- SQLite migration 聚焦：`.venv/bin/python -m pytest -q tests/test_plans_acceptance.py -k 'schema_v1 or schema_v2 or read_only_store_refuses_plan_migration'` → **3 passed**。
- Plan/store/scheduler/Gateway/Core/恢复聚焦：

  ```sh
  .venv/bin/python -m pytest -q \
    tests/test_plans_scheduler.py tests/test_plans_store.py tests/test_plans_acceptance.py \
    tests/test_plans_replanning.py tests/test_plan_execution_bindings.py \
    tests/test_tools_policy.py tests/test_tools_gateway.py \
    tests/test_runtime_execution_bindings.py tests/core/test_core_agent_loop.py \
    tests/core/test_core_tool_execution.py tests/test_provider_core_bridge.py \
    tests/test_runtime_resume.py tests/test_runtime_commit_boundaries.py
  ```

  → **354 passed**。

## E2：Gateway 预览和资源版本

- 完成既有契约审计后，没有新增重复的通用 preview 对象。现有 ToolDefinition preview 与 ToolInvocation `target_version` 已能提供 host 资源摘要和目标版本；真正资源约束仍由宿主 adapter 负责。
- `preview_hash` 使用公开的 `PREVIEW_DIGEST_VERSION = 1`，摘要域包含版本号与 preview；旧摘要会进入既有 supersede/reapproval 流程。显式 `preview_hash=None` 会参与比较，所以删除 preview 与未提供 preview 不再混为一谈。工具版本漂移先于参数摘要给出具体拒绝原因。
- 新 Gateway 测试通过真实 handler 断言 preview 改变/移除、目标版本、tool version、policy version 漂移会重新询问审批且零 handler；deployment `dispatch_guard` 漂移在准入/派发前拒绝。已确认 receipt 重放及 unknown 不自动重试仍沿用操作账本事实。
- `docs/contract.md` 记录目标版本由服务端提供以及 preview 摘要版本/删除漂移语义。
- E2 Gateway、policy、Core bridge、SQLite ledger 和 MCP catalog/Gateway 聚焦：`.venv/bin/python -m pytest -q tests/test_tools_policy.py tests/test_tools_gateway.py tests/test_tools_core_bridge.py tests/test_storage_sqlite.py tests/mcp/test_catalog.py tests/mcp/test_gateway.py tests/extensions/test_e3_mcp_extension.py` → **85 passed, 11 skipped**。跳过项是可选集成依赖/环境缺口，不计作通过用例。

## E3：中性宿主参考装配

- 新增 `examples/17_execution_binding.py`：前两段分别用旧 Harness 独立启动 Run、用旧 LocalWorker 完成普通 Task，均不配置绑定适配器；Plan 段经批准的 profile A 将只读 `notes.read` 和真实 workspace 内容摘要绑定进 SQLite，再由真实 Core Runner / ToolGateway / SQLite operation ledger 执行。
- 子进程在 Task=`waiting`、Run=`suspended` 且一条 Gateway receipt 已提交时退出。父进程重新打开两个 SQLite store，修改 profile 和 workspace 后通过持久绑定的 model dispatch check 得到 `actual_deployment_mismatch`；这次拒绝产生 **0 次新 Provider dispatch 和 0 次新 handler 调用**。恢复 profile A 与原 workspace 后，Worker 用同一个 Task/Run/绑定继续；原 receipt 未重复执行，Plan 完成。
- 描述符只声明 model/tool binding guard，不声明 `workspace_isolation`。示例明确输出隔离能力“不支持”且未声称 sandbox；read adapter 只允许一个固定文件，这只是此 demo 的应用级访问限制。
- 示例回归：`.venv/bin/python -m pytest -q tests/test_examples_plans.py` → **4 passed**。该文件运行真实子进程恢复示例，检查旧入口、漂移零派发、持久 receipt 和原配置恢复。
- E3 参考程序集成目前用 SQLite；Memory 语义由 E1 参数化存储回归覆盖。跨平台矩阵仍沿用 R3 已登记的 Linux/Windows 环境缺口，不能外推为跨平台通过。

## B0：预算 owner 与策略快照合同

- 新增并冻结[预算策略合同](budget-policy-contract.zh-CN.md)：B0 将账户身份固定为 `task_root + namespace + root_task_id` 或 `plan + namespace + plan_id`。Plan revision 不进入账户 key；session 是访问/归属元数据，不会把跨 revision 账户拆开。B1 增加 `task_grant + namespace + spawn_identity` owner。
- 新增公开不可变 `BudgetLimit`、`BudgetPolicySnapshot` 与 `BudgetLimitMode`。limit 只接受非负整数；单位区分 input/output token、毫秒、微币种；快照按 unit 排序、拒绝重复 unit，含 schema version 与域分离摘要。
- Token/cost 的 hard limit 必须附带 host 上界引用；没有可执行上界时快照标为 observe。Cost 还必须包含币种与 price version。旧通用 `tokens` 与 `seconds` 保留给 TaskBudget API，但不能进入新快照：方向不明的 tokens 拒绝迁移，seconds 只在精确到整数毫秒时转换；cost 转换要求显式币种/价格元数据。
- `snapshot_from_task_budget(...)` 为显式转换边界，验证 NaN/Infinity、负值、非整数计数和精度损失，不自动改写任何既有 Task reservation。旧 `TaskBudget`、`BudgetLedger`、SQLite `budgets` 表和其 float API 均保持原状；B1 的 typed reservation 使用独立 schema，B2 接入真实派发，B4 处理旧 reservation 诊断/迁移。
- 新增 `tests/test_budget_policy.py` 覆盖 owner identity、摘要/round-trip、未来 schema、零/负/非有限/小数、token/cost hard-bound、Legacy 转换和拒绝边界。聚焦类型、已有 Task 预算回归与公开 API：`.venv/bin/python -m pytest -q tests/test_budget_policy.py tests/test_tasks_budgets.py tests/test_public_api.py` → **36 passed**。
- 当前公开 API 清单在 E0 snapshot 后已变化；待后续公共接口定型后统一刷新 `SNAPSHOT.json` 并运行构建/导入门禁。

## B1：typed reservation 与多范围覆盖

- 新增可选 `TypedBudgetStore` capability 与公开 `TypedBudgetDispatch`、`TypedBudgetReservation`、`BudgetScopeCoverage`、`TypedBudgetUsage`。输入只含 Task、Run、dispatch identity、真实 request instance、unit、整数 amount 及 cost 元数据；调用方不能传入/删减 scopes。
- 控制存储在 reservation 事务中从已提交 Task parent chain、Plan Step bindings 和 ChildRelation 推导 `task_root`、`plan`、所有适用 `task_grant` scopes。父链缺 relation、root 不一致、跨 namespace/session、跨多个 Plan 或 scope policy session 不匹配时拒绝。
- 同一 dispatch identity 在 namespace 内唯一；重放要求 Task/Run/request instance/unit/数量/币种/price version 及推导 scope 一致，否则拒绝。同一 dispatch 使用一条 canonical reservation；各 scope 的 coverage 行仅引用这条 actual/state 事实。
- Memory 在同一 `RLock` 临界区检查额度并写入；SQLite 新增独立 `typed_budget_schema_version=1` 和 policy/dispatch/coverage 表，在 `BEGIN IMMEDIATE` 中完成多 scope hard-limit 检查与写入。scope 不足时事务未写；中途注入失败会回滚 canonical 行和 coverage。typed store 显式 opt-in；只读旧库启用时拒绝，不建表、不迁移。旧 `budgets` schema/API 未改。
- 每个 owner/unit 的用量按 settled actual + active reserved + unknown reserved 计算；released 不占额。实际超过预留的 usage 不截断，并关闭后续 hard admission。所有 cost scope 与 dispatch 必须采用同币种和 price version；observe cost 只记录，不显示 hard available。
- B1 合同见[预算覆盖合同](budget-reservation-contract.zh-CN.md)。专项测试 `tests/test_typed_budget_store.py` 覆盖 Memory/SQLite Plan + child grant scopes、任一 scope 不足、幂等冲突、单一 actual、多进程争最后额度、插入失败回滚、unknown/overage、cost 元数据、SQLite 重开恢复及只读 schema 门禁。真实 Provider/Gateway 派发接线与迟到 usage 留给 B2。
- 离线示例 `examples/18_typed_budget_scopes.py` 展示一个 reservation 同时覆盖已提交的 Plan、Task root、child grant scopes；幂等重放返回同一 reservation，示例未打开外部派发窗口并安全释放，Provider 调用数为 0。`tests/test_examples_plans.py` 对 scope、用量和释放结果作回归断言。
- 当前 B1 聚焦复验均退出码为 0：typed store/B0/Task/public API/import 组 **91 passed**；Plan/store/scheduler/acceptance/replanning/binding/diagnostics/examples 组 **186 passed**；Task contract/release regressions/acceptance matrix 组 **58 passed**，合计 **335 passed**。typed store 自身 **16 passed**。

## B2：真实派发、usage 与故障恢复（已完成）

- 新增公开 `TypedDispatchBudget` 接入 `DefaultHarness` 和 `ToolGateway`。主模型在 Runner 的 dispatch callback 中原子预留 `model_requests=1` 与所有 hard token/cost bounds；Gateway 在 handler 入口前预留 `dispatches=1`。幂等 request/operation identity 冲突时拒绝第二次外部调用。
- `upper_bounds` 按不可变策略中的 `hard_bound_ref` 绑定宿主可执行能力。每个请求解析出来的同一 unit 上界及 cost 元数据必须在所有适用 scopes 一致；缺少 capability 或数值不是非负整数时在预留前拒绝。Runner 只将当前模型、最终消息和 output cap 交给进程内 resolver；不把请求材料写进预算事实。
- Provider `Usage` 中的 input/output token 及可选 cost 作为 actual 记录。缺失的 hard usage 保留完整 reservation 为 unknown；未知 usage 不转成 0。迟到报告通过 `record_id + unit` 幂等补记；原 reservation 同单位的 report identity 冲突会被拒绝。实际值超过 reservation amount 时保留完整 actual，并通过 `TypedBudgetUsage.upper_bound_overrun` 报告宿主上界被突破的量；hard limit 超额关闭新准入。
- 已接入辅助摘要：`DefaultHarness` 将 Task/Run context 与 typed budget 传到 `ContextController`/`SummaryCompactor`；cache hit 不派发。`ModelDecisionRouter` 提供可选 typed budget/context/usage extractor，host call 前预留，失败或取消保持 unknown，缺 usage 不伪造 0。
- 新增 `reserve_provider_retry(...)` 供宿主自定义 Provider adapter 对每个额外真实请求单独预留，并用 `settle_auxiliary(...)` 结算；同一 retry identity 不可重复授权。内建 OpenAI-compatible transport 禁止自动重试。自定义 adapter 必须把相同 typed budget 与 Task/Run context 接到每次内部请求，否则不能声称 hard `model_requests` 覆盖了隐藏重试。
- 新增[预算派发合同](budget-dispatch-contract.zh-CN.md)与离线示例 `examples/19_typed_budget_dispatch.py`，覆盖 fake primary Provider、辅助调用 unknown/迟到 usage、Gateway receipt replay；所有调用均在本地，不连网络。`tests/test_examples_plans.py` 增加示例输出断言。
- 子进程故障用例覆盖预留后、模拟请求已送达但无响应、usage 已提交但 Run 未观察，以及 usage 事务提交后回复丢失；新进程读取 durable coverage，unknown 不释放，迟到/重复 report 不加倍。另测 canonical 插入中途失败回滚、Gateway review 恢复 open intent 为 unknown、receipt 重放不重复计费。
- 跨范围回归覆盖 Plan、root Task 和 child grant 下的主模型与辅助模型 adapter；cost hard bound 通过真实 Core `Usage` 往返记录 metadata、超额并关闭后续准入。B2 聚焦复验：`.venv/bin/python -m pytest -q tests/test_typed_budget_dispatch.py tests/test_typed_budget_store.py tests/test_context_summary.py tests/test_context_summary_isolation.py tests/test_context_durability.py tests/test_context_wiring.py tests/test_decisions_model.py tests/test_tools_ledger_recovery.py tests/test_examples_plans.py tests/test_public_api.py tests/test_import_fence.py` → **162 passed in 8.66s**, exit 0。
- 能力边界：隐藏在未接线自定义 adapter 内的 retry 仍不受此合同保证；本轮没有网络调用、真实付费 Provider、Linux 或 Windows 复验。`typed_budget_schema_version=1` 未变，Memory/SQLite 批量预留共用同一接口。

## B3：重规划与版本化额度变更（已完成）

- Plan owner key 仍由稳定 `plan_id` 定义，不含 revision、retry、attempt 或进程身份。重规划后完成旧 Task/Run 并发布、重新批准新 revision，下一 revision 的 Task 继续落在同一 Plan owner；回归将原 Plan 限额保持为 1，在 rev1 已消费一次后，即使切到新策略版本和新 Task，新的 dispatch 仍被拒绝。
- 新增 `BudgetPolicyChangeRecord`、`update_budget_policy(...)`、`read_budget_policy_version(...)` 与 `read_budget_policy_changes(...)`。初次安装仍用 `put_budget_policy`；后续更新要求 expected digest、唯一幂等键、新策略版本、actor、authorization reference 和原因。Memory 锁与 SQLite `BEGIN IMMEDIATE` 事务都原子地写入历史/审计并移动当前策略；相同请求重放幂等、键冲突与过期 CAS 拒绝、并发 CAS 只有一个胜者。store 只保存宿主提供的 actor/authorization 数据，不负责认证；调用方必须在授权后才调用。
- 提额与降额都留下新不可变策略和审计事实。降额不删除现有 reservations、unknown 或 actual；已有 actual 超过新限额时，后续准入关闭，结算/取消路径仍可工作。策略更新不可改变 owner/session；cost 使用已有后不能改变币种。
- typed budget schema 从 v1 升为 v2，新增策略历史与审计表。显式开启 capability 的可写打开在一个事务中从完整 v1 schema 迁移并回填当前策略；损坏的 v1 policy 使 DDL、回填和版本号一并回滚。只读 v1 打开明确拒绝且不写库。SQLite 触发器注入审计插入失败也验证更新快照、历史和审计同事务回滚；旧 Task `budgets` 表和 float API 不变。
- Memory/SQLite 共用 CAS、Plan replan、降额保留 actual、成本币种拒绝回归；另有 SQLite 成功迁移、坏数据迁移原子回滚、只读拒绝以及审计写失败回滚用例。另验证旧 v1 schema 在未启用 typed budget 时不阻塞普通 Task store，也不会被隐式迁移。聚焦复验：`.venv/bin/python -m pytest -q tests/test_typed_budget_store.py tests/test_budget_policy.py tests/test_typed_budget_dispatch.py tests/test_tasks_budgets.py tests/test_plans_replanning.py tests/test_plans_store.py tests/test_plans_acceptance.py tests/test_public_api.py tests/test_import_fence.py` → **200 passed in 3.36s**, exit 0。
- 能力边界：actor 与 authorization reference 是宿主信任边界内的审计字段，不是该 store 自己验证的授权令牌；当前本地复验仍限 macOS CPython 3.12，没有付费 Provider、Linux 或 Windows 复验。

## B4：预算诊断与旧 reservation 迁移（已完成）

- 新增 `migrate_legacy_budget_reservations(root_task_id)`，仅接受显式调用、已存在的真实 Root Task 和可写 typed-budget store。SQLite 在一个事务内迁移；Memory 在锁内导入并在异常时恢复原状态。迁移按旧 reservation ID 的 SHA-256 生成稳定 dispatch identity，保留旧 `budgets` 行，重复运行只报告已迁移，不覆盖后续结算事实。
- 迁移只给 `task_root` 建 coverage，不推断 Plan 或 grant。仅精确整数且有无歧义 unit 的事实可导入；通用 `tokens`/`seconds`、缺 currency/price version 的旧成本、estimated、fractional、非法或越界值都会返回结构化跳过原因。来源 Task 的已提交 parent-child 链必须回到指定 Root Task；迁移不为旧记录伪造实际 Plan 绑定或 Run。
- Task 与 Plan 只读诊断新增同形 typed budget 视图：每个 owner/unit 报告 limit、actual、reserved、unknown、used、over-limit、可用额度及 enforcement basis；scope totals 引用唯一 canonical reservation ID，Plan 与 Task 的重叠 coverage 不会生成两笔 dispatch。旧预算记录仍作为兼容事实单独展示，不计入 typed 汇总。Task-grant owner ID 以摘要呈现。
- 没有 typed schema 的旧只读库明确返回 unsupported，CLI 不自动迁移；显式 migration API 在只读库拒绝。token/cost scope 只有 `hard_bound_ref` 存在时才报告可用的上界 enforcement；`observe` 模式明确标为 `observe_only`。
- 回归覆盖 Memory/SQLite 旧事实迁移、真实 Root Task 与 Plan coverage 隔离、重复迁移和结算后重放、estimated/fractional/歧义 unit 跳过、SQLite 中途触发器失败整批回滚、readonly refusal、Task/Plan 同一 dispatch 去重以及旧库/新版 CLI 只读兼容。`.venv/bin/python -m pytest -q tests/test_diagnostics_plans.py tests/test_diagnostics_tasks.py tests/test_typed_budget_store.py tests/test_budget_policy.py tests/test_typed_budget_dispatch.py tests/test_tasks_budgets.py` → **108 passed**，exit 0。
- 当前本地证据仍限 macOS CPython 3.12；没有在 Linux/Windows 或真实付费 Provider 上复验。typed budget schema 仍独立于 Task、Plan 和 Run schema。

## C0：有界 Step 材料模型（已完成）

- 新增并公开 `StepMaterialRef`、`StepContextManifest`、`StepMaterialDecision`、`StepContextLimits` 和对应来源、类型、信任、有效性、处置枚举。材料固定 namespace/scope、来源 ID/version/digest、授权引用、有效期，以及 inline 内容或一个 `ArtifactRef`；记录中只包含摘要与 provenance，不写入 inline 原文。
- `APPROVED_CONTROL` 只允许来自批准 Plan；accepted output 必须标明 acceptance 来源并保持 evidence；模型推断必须标记为 optional + unverified。manifest 的 predecessor 列表只能引用仍保留的 accepted output。内容边界拒绝，不静默截断；required 被丢弃、超限、过期或不可用时返回带 `network_dispatch_allowed=false` 的 `StepContextRefused` 记录。
- Artifact 的 `byte_length` 保留原始大小，`included_bytes` 只计 manifest 中 artifact reference 表示；预算映射在 manifest 创建时递归冻结，摘要不会受调用方之后的容器修改影响。
- Adapter guide 已说明 C0 值对象、C1 来源授权复查及 C2 Provider 请求接线各自的边界；材料记录不保存 inline 内容，也不赋予执行或审批权。
- 聚焦值对象、公开 API 与 import fence：`.venv/bin/python -m pytest -q tests/test_step_context.py tests/test_public_api.py tests/test_import_fence.py` → **19 passed in 0.63s**。
- 当前验证为 macOS CPython 3.12 的源码树聚焦检查，不代表跨平台、打包产物或真实 Provider 验证。

## C1：来源复查与 Step 上下文装配（已完成）

- 新增可选 `StepContextAssembler` 和宿主 `StepMaterialResolver` 协议。装配只读取当前 EXECUTING、批准仍有效的 Plan revision，以及当前 Step 的最新 admitted/active binding；required refs 必需，input refs 可选，Plan overview 需显式启用。
- 直接依赖的 accepted 输出只按显式 `predecessor_steps` 选择；必须是当前 revision 最新 accepted binding，且当前验收项全部通过并指向同一 source snapshot。仅解析 acceptance evidence refs，不会把旧 retry 或无证据 acceptance 当作输出。
- resolver 必须对每次读取及发布前复查执行真实授权，并返回匹配的版本/摘要证据。required source 漂移或失权会拒绝；optional source 的超时、失效或漂移会保留丢弃原因。artifact ref 需由 artifact store 确认存在。
- 增加每次读取超时、总装配 deadline、resolver 请求数上限和 C0 项数/字节边界；manifest schema v2 记录 `bracketed_non_atomic` 来源一致性边界、Plan control version、来源向量摘要、accepted IDs 与 omission。
- `docs/adapters.md` 已说明 resolver 的授权/限量责任、取消协作要求、C1 非原子一致性边界及 C2 Provider/恢复接线。
- 聚焦装配、值对象、公开 API 与 import fence：`.venv/bin/python -m pytest -q tests/test_step_assembler.py tests/test_step_context.py tests/test_public_api.py tests/test_import_fence.py` → **34 passed**，exit 0。
- 当前验证为 macOS CPython 3.12 源码树聚焦检查；Linux/Windows、安装制品和真实 Provider 验证尚未覆盖。

## C2：Provider 请求与恢复接线（已完成）

- 新增公开 `StepContextSelection` 和 `StepContextRuntime`，并将它接入 `DefaultHarness` 的既有准备回调。绑定读取从 Plan reader 按 `task_id` 查询，不要求把 Plan 读取能力塞进 Task inbox/control store；装配默认不选前序输出或 Plan overview，只有宿主显式选择时才解析可选来源。
- runtime 把 manifest 材料渲染成带 reference-data notice 的 `user` 消息；最终 transcript 项是 user prompt 时放在它前面，其余情况附在完整 transcript round-trip 后，避免必需材料位于上下文开头而阻止旧轮次投影，也不拆分工具调用/结果。required 消息作为 `extra_mandatory` 交给 `ContextController`，摘要 compactor 保留所有宿主指定的消息；required 不适配、超出 token 容量或真实测量拒绝时，运行状态记为 `usage_limited` 且 Provider 请求为零。
- `rendered_context_digest` 对应实际送入 request view 的完整材料消息字节（包括消息前缀和 JSON envelope）。对可选材料，渲染字节上限及完整请求投影各自记录 `render_byte_limit` / `request_context_budget` 等丢弃原因；`max_render_bytes` 不替代全请求 token 估算或最终 serialized payload gate。
- manifest digest 与脱敏 manifest record 写入 `RunRecord.context_state.step_context`；实际请求 view 在 transcript 外保存。该关联经现有 `RequestPrepared → state_sink` 在发送 Provider 请求之前提交，提交失败注入测试确认 loopback 请求数为 0。真实 wire 回归对照同一转换得到的 system、工具声明、消息、输出 cap、材料记录和 digest；原 transcript 不含合成 Step 材料。
- `DefaultHarness` 在每个新模型请求（包括恢复后的请求）重新读取当前 binding 并装配来源。原 `context_state` 仍作为 prior 传给 ContextController，因此 summary usage/allowance 不因重新装配而重置。artifact material 只下发 reference；读取原文需宿主单独注册有 scope 的 READ-effect 工具。manifest 仍不授予工具、审批或验收权。
- 新回归：`tests/test_step_context_runtime.py::test_plan_material_manifest_is_committed_and_the_real_wire_body_contains_it`、`::test_required_plan_material_that_cannot_fit_refuses_before_provider_dispatch`、`::test_optional_plan_overview_drop_is_measured_and_recorded_against_the_wire_body`、`::test_failed_manifest_association_commit_dispatches_no_provider_request`、`::test_step_context_recovery_reassembles_without_refreshing_summary_allowance`。聚焦命令见[验证记录](verification.md#harness-next-stage-c2-provider-request-and-recovery-wiring)：**90 passed in 9.45s**，exit 0。
- 当前证据是 macOS CPython 3.12 的源码树测试和 loopback HTTP transport；未调用真实付费 Provider，也未重复 Linux/Windows 或 wheel/sdist 完整制品门禁。

## C3：三步骤版本化上下文案例（已完成）

- 新增 examples/20_step_context.py：真实 SQLite Plan/Task/Run 存储、LocalWorker、DefaultHarness、StepContextRuntime、host-owned versioned resolver 和 AcceptanceCoordinator 串联“读取资料 → 生成候选变更 → 宿主 verifier 核验”。第三步只选择直接前置的候选输出，并读取自身声明的当前源文件引用；不接收第一步接受输出，也不启用 Plan overview。
- 案例在子进程提交第一步 Run 后主动退出；父进程重开 SQLite 并结算同一 Task/Run，再完成余下两个步骤。实际 Provider 调用共三次，恢复没有重发已提交的第一步请求。第三步运行记录带 manifest，案例从收到的 ProviderRequest 重新计算 rendered_context_digest 并逐项检查实际材料。
- 测试注入必要源改版、工件不存在、复查前撤权、超大输出、跨 scope 返回和解析失败；每种情况都在 Provider 调用前拒绝，真实 loopback request 数为零，已存原 transcript 不含合成材料。恶意材料文本只出现在 user/reference-data 消息，system 提示保持原样。这个 C3 runtime 组同时覆盖 NX35 的解析失败历史保全与跨 scope 隔离。
- 聚焦命令：.venv/bin/python -m pytest -q tests/test_step_context_runtime.py tests/test_examples_plans.py → **19 passed in 8.32s**，exit 0；其中包含三步骤 SQLite/跨进程恢复示例。
- 当前证据限 macOS CPython 3.12 源码树和 loopback Provider；未调用付费模型，未完成 Linux/Windows 或 wheel/sdist 完整制品复验。

## O0：多来源只读时间线合同（已完成）

- 新增 `diagnostics/timeline.py`，定义 `TimelineReadPort`、whitelist fact、source page、coverage gap、固定水位和每来源游标向量。来源必须声明 `read_only=True`；每个来源负责其自己的权威数据和水位，Timeline 不写入或复制到 Plan store。
- 续页沿用首读捕获的水位。sequence 来源按本地序号校验并保持顺序；无序号来源以稳定 reference 展示并加 `sequence_unavailable`。来源页可以返回已扫描游标以跨过无关行；全局页只在已返回的来源事实都被输出后应用该 checkpoint，避免跳过事实。
- 明确因果前驱与一般关联的区别：只有 `causal_references` 建立排序边，`related_references` 只用于关联；并列顺序按 source ID、本地 sequence、reference，`display_at` 仅显示。来源缺失、分页截断、扫描触限、关系缺失、重复 reference、无序号、损坏页、非原子页和因果环均可见。
- 基于目前的存储契约，operation ledger `all_operations()` 没有本地 sequence 或可复用的追加事件游标；适配器必须报告无序号或水位缺失，不能把 `intent_at` 伪装成因果顺序。各宿主存储通过可选端口提供 adapter；不承诺跨库原子快照。
- 新增 `PlanInspector.inspect_linked_timeline(...)` 可选 facade；缺省不配置时明确报 `ConfigurationError`，现有 Inspector 路径不增加执行能力。
- O0 定向合同覆盖固定水位多页续读无跳项、跨过无关游标、因果顺序、重复引用/缺失关系/截断/不可读/无序号、写连接拒绝及 JSON/Markdown 安全输出。

## O1：只读指标、脱敏输出与根因（已完成）

- Timeline 由同一 whitelisted report 生成 JSON 和 Markdown；摘要经过既有诊断脱敏，Markdown 使用既有 escape。输出不持有 prompt、凭据、原始环境或完整证据载荷。
- 支持 accepted/rejected/inconclusive、人工决定、ready→admission、恢复和 unknown 时长、预算用量覆盖、重试等指标。持续时间必须由来源提供相同 clock domain 的已记录端点；缺失值保留 unknown，不从 wall-clock 显示字段推算。
- `budget_usage` 有值时必须携带 canonical reservation dedupe key；Plan/Task 对同一 reservation 的重复观测只汇总一次，冲突金额的 aggregate 标为 partial 且不输出合计。
- 确定性根因支持 approval、dependency、deployment_drift、budget、context、provider、tool、operation_unknown、storage_conflict、acceptance、cancellation，且保留来源 reference、标记 `inferred=false`。模型质量与计划合理性不由事实层推断。

## O2：六类冻结离线时间线评测（已完成）

- 在既有 `EvalCase`/`EvalSuite`/`VerifierOutcome` 体系旁增加 `TIMELINE_SUITE` 和确定性 fixture runner，使用独立 `timeline-protocol` verifier。冻结用例包括正常多 Step、最后额度竞争、审批后资源漂移、部分副作用崩溃、陈旧依赖上下文、验收拒绝后重规划；每个 case digest 包含 fixture、policy、预算、故障位置、初始状态及成功判据。
- 离线 case 执行六个 source fixture，通过同一只读 Timeline reader 生成报告；未调用 Provider，`provider_requests=0`。切换成非 offline model 的 case 返回 `not_run`，不会以固定 Provider 替代真实模型任务。
- 冻结 suite digest：`sha256:371806f3ce3c52824930c1e323dc902c65494242a9552f269e82e7a478387699`。六例独立 verifier 全部通过；负向控制确认缺少必需事实会判 `failed`，而 verifier/schema 失效归 `infra_error`，未配置真实模型归 `not_run`。
- 评测衡量诊断关系与恢复协议事实，不度量模型任务质量，也不声称 fixture runner 重放了真实外部副作用。

O0–O2 设计、字段和宿主接入边界见[关联诊断时间线指南](diagnostics-timeline.zh-CN.md)，交付摘要见[O0–O2 交付报告](harness-next-stage-o0-o2-delivery-report.zh-CN.md)。

## R3：派发/取消补充修复后的当前候选复验（2026-10-29）

本次使用隔离 CPython 3.12.13 环境和预先准备的 wheelhouse，对已修复工作树执行 `scripts/ci_check.py --offline` 六步候选门禁。命令 exit 0，所有步骤通过：离线安装 `dev,yaml,pydantic` extras、完整源码套件、cold import fence、wheel/sdist 构建与源码树外安装测试/示例、相对 Markdown 链接和来源文件检查。

| Python / 平台 | 完整源码套件 | 六步离线 CI | 记录 |
| --- | --- | --- | --- |
| 3.12.13 / macOS 27.2 arm64 | **1796 passed, 22 skipped** | **6 steps; failed: none** | wheel/sdist 树外安装、测试与示例通过。命令和隔离 wheelhouse 见[验证记录](verification.md)。 |

这次结果不替代对当前源码的其他解释器/操作系统运行：Python 3.10、3.11 以及 Linux、Windows 当前不可用。此前 3.10/3.11/3.12 的成功矩阵属于更早源码快照；历史矩阵保留供追溯，不用于证明这次修复。

## 阶段门禁

| 阶段 | 状态 | 依据 / 缺口 |
| --- | --- | --- |
| R0 | 已记录 | HEAD、分支、解释器、schema、现有工作树归属和文档状态冲突见上文。 |
| R1 | 已完成 | 两条初始失败的触发时点已归因并修正；修复了 stop-hook 和请求前取消边界；真实 Provider/工具结果保留且取消不触发新派发。完整 Core、相邻回归和最终快照/CI 证据见上文及验证记录。 |
| R2 | 已完成 | Plan/DAG 原范围与预算/Gateway 补充独立复审均无剩余发现；用户报告的四项问题和后续取消清账边界已修复并通过 182 项聚焦集。principal 信任、验收读取 TOCTOU 与已 claim 工作撤销边界仍按上文声明。 |
| R3 | 已完成（平台缺口已登记） | 当前修复源码在 macOS CPython 3.12.13 完整套件和六步离线 CI 通过；wheel/sdist 树外安装、测试和示例均通过。当前源码的 Python 3.10/3.11、Linux/Windows 缺口保留；旧矩阵不冒充本候选证据。 |
| E0 | 已完成 | 不可变值对象、结构化拒绝、摘要与确定性 binding ID 已冻结；Python 3.12/3.10 聚焦合同/API 检查通过。 |
| E1 | 已完成 | Memory/SQLite 原子准入、v3 schema/migrations、Worker/DefaultHarness/Gateway 派发防护与恢复回归通过；聚焦集 354 passed。 |
| E2 | 已完成 | 版本化 preview digest、删除漂移、target/tool/policy 漂移的真实 Gateway 处理器门禁通过；聚焦集 85 passed / 11 skipped。 |
| E3 | 已完成 | 新参考示例展示独立旧 Run、普通 Task、绑定 Plan Task、真实 SQLite/Gateway、进程退出与漂移拒绝；恢复原 profile/workspace 后完成同一 Run 与 Plan；能力缺口明确。 |
| B0 | 已完成 | owner identity、typed policy snapshot、units、hard/observe 语义和 legacy conversion 已冻结并公开；36 个类型/Task-budget/API 用例通过。 |
| B1 | 已完成 | Memory/SQLite typed store 原子覆盖 Task root、Plan 与父子 grant；多进程最后额度、部分写入回滚、重开恢复和 cost scope 检查通过。真实 dispatch 尚待 B2。 |
| B2 | 已完成 | `TypedDispatchBudget` 覆盖主模型、显式 Provider retry、摘要、决策与 Gateway；hard usage、unknown/迟到 usage、overage、跨 scope、跨进程故障窗口、dispatch 阶段计费、策略摘要竞争、绑定复查及取消/receipt replay 清账已由 182 个聚焦用例覆盖。自定义 adapter 必须显式接入 retry 门禁。 |
| B3 | 已完成 | 版本化 CAS/审计、Plan 跨 revision 共用总账户、降额保留 usage、SQLite v1→v2 原子迁移及 opt-in 兼容性均有回归；聚焦集 200 passed。 |
| B4 | 已完成 | 旧 Task budget 仅迁移到真实 Task root；SQLite 原子回滚、readonly refusal、Plan/Task canonical dispatch 去重、旧 schema unsupported 和 token/cost 上界能力报告均通过；聚焦集 108 passed。 |
| C0 | 已完成 | 有界材料/manifest 类型、授权来源标记、内容隐藏、required refusal、accepted predecessor 引用、artifact 字节计量及公开 API/import fence 回归通过；聚焦集 19 passed。 |
| C1 | 已完成 | 当前 Plan/binding/acceptance 读取、显式依赖选择、resolver 授权和版本复查、artifact 存在性、required fail-closed、optional omission、来源向量及总 deadline 通过；聚焦集 34 passed。 |
| C2 | 已完成 | DefaultHarness 在请求前关联并提交 manifest；required 超限和提交失败零 Provider 派发；可选材料丢弃原因、真实 wire body/digest/测量输入、原 transcript 不变和 prior summary allowance 均在 90 项聚焦集覆盖。 |
| C3 | 已完成 | 三步骤 SQLite 案例跨进程恢复同一 Run；第三步 ProviderRequest 与 manifest digest 一致；来源改版、工件缺失、撤权、超限、跨 scope 和解析失败均零派发，恶意文本不进入 system；19 项聚焦组通过。 |
| O0 | 已完成 | 固定水位/来源游标合同、因果排序、无序号显式缺口、只读接线与重复/关系/截断/不可读覆盖已实现。 |
| O1 | 已完成 | whitelisted JSON/Markdown、measurement coverage、canonical budget dedupe、同 clock duration 和确定性根因实现。 |
| O2 | 已完成 | 六例 frozen suite digest 和独立 verifier 完成；offline case 通过，真实模型路径保持 not_run。 |

S0 是按容量需求启动的阶段，目前没有来自实际负载的容量瓶颈证据，因此尚未启动，NX42 未触发。R3 wheel/sdist 仅保留在对应 `/tmp` CI 证据目录；工作区中原有 P0.6 改动与忽略的 build 工件仍保留。本轮未提交或发布。
