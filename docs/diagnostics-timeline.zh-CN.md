# 关联诊断时间线

`yanxu_harness.diagnostics.TimelineInspector` 将 Plan、Task、Run、审批、验收和 operation
等各自拥有的事实组合成只读视图。它不另建事实库，不驱动 Worker、Provider、Gateway 或
reconciler，也不把来源事件复制进 Plan 控制库。

## 读取端口与水位

每个来源实现 `TimelineReadPort`，声明稳定的 `source_id`、`read_only=True` 和是否具有本地
sequence 顺序，并实现 `capture_watermark(identity)` 与 `read_page(identity, cursor, watermark,
limit)`。端口必须遵守传入的固定水位；Timeline 返回的 `TimelineCursor` 保存每个来源原来的
水位和游标，续读时不得重新捕获已有水位。

```python
from yanxu_harness.diagnostics import PlanInspector, TimelineInspector

timeline = TimelineInspector((plan_events, task_inputs, run_events, operation_receipts))
inspector = PlanInspector(
    control=readonly_plan_store,
    linked_timeline=timeline,
)
page = await inspector.inspect_linked_timeline(
    {"plan": plan_identity, "task": task_identity, "run": run_identity,
     "operation": operation_identity},
    limit=100,
)
```

调用方按来源传入身份；来源端负责再次验证 namespace、session、Plan/Task/Run 关联。事实只
暴露白名单字段：source/reference、kind/status、sequence、关联引用、短摘要和仅供显示的
`display_at`。因果前驱必须通过 `causal_references` 明确表达；`related_references` 只建立
关联，不声称先后。页面按本地 sequence 和因果前驱排序；没有因果约束时用 source ID、
本地 sequence、reference 作稳定并列规则。墙钟不会参与排序。

来源不可读、页面截断、扫描触限、关系缺失、重复 reference、来源缺少 sequence、页面损坏、
非原子读取和因果环都会返回 `CoverageGap`。没有 durable sequence 的来源仍可以报告事实，
但会标记 `sequence_unavailable` 并按稳定 reference 显示；这不表示跨库因果顺序。当前
SQLite operation ledger 的 `all_operations()` 是 receipt 状态快照，没有本地 sequence，因此
实现 adapter 时不能把 `intent_at` 当作因果序号。来源没有固定水位能力时，应报告缺口，不得
假称有可续读的一致边界。

这是可选读端口合同；宿主按自己的实际 Plan、Task、Run 与 operation 存储提供来源 adapter。
本包不假定这些库位于同一 SQLite 文件，也不承诺跨库原子快照。

## 指标和根因

`TimelineFact.metrics` 接受显式 `MetricObservation`。输出会分别报告观测数、已知数、未知数和
覆盖状态。ready→admission、恢复和 unknown 持续时间只接受来源记录了相同 clock domain 的
端点；`display_at` 是墙钟显示字段，不会推导 duration。已知 `budget_usage` 必须携带 canonical
reservation `dedupe_key`；多 scope 重复引用同一 reservation 只汇总一次，值冲突时总量标为
未知并报告 partial coverage。

acceptance 的 accepted/rejected/inconclusive 与 approval decision 次数可由明确事实计数。重试、
预算用量及其覆盖率由来源提供。根因只接受冻结类别：approval、dependency、deployment_drift、
budget、context、provider、tool、operation_unknown、storage_conflict、acceptance、cancellation。
输出带来源 ID/reference，全部标为 `inferred=false`。模型质量或计划合理性不由这个视图推断。

JSON 是稳定事实记录；Markdown 从同一报告生成，并复用诊断脱敏与 Markdown 转义。`PlanInspector`
只在显式配置 `linked_timeline` 时提供 `inspect_linked_timeline`；默认行为、现有 Plan/Task/Run
诊断和执行入口保持独立。

## 冻结离线评测

`yanxu_harness.evals.TIMELINE_SUITE` 有六个带 fixture、policy、budget、fault position、initial
state、expectations、case digest 和独立 verifier 的离线用例。运行 `run_timeline_suite()` 可得到
通用 `EvalSuiteResult`：

1. 正常多 Step 关联；
2. 两个 claimant 争用最后额度；
3. 批准后资源漂移；
4. 部分副作用后进程崩溃；
5. 依赖上下文陈旧；
6. 验收拒绝后的重新规划。

这些固定 source fixtures 验证只读时间线协议、关系、coverage、分类与 verifier；它们不执行真实
Provider 请求，也不度量模型任务质量。把 case 改成真实模型时，当前离线运行器返回 `not_run`；
只有配置 Provider、模型、重复次数和预算并得到显式运行授权后，才能报告真实模型结果。

验证命令：

```sh
.venv/bin/python -m pytest -q tests/test_diagnostics_timeline.py tests/test_evals_timeline.py
```
