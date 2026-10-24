# Plan 与任务 DAG 实施进度

状态：进行中。范围为方案 `docs/plan-dag-harness-implementation-plan.zh-CN.md` 的 P0.0–P0.6；不包含 P1/P2。此文记录实际观测结果，不把计划目标当作通过证据。

## 开工基线

| 项目 | 记录 |
| --- | --- |
| 基线提交 | `8b92d50b2664431ee62d1f2b94a24254484d33da` |
| 工作分支 | `main`（开工时） |
| Python / OS | Python 3.12.13；Darwin kernel 27.0.0 arm64 |
| 包版本 / 最低版本 | `0.4.0a1`；`requires-python >=3.10` |
| 可选依赖 | `.venv` 中 pytest、pytest-asyncio、pydantic、yaml 已安装；MCP SDK 与 jsonschema 未安装 |
| 开工前工作区 | 用户已有 `M docs/README.md` 与未跟踪的方案文档 `docs/plan-dag-harness-implementation-plan.zh-CN.md`；保留，不纳入本轮改动或提交 |
| 相关测试基线 | `.venv/bin/python -m pytest -q tests/test_tasks_contract.py tests/test_tasks_scheduler.py tests/test_tasks_inbox.py tests/test_tasks_acceptance_matrix.py` → **86 passed in 1.92s** |

仓库根目录原本没有 `AGENTS.md`/`RTK.md`。本轮按用户要求新增：`AGENTS.md` 提供 CodeGraph 规则，`RTK.md` 记录项目边界与兼容要求。

## 阶段状态

| 阶段 | 状态 | 结果 / 验证 |
| --- | --- | --- |
| P0.0 契约冻结 | 已完成 | 冻结数据模型、规范摘要、审批绑定、状态迁移、拒绝码、DAG/额度、原子准入、验收及静止重规划边界，见 `docs/plans-contract.zh-CN.md`。实现阶段发现的偏差必须回写本契约后再继续。 |
| P0.1 纯模型与 DAG | 未开始 | 尚无新运行时类型或测试。 |
| P0.2 控制存储 | 未开始 | 尚无 Plan schema/存储端口/迁移。 |
| P0.3 调度接线 | 未开始 | 尚未接入 LocalWorker。 |
| P0.4 验收与完成 | 未开始 | 尚无 verifier、reconcile 或 finalize。 |
| P0.5 暂停/重规划/恢复 | 未开始 | 尚无新控制操作或跨进程故障验证。 |
| P0.6 诊断/示例/交付 | 未开始 | 尚无 PlanInspector、示例、完整 PA 映射或树外安装验证。 |

## 变更与偏离

- 首轮保守冻结 Plan 的动态 token/cost/dispatch 共享预算为不支持；只保证文档列出的结构、次数、deadline、并发与现有 Task 预算。没有虚构不存在的 root Task。
- 阶段提交尚未创建；在每阶段实现与验证形成可审查文件集合后按方案单独提交，不包含开工前的用户改动。

## 阶段验证记录

阶段验证命令、结果、失败和未测平台追加在对应阶段完成时。首轮 P0 完成前不得将总体状态改为完成，也不得删除失败/未测记录。
