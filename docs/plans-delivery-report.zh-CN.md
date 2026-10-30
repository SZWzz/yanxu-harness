# Plan 与任务 DAG P0.6 交付报告

状态：P0.6 实施完成；完整门禁有 2 条未修改的 Core 取消测试失败。本报告不代表已发布包或独立审查结论。

## 交付内容

- 只读 `PlanInspector` API、JSON/Markdown 渲染和 `python -m yanxu_harness.diagnostics plan` CLI。CLI 不创建/迁移数据库并校验 namespace/session；诊断输出不包含 Plan goal/instruction、验收 evidence payload 或工具操作 payload。
- 两个无网络离线例子：候选解析、可信审批、DAG 执行、Gateway/SQLite 回执和跨进程恢复。例子详见 [`plans-integration.zh-CN.md`](plans-integration.zh-CN.md)。
- PA01–PA40 映射及实际状态：[`plans-acceptance-review.zh-CN.md`](plans-acceptance-review.zh-CN.md)。
- 公开契约、适配器接入、分层迁移和文档索引已更新。

## 验证环境

| 项目 | 观测值 |
| --- | --- |
| 操作系统 / 架构 | macOS / Darwin arm64 |
| 解释器 | 主测 Python 3.12.13；Python 3.11.15 可用并通过导入烟测；Python 3.10 当前不可用 |
| 基础依赖 | 标准库；离线示例无网络、账号或 API key |
| extras | 由完整 CI 检查命令决定；未安装 extra 的 skip 不作为通过证据 |

## 命令和结果

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| Plan / diagnostics / examples / public API 回归 | `.venv/bin/python -m pytest -q tests/test_plans_contract.py tests/test_plans_store.py tests/test_plans_scheduler.py tests/test_plans_acceptance.py tests/test_plans_replanning.py tests/test_diagnostics_plans.py tests/test_examples_plans.py tests/test_public_api.py tests/test_public_snapshot_whitelist.py tests/test_import_fence.py` | 157 passed in 2.96s。 |
| 完整源码测试 | `.venv/bin/python -m pytest -q tests` | 1,592 passed、22 skipped、2 failed in 100.20s。失败为两条 Core 取消断言，详见验收映射；本轮未修改 Core。 |
| 离线依赖准备 | `.venv/bin/python scripts/ci_check.py --prepare-wheelhouse build-wheelhouse` | 退出 0；准备 46 个 wheel。 |
| 离线 CI | `.venv/bin/python scripts/ci_check.py --offline --wheelhouse build-wheelhouse --keep-artifacts --artifacts-dir build/p0.6-ci-final` | 退出 1。package suite 1,592 passed、22 skipped、2 failed；冷导入、相对 Markdown 链接、来源文件检查通过。 |
| wheel / sdist 树外构建与安装 | 离线 CI 调用 `scripts/verify_release.py`。 | wheel `yanxu_harness-0.4.0a1-py3-none-any.whl`：666,939 bytes，SHA-256 `0de39e45ae882b998979db9b23546aea7c74f03864618efcd7db4cfcb918c004`；sdist `yanxu_harness-0.4.0a1.tar.gz`：1,607,186 bytes，SHA-256 `b016f5913bb8ec26238303293250da89d999c0a39b865f0bfe4c790eb8dcc149`。两者均构建并离线安装；wheel 导入来自临时 `site-packages`。复制套件 1,665 passed、30 skipped、2 failed（同两条 Core 取消用例），所以 verifier 退出 1。 |
| Python 3.11 | `PYTHONPATH=src python3.11 -c 'import sys, yanxu_harness; print(sys.version.split()[0], yanxu_harness.__file__)'` | 3.11.15 源码导入成功；未运行全套测试。 |
| Python 3.10 | 当前环境没有 `python3.10`。 | 未验证。 |

## 迁移与边界

- 普通 Run 和非编排 Task API 不需要启用 Plan schema。
- SQLite host 明确以 `enable_orchestration=True` 启用可写 schema；只读 Inspector 使用既有数据库并且不迁移。
- `LocalWorker` 只有显式 `enable_orchestration=True` 并指定单个 `orchestration_namespace` 才读取 Plan 候选。
- Host 负责身份认证、审批授权、工具/工作区配置、evidence reader、verifier、quota 解析、outbox 调度和何时 pause/retry/replan。
- 首轮不提供 TUI/Web、后台 daemon、shell/coding tool、动态共享 token/cost/dispatch 预算、跨 Plan/跨 revision 自动复用、P1/P2 时间线或自动目标循环。PlanInspector 能报告声明的 budget refs，但不会声称知道宿主额度的当前数值。

## 提交记录

先前阶段提交及阶段命令见 [`plans-progress.zh-CN.md`](plans-progress.zh-CN.md)。本轮改动仍在工作树中，未创建 P0.6 提交；未发布或标记版本。
