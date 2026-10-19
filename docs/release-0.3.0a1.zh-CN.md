# 0.3.0a1 本地发行与 Yanxu 采用记录

日期：2026-10-20（Asia/Shanghai）。本轮由作者授权完成修复提交、升版、正式产物验证、
合并 main 和 Yanxu 固定 wheel 验证。分发名为 `yanxu-harness`，导入名为 `yanxu_harness`。
这是本地 Alpha 发行；不上传包索引，不推送发行 tag，不调用付费模型。

## 来源与范围

R5 修复与独立复核记录提交：`a636523`。版本从 `0.2.0a1` 升为 `0.3.0a1`。
新增可选 MCP Tools 客户端（stdio、Streamable HTTP）、显式工具效果结果和可选派发守卫。
基础安装保持标准库依赖；公共 API 与包元数据同步升版，已有持久化格式版本保持独立。

历史审查、失败复现和修复前后测试证据保留原版本号，见
[独立复核](mcp-client-independent-review.zh-CN.md) 与 [交付报告](mcp-client-delivery-report.zh-CN.md)。

## 构建与门禁

状态：**通过**。以干净提交 `6f16551502a87179dc3e69f77a97fbd8a00daaff` 构建。
交付目录为 `build/release-0.3.0a1/artifacts/`，包含 wheel、sdist、`SHA256SUMS`、
`source.json`、安装门禁报告及 `verification-report.md`。取同一次构建的产物验证与交付。

| 产物 | bytes | SHA256 |
| --- | --- | --- |
| `yanxu_harness-0.3.0a1-py3-none-any.whl` | 558839 | `cc7865176ada237f6a414d657c386e5e14397bddb8c549c252cd21d7823b84e5` |
| `yanxu_harness-0.3.0a1.tar.gz` | 1300461 | `270462e3652823b6226c71044bd7ab06422e40fed8574bc784d072d4e14ff253` |

命令：`.venv-mcp/bin/python scripts/mcp_gate.py --workdir build/release-0.3.0a1 --report build/release-0.3.0a1/mcp-gate-report.json`。

| 检查 | 结果 |
| --- | --- |
| 基础 wheel，无 SDK | 安装、106 个子模块冷导入无可选依赖泄漏、缺 extra 明确提示、基础示例全部通过 |
| wheel 与 sdist 的 MCP 专项 | 各 **92 passed / 0 skipped** |
| wheel 与 sdist 的树外全套 | 各 **1362 passed / 36 skipped / 0 failed** |
| 原 8 场景探针、两个 MCP 示例 | 两种产物全部通过 |
| 已安装正式 wheel 的独立 R5 探针 | **通过**，Run 保持暂停，远端与后续模型调用为 0 |
| 版本/API/清单检查；导入隔离/静态 Python 下界 | **20 passed**；**16 passed** |

树外全套的跳过为安装布局缺少检出目录的门禁，MCP 专项未跳过。

## Yanxu 采用

状态：待运行。固定 `vendor/harness/` 的新 wheel，并同步宿主版本、digest、源码提交和
依赖声明。宿主已有工作区改动保留；验证采用临时测试数据，不改真实 vault。
本轮固定基础 wheel 并验证宿主现有合同和恢复行为；宿主开启 MCP 服务的产品配置另行实施。

## 验证边界

本机 Python 3.12 / macOS arm64。Python 3.10 实跑、远程 CI、其他平台和真实付费模型
仍没有本轮证据；历史 Yanxu 真实模型测试不改记成本版本的测试结果。
