# Documentation / 文档索引

Current branch / 当前分支：**`0.4.0a1` · local alpha candidate**。见[候选版本记录](release-0.4.0a1.zh-CN.md)；AC38（Python 3.10）仍 BLOCKED。

## Start here / 从这里开始

| Goal / 目标 | Read / 入口 |
| --- | --- |
| Install and run the first offline demo / 安装并运行离线演示 | [English README](../README.md#quickstart) · [中文 README](../README.zh-CN.md#快速开始) |
| Embed the harness / 接入应用 | [Public API contract](contract.md) · [Adapter guide](adapters.md) |
| Understand approvals and recovery / 理解审批与恢复 | [Recovery semantics](recovery.md) · [Approved-edit example](../examples/05_durable_approved_edit.py) · [Partial-batch recovery](../examples/07_partial_batch_recovery.py) |
| Use persistent tasks / 使用持久任务 | [任务、输入、预算与父子任务契约](tasks-contract.zh-CN.md) · [输入重投](../examples/08_durable_input_redelivery.py) · [调度](../examples/09_fair_task_scheduling.py) · [父子恢复](../examples/10_parent_child_recovery.py) |
| Connect MCP tools / 接入 MCP 工具 | [MCP Client 使用指南](mcp-client.zh-CN.md) · [stdio 示例](../examples/11_mcp_stdio.py) · [HTTP 恢复示例](../examples/12_mcp_http_recovery.py) |
| Assemble extension packs / 装配扩展包 | [扩展开发指南](extensions.zh-CN.md) · [ADR](extensions-adr.zh-CN.md) · [tools/skills 示例](../examples/13_extension_tools_skills.py) · [MCP 恢复示例](../examples/14_extension_mcp_recovery.py) |
| Inspect runs and tasks / 查看运行与任务 | [诊断事实说明](diagnostics-facts.zh-CN.md) · [运行报告](samples/partial-recovery-report.md) · [任务报告](samples/task-diagnostics.md) · [离线评测](samples/evals/README.md) |

Most integration contracts are in English. Detailed architecture, MCP and task guides are currently in Chinese.
多数接入契约使用英文；详细架构、MCP 与任务指南目前使用中文。

## Current architecture and support / 当前架构与支持

- [当前 Harness 架构](current-harness-architecture.zh-CN.md)与[Core 执行原理](core-execution-current.zh-CN.md)：执行路径、提交边界与恢复。
- [0.3.0a1 发行与宿主采用记录](release-0.3.0a1.zh-CN.md)：发行源码基线、产物摘要、实际验证及限制。
- [0.4.0a1 分支候选记录](release-0.4.0a1.zh-CN.md)：扩展装配、复核结论及新版本验证。
- [Verification history](verification.md)：按版本、解释器与故障窗口记录的证据；历史通过数字不自动认证当前版本。
- [Supported environments and help](../SUPPORT.md)、[Contributing](../CONTRIBUTING.md)、[Security policy](../SECURITY.md)。
- [公开源码整理与敏感内容检查](public-source-audit.zh-CN.md)：2026-10-20 的检查范围、发现与日志可用性。

## Design history / 设计与实施记录

以下文档保留原始计划、失败复现和修复证据，供贡献者追溯。**它们的历史状态、旧版本号与待办不替代当前契约和发行记录。**

| Area / 领域 | Plan / 设计 | Review and delivery / 审查与交付 |
| --- | --- | --- |
| Core 与提交边界 | [状态机实施计划](core-state-machine-implementation-plan.zh-CN.md) · [持久提交修复](harness-durable-commit-fix-plan.zh-CN.md) | [迁移记录](core-state-machine-migration.zh-CN.md) · [验证记录](verification.md) |
| 上下文工程 | [实施计划](context-engineering-implementation-plan.zh-CN.md) | [验收记录](context-engineering-acceptance-review.zh-CN.md) · [准备基线](context-preparation-baseline.zh-CN.md) |
| 工具恢复 | [逐工具结算计划](per-tool-settlement-recovery-implementation-plan.zh-CN.md) | [独立验收](per-tool-settlement-recovery-acceptance-review.zh-CN.md) |
| 诊断与评测 | [实施计划](runtime-diagnostics-evaluation-implementation-plan.zh-CN.md) | [独立验收](runtime-diagnostics-evaluation-acceptance-review.zh-CN.md) · [交付报告](runtime-diagnostics-evaluation-delivery-report.zh-CN.md) |
| 持久任务 | [实施计划](durable-input-orchestration-implementation-plan.zh-CN.md) | [交付报告](durable-input-orchestration-delivery-report.zh-CN.md) · [发行修复与复验记录](durable-input-orchestration-release-fix-report.zh-CN.md) · [后续独立验收](durable-input-orchestration-rf-acceptance-review.zh-CN.md) |
| MCP | [SDK 基线](mcp-client-sdk-baseline.zh-CN.md) · [实施计划](mcp-client-implementation-plan.zh-CN.md) | [独立复核](mcp-client-independent-review.zh-CN.md) · [交付报告](mcp-client-delivery-report.zh-CN.md) |
| 扩展装配 | [E0–E3 方案](extensions-implementation-plan.zh-CN.md) · [开发指南](extensions.zh-CN.md) | [ADR](extensions-adr.zh-CN.md) · [交付报告](extensions-delivery-report.zh-CN.md) · [独立复核](extensions-independent-review.zh-CN.md) |

[演进路线图](harness-evolution-roadmap.zh-CN.md)顶部汇总当前交付状态，正文保留阶段设计。
