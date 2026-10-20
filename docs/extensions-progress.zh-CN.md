# Harness 扩展 E0–E3：阶段进度与证据

日期：2026-10-20。状态：**方案准备完成，实施尚未开始**。
主方案：[E0–E3](extensions-implementation-plan.zh-CN.md)。交接：[DeepSeek](extensions-deepseek-handoff.zh-CN.md)。

## 编制基线

- 源码：`9aed198872916c9b3d1496a4647ed24875364ef4`，版本 `0.3.0a1`。
- 编制从干净 main 开始；计划工作在 `codex/extensions-plan`，只改文档与生成清单。
- 已核对 ToolDefinition/ToolRegistry、SkillProvider、MCP manager/adapter、DefaultHarness 的指纹/恢复比较和 guard 接线。
- 本次没有实现扩展运行时、运行扩展功能测试或调用外部模型。
- 方案编制的链接/验收编号/清单检查在交付时记录；旧 MCP 数字不计作扩展验收通过。

方案静态核对：四份文档的 **40 个本地链接**全部可解析；代码围栏成对；EXT-AC01–40
连续且唯一，E0 的 01–08、E1 的 09–18、E2 的 19–31、E3 的 32–40 分段一致。
本次 `git diff --check` 通过。以下阶段状态仍全部待实施。

文档交付检查：`.venv/bin/python -m pytest -q tests/test_public_api.py tests/test_build_scripts.py
tests/test_public_snapshot_whitelist.py` → **20 passed**。这是既有公开 API、构建与文档清单检查，
不计入 EXT-AC 的功能实现证据。`SNAPSHOT.json` 用导出脚本生成。

## 阶段状态

| 阶段 | 当前状态 | 退出范围 |
| --- | --- | --- |
| E0 清单与绑定合同 | 待实施 | EXT-AC01–08 |
| E1 原子能力装配 | 待实施 | EXT-AC09–18 |
| E2 生命周期与真实恢复 | 待实施 | EXT-AC19–31 |
| E3 示例、文档与发行门禁 | 待实施 | EXT-AC32–40 |

## 实施记录格式

每阶段追加以下内容，保留失败历史，不把“代码已写”改成“验收通过”：

1. 接手时的 HEAD、工作树、包版本、Python/SDK 环境与运行时导入来源。
2. 实际修改和契约决策，特别是相对方案调整的 ADR。
3. RED 节点、实际命令、失败原因和退出码；随后 GREEN 的同一行为断言。
4. 影响面回归与安装来源、资源/副作用计数、实际故障窗口。
5. 逐 AC 的 PASS/FAIL/BLOCKED/NOT_RUN 状态和证据位置；skip 单列。
6. 本阶段提交、剩余缺口与下一阶段前置条件。

## 交付前必须保留的边界

实施保留 `0.3.0a1`，复核后再决定 `0.4.0a1`。宿主集成与真实模型既有证据仍属于原版本/原环境。
Python 3.10 没有实跑就不得将 AC38 写成通过；E3 状态需与其必需退出条件一致。
本地 wheel/sdist 门禁产物是验证候选，不标为已发布到索引或已被宿主采用。
