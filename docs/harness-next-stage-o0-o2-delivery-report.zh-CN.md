# Harness 下一阶段 O0–O2 交付报告

日期：2026-10-29。源码版本仍为 `0.4.0a1`，工作分支为 `codex/plan-dag-harness`。本报告记录
源码树交付，不代表已发布发行制品，也不覆盖同一工作树中实施前已有的 P0.6 与用户文档改动。

## 实施结果

- O0 新增 `yanxu_harness.diagnostics.TimelineInspector` 与 `TimelineReadPort` 合同。每个 authority
  source 自己读水位和 cursor；多来源 cursor vector 固定首读水位。sequence 来源校验本地顺序，
  unsequenced source 明确输出 coverage gap。跨来源只有 explicit causal reference 建立前后；稳定
  tie-break 不使用 wall clock。source 缺失、截断、扫描触限、缺失关系、重复 reference、损坏页、
  非原子读取和因果环均在报告中可见。
- O1 新增同一 Timeline report 的 JSON/Markdown 输出、PlanInspector 的可选关联读方法、显式指标
  coverage 与 deterministic root cause facts。duration 需要相同 clock domain 的记录端点；budget usage
  使用 canonical reservation dedupe key；冲突金额不输出合计。已有 Prompt、凭据、原始环境及完整证据
  载荷不属于 Timeline fact schema。
- O2 新增六个基于 `EvalCase`/`EvalSuite` 的冻结离线场景和 `timeline-protocol` independent verifier：
  正常多 Step、最后额度竞争、审批后资源漂移、部分副作用崩溃、陈旧依赖上下文、拒绝后重规划。Suite
  digest 固定为 `sha256:371806f3ce3c52824930c1e323dc902c65494242a9552f269e82e7a478387699`。这些 fixtures
  证明事实投影和 verifier 行为，不会执行 Provider，也不度量模型能力；非 offline model 返回 `not_run`。
- 操作账本当前 `all_operations()` 没有事件 sequence/追加游标；关联 adapter 必须表达该限制，不能用
  `intent_at` 冒充因果序号。各宿主为自己的 Plan、Task、Run、operation、approval 和 acceptance 存储实现
  read-only source port；此交付没有新增 store、schema、迁移、执行入口或跨库原子性承诺。
- S0 按实施计划只在真实负载表明容量需求时启动；当前没有此类证据，NX42 未触发。

## 验证

环境为 macOS arm64、CPython 3.12.13、仓库 `.venv`。完整 O0–O2 定向组及三步骤 Plan 示例：

```sh
.venv/bin/python -m pytest -q \
  tests/test_diagnostics_timeline.py tests/test_diagnostics_plans.py \
  tests/test_diagnostics_tasks.py tests/test_diagnostics_reader.py \
  tests/test_diagnostics_types.py tests/test_evals_timeline.py \
  tests/test_evals_offline.py tests/test_examples_plans.py
```

结果：**123 passed in 35.84s**，退出码 0。覆盖 fixed-watermark cursor reread、不跨未输出事实推进、因果排序、wall-clock 非排序、coverage gaps、写来源拒绝、脱敏、指标/根因、预算去重、case digest、负向 verifier、非 offline `not_run`。

## 限制与下一步

尚未为任意宿主数据库提供预制 adapter；source 实现须遵守 `TimelineReadPort` 的只读和固定水位合同。
未验证跨数据库原子快照、任意外部 effect 的真实 crash replay、Linux/Windows、wheel/sdist 安装和真实模型
任务质量。本轮未配置或调用真实 Provider。跨平台和安装制品仍是既有 R3 环境缺口的后续复验项。

没有创建仓库提交或发布；工作树仍包含用户实施前的改动，交付保持在当前未提交源码树中供审阅。
