# 公开源码整理与敏感内容检查

日期：2026-10-20（Asia/Shanghai）。包版本：`0.3.0a1`。
远端检查基线：`origin/main` 的 `9aed198872916c9b3d1496a4647ed24875364ef4`。
本记录描述公开后的整理与检查，不改变已有发行产物及其 SHA256，也不代表完整安全审计。

## 1. 公开文档与入口

- README 中英文以源码安装和无需 API Key 的恢复演示开场，补齐持久 inbox、本地调度、父子任务及 MCP 能力和示例。
- [文档索引](README.md)区分当前接入契约、发行证据与历史方案；保留原始失败复现和修复记录。
- [支持范围](../SUPPORT.md)区分当前版本实测、旧版本历史实测、静态检查和未验证平台。
- [安全政策](../SECURITY.md)与贡献指南使用真实仓库地址；包元数据增加仓库、文档、问题与安全政策链接。
- GitHub **Private vulnerability reporting** 已在仓库设置启用，并在 Security 概览核对为 Enabled。
  私密入口：[Report a vulnerability](https://github.com/SZWzz/yanxu-harness/security/advisories/new)。
  Issue 入口也提供该链接，避免把漏洞报告引向公开讨论。
- 移除已跟踪的空文件 `src/yanxu_harness/errors.py.new`。它没有内容，不承担实现或兼容职责。

## 2. Git 历史与当前文件

已先执行 `git fetch origin --prune`，并用 `git ls-remote --heads --tags origin` 核对远端引用。
当时远端有 `main` 与 `codex/release-0.2.0a1` 两个分支、无 tag。本地全部可达引用共
**137 个提交、1,122 个唯一 blob（26,875,055 bytes）**，包括未推送的本地分支。

| 检查 | 方法与结果 |
| --- | --- |
| 历史补丁中的疑似密钥 | Gitleaks **8.30.1**，默认规则，`--all --full-history`，输出脱敏；扫描 137 个提交，1 条命中，人工核对为测试哨兵 |
| 提交消息、作者和提交者元数据中的疑似密钥 | Gitleaks `stdin` 扫描，0 条命中 |
| 所有可达历史 blob 与文件名 | 补充检查私钥头、URL 内嵌账号密码、凭据文件名和本机绝对路径；未发现私钥、URL 凭据、凭据文件或个人 home 路径。卷路径仅命中导出脚本自身的防泄漏规则定义 |
| 整理后的当前文件 | 复制 Git 跟踪文件及未忽略的新文件（共 313 个）后用 Gitleaks `dir` 扫描；同一测试哨兵 1 条命中，没有新增疑似密钥 |

唯一 Gitleaks 命中是提交 `3626c1fc21e96900eff97daddb554542510fe547` 中
`tests/mcp/test_http.py:33` 的 `SECRET` 常量。该字符串由测试明确设为 loopback HTTP
夹具的预期 token，用来断言公开描述、错误、facts 和 stderr 不泄漏凭据；它不是从用户环境
加载的真实服务凭据。不删除该测试、不改写历史、不把扫描器规则整体关闭。

可复现的历史命令（在源码根目录运行；原始报告留在忽略的 `build/` 中）：

```bash
mkdir -p build/public-audit
gitleaks git --redact --no-banner --log-opts='--all --full-history' \
  --report-format json --report-path build/public-audit/gitleaks-history.json .
```

上述命令会因这条人工核对的测试哨兵返回 **1**；不能把该退出码记录为零命中或执行失败。
Git 历史扫描和模式检查不能证明不存在所有形式的敏感数据，也不覆盖他人 clone/fork、
不可达/已删除的远端引用或 GitHub 后台缓存。真实凭据一旦发现，应先撤销/轮换，再评估清理范围。

## 3. GitHub Actions 历史

分页能力为 `per_page=100`；API 返回全部 **6 次运行**，每次仅 1 次 attempt，共 **42 个 job**。
逐 job 读取步骤和日志，并逐 run 检查 artifacts：

| Run | 结局 | Job 数 | 已列出的执行步骤 | 日志结果 | Artifact 数 |
| --- | --- | --- | --- | --- | --- |
| [39340985433](https://github.com/SZWzz/yanxu-harness/actions/runs/39340985433) | failure | 7 | 0 | 7 个均为 404 / BlobNotFound | 0 |
| [39340473140](https://github.com/SZWzz/yanxu-harness/actions/runs/39340473140) | failure | 7 | 0 | 7 个均为 404 / BlobNotFound | 0 |
| [39223179637](https://github.com/SZWzz/yanxu-harness/actions/runs/39223179637) | failure | 7 | 0 | 7 个均为 404 / BlobNotFound | 0 |
| [38932316579](https://github.com/SZWzz/yanxu-harness/actions/runs/38932316579) | failure | 7 | 0 | 7 个均为 404 / BlobNotFound | 0 |
| [38932316339](https://github.com/SZWzz/yanxu-harness/actions/runs/38932316339) | failure | 7 | 0 | 7 个均为 404 / BlobNotFound | 0 |
| [38704090465](https://github.com/SZWzz/yanxu-harness/actions/runs/38704090465) | failure | 7 | 0 | 7 个均为 404 / BlobNotFound | 0 |

**没有取得可供内容扫描的历史日志字节。** 这说明本轮检查时日志不可用，不能表述为“42 份日志已扫描且安全”，
也不能仅凭 404 推断每条日志从未产生或泄漏。没有发现需要删除的可下载日志或 artifact；未删除运行记录。
这些执行前失败也不构成 Linux/Windows 或 Python 矩阵的验证证据。

最新历史运行 `39340985433` 的网页 Annotations 也逐项说明 7 个 job 被 GitHub 账户运行限制拦截，
未启动。此次整理不更改账户账单或支出设置；新的公开仓库运行结果需另行核对。

## 4. 验证记录

第一轮基础套件：**1305 passed / 13 skipped**（Python 3.12.13 / macOS arm64）。
第一轮 `ci_check.py --offline` 的安装、基础测试、冷导入、文档链接与来源许可通过；发行步骤失败，
原因是缓存准备只枚举固定的 `dev/yaml/pydantic`，而发行安装会安装产物声明的所有 extras，遗漏了 MCP。

本轮修正 `scripts/verify_release.py::declared_requirements`，从 `pyproject.toml` 读取所有 extras
供准备命令和离线缺项检查使用，避免每新增适配器再维护一份固定列表。回归测试
`test_preparation_covers_optional_extras_the_artifact_installer_will_use` 在修复前因缺少 MCP 失败，
修复后通过，并覆盖未来新增的 extra。构建脚本、公开 API、清单、导入隔离和静态 Python 下界的聚焦门禁
**37 passed**。

实际重跑准备命令时又复现相对目录问题：子进程在缓存目录内启动，同时把同一相对目录传给 pip，
导致下载落到嵌套目录、随后缺项检查仍失败。`ci_check.py::prepare_wheelhouse` 现在先解析为绝对路径，
再设置子进程 cwd 与目标；新增回归在修复前失败，并断言文件落到请求的缓存根目录且不创建同名嵌套目录。

第二轮完整门禁的源码套件 **1307 passed / 13 skipped**；树外 MCP 测试收集因共享辅助模块不可导入而失败。
原因是验证器删除了复制后的根 `conftest.py`。现保留该文件以设置测试辅助目录，包位置仍由复制后的
`package_paths` 从安装环境解析，并清空 pytest 子进程继承的 `PYTHONPATH`。新增独立子进程回归在
修复前复现导入失败，修复后通过；最终聚焦门禁 **39 passed**。运行时代码与持久化格式未改动。

最终重新执行：

| 命令或检查 | 结果 |
| --- | --- |
| `python scripts/ci_check.py --prepare-wheelhouse build-wheelhouse` | exit 0，41 个 wheel，MCP 及其依赖已包含 |
| `python scripts/ci_check.py --offline --wheelhouse build-wheelhouse --keep-artifacts` | exit 0，**6/6**：安装、源码测试、冷导入、wheel/sdist 树外测试与十二个示例、文档链接、来源许可全部通过 |
| 最终源码套件（由上述完整门禁运行） | **1308 passed / 13 skipped**；未安装可选 SDK 的基础环境不作为 MCP 专项通过证明 |
| 当前文件 Gitleaks 复扫 | 同一测试哨兵 1 条命中，无新增疑似密钥 |

保留的失败与成功日志位于本机忽略的 `build/public-audit/`，未发布原始日志。wheel 与 sdist
均从新建环境中的安装产物运行测试、十二个离线示例、评测与只读诊断；本次不是远程 CI 结果。

已有 `0.3.0a1` 发行产物的验证数字和摘要保持原样，见[发行记录](release-0.3.0a1.zh-CN.md)。
本次构建仅验证新的源码与元数据，不替换宿主已经固定的发行 wheel；不重新声明真实模型质量或未运行平台已经通过。

## 5. 公开后的首次远程 CI

提交 `615ecf2838aa5affa863c32ff09a3472d6e4ec27` 推送并创建 [PR #1](https://github.com/SZWzz/yanxu-harness/pull/1)
后，GitHub 托管 runner 已实际启动。[PR 运行 39358039181](https://github.com/SZWzz/yanxu-harness/actions/runs/39358039181)
的文档与许可 job 通过，五个环境的依赖准备均通过；Linux Python 3.10/3.11/3.12 的首次源码与树外测试
均在 `test_the_namespace_cap_counts_tasks_not_harness_objects` 失败，其余基础测试为 1307 passed / 13 skipped。
这些失败保留为首次执行证据，不改记为通过。

核对发现测试用固定 50 次 `asyncio.sleep(0)` 假定 SQLite 领取已完成，未实际等到 Harness 启动，
因此把空的 inflight 记录当作并发限制失败。本轮改为等待阻塞 Harness 的 `started` 事件（有界超时），
仍断言 tenant-a 正在运行、同 namespace 的第二任务不被选中、tenant-b 获得服务；`finally` 释放驱动。
只修改测试同步，不修改调度器或降低并发断言。跨平台状态仍以指定提交的实际运行结果为准。

### 同步修正后的证据与保留限制

修正提交：`15c4a5bfde212ecfe6454b893730e075b66ebcdf`。本地调度与恢复相关回归 **41 passed**。
同一提交的 [push 运行 39358744179](https://github.com/SZWzz/yanxu-harness/actions/runs/39358744179)
中，Linux Python 3.10/3.11/3.12 与 macOS Python 3.12 的完整门禁分别 **6/6 通过**；
每个基础套件均为 **1308 passed / 13 skipped**。各成功 job 的链接见[支持范围](../SUPPORT.md)。

同一提交的 [PR Linux/Python 3.12 job](https://github.com/SZWzz/yanxu-harness/actions/runs/39358747581/job/118420010182)
在基础套件通过后，于树外套件的 `test_the_long_context_case_compacts_and_each_repeat_gets_its_own_allowance`
出现 **1 failed / 1371 passed / 29 skipped**。该案例使用 50ms 期限，第一重复失败、第二重复通过；
本地具名复验 **1 passed**，同提交 push job 的完整门禁通过。此处保留为间歇性评测失败，
不把它写成已经修复，也不将整个 PR workflow 写成全绿；没有更改冻结案例或放宽验收器。

首次 Windows 基础套件为 **11 failed / 1296 passed / 14 skipped**；其完整安装验证随后因新提交
按并发策略取消。按作者明确选择，本轮仅完成公开整理，Windows 兼容性另行处理，相关测试要求未降低。

公开后补充取得并扫描了 **19 份已结束 job 的日志**：首次 PR 的 7 份（包括失败/取消），
以及同步修正后 push 与 PR 中已结束的 12 份。Gitleaks 8.30.1 脱敏扫描约 **473,792 bytes**，
**0 条命中**。未结束的 Windows job 不计入这 19 份；历史最初 42 份日志不可取得的限制仍然保留。
原始日志仅留本机忽略目录，仓库只记录方法与结论。

## 6. Windows CI 兼容性跟进（PR #2）

[PR #2](https://github.com/SZWzz/yanxu-harness/pull/2) 在 `windows-latest` 上运行了真实 wheel/sdist 门禁。
本轮修正覆盖 Git 行尾归一化、Windows 路径与子进程环境、异步 HTTP 读取与取消、MCP 示例资源关闭、
Windows 进程存活检测，以及恢复评测用的租约期限。此前首次基础套件的 **11 failed / 1296 passed / 14 skipped**
仍保留为历史记录；后续 job 已进入完整发行验证，最近日志中没有重现那些路径、行尾和资源关闭问题。

| 提交 / Windows job | 结果 | 失败情况 |
| --- | --- | --- |
| [`c15dd30`, 首次](https://github.com/SZWzz/yanxu-harness/actions/runs/39370845757/job/118458136127) | 1 failed / 1373 passed / 29 skipped | 50ms `test_the_long_context_case_compacts_and_each_repeat_gets_its_own_allowance`，第二次重复失败 |
| [`c15dd30`, 重跑](https://github.com/SZWzz/yanxu-harness/actions/runs/39370845757/job/118461264746) | 1 failed / 1373 passed / 29 skipped | 冻结案例 `long-context-summary-cancel` |
| [`0d2145b`](https://github.com/SZWzz/yanxu-harness/actions/runs/39373660633/job/118466861717) | 2 failed / 1372 passed / 29 skipped | `deadline-during-recovery` 在两个 CLI 子进程执行中失败 |
| [`af9de51`, 首次](https://github.com/SZWzz/yanxu-harness/actions/runs/39374962486/job/118470939684) | 3 failed / 1371 passed / 29 skipped | `deadline-during-recovery`；附加诊断显示 `max_runtime_exceeded`，50ms 到期前没有 `probe.c` admission |
| [`af9de51`, Windows job 重跑](https://github.com/SZWzz/yanxu-harness/actions/runs/39374962486/job/118473878804) | 1 failed / 1373 passed / 29 skipped | `long-context-summary-cancel`，`deadline-during-recovery` 通过 |

失败在几个固定 50ms 截止时间案例之间变化，与已记录的 Linux 3.12 短期限评测不稳定属于同一类时间敏感证据；目前没有一次 Windows 发行门禁完整通过。
没有修改冻结评测案例、降低预算或放宽验收器，因此 Windows 仍**未验证**。`0d2145b` 上本地离线六步门禁 **6/6 通过**（基础套件 1309 passed / 13 skipped）；
`af9de51` 的 Windows 变更仅增加失败记录字段，相关两个本地 CLI 测试通过，且同一 PR run 的 Linux/macOS job 通过。
这些本地与其他平台结果不替代 Windows runner 证据。新增 Windows 原始日志仅保留在本机忽略的 `build/public-audit/new-action-logs/`。

## 7. Windows CI 修复收尾：根因、证据与回归（PR #2，`b06c6b2`）

本轮在 `codex/windows-ci-compat` 上接续 PR #2，不改写历史发行产物、不改写 `e3031d5` 的发布准备提交、
不合并 PR、不发布 PyPI。基线为 `070213c`（远端同名分支一致、工作区干净），修复提交为
`9aeaf48`（崩溃夹具）、`785bebe`（期限评测版本化 + 通道 2 运行时契约）、`4de5bb6`（新的 wire reader 覆盖）、
`b06c6b2`（安装态读取源码的测试修正）。

### 7.1 根因表

| 症状 | 证据 | 根因 | 修复 | 回归 |
| --- | --- | --- | --- | --- |
| `partial-batch-evidence` 记录 `fault_exit_code=9`，但恢复停在模型请求、没有持久化工具批次 | push [39378094595 / job 118480832056](https://github.com/SZWzz/yanxu-harness/actions/runs/39378094595/job/118480832056) 的 `classified_interaction = run-1:orphaned-drive`，detail 为"停在模型请求、未提交响应" | `child.main` 用 `except BaseException: pass` 吞掉异常后仍 `os._exit(9)`；正常返回也走同一行。父进程把退出码 9 当成"已注入崩溃" | 仅 probe.b handler 使用 9；提前异常退出 70 并输出脱敏诊断（fault/phase/类型/traceback），未命中故障点而正常返回退出 71；两条路径都关闭自己持有的 SQLite 句柄 | `test_a_child_that_fails_before_the_fault_point_is_a_fixture_failure`、`test_a_child_that_finishes_without_the_fault_is_a_fixture_failure`（在 `070213c` 上分别记录 `fault=process-exit`，现为 `fixture-failure` + `infra_error`） |
| 同一夹具的"崩溃窗口"从未被核验 | 同上：ledger 里没有 probe.b 的 open intent，恢复却已开始 | 退出码是唯一证据 | 恢复前核验三条真实持久化前提：probe.a settled、probe.b 有 intent 无 receipt、probe.c 无行；并核验 probe.b handler 写下的 trace 行。不满足即 `fault-setup infra_error`，不交给恢复 verifier | `test_a_real_crash_proves_the_three_ledger_preconditions` |
| `deadline-during-recovery` 在 Windows 上 `max_runtime_exceeded` 且没有 `probe.c` admission | PR [39378099496 / job 118480852806](https://github.com/SZWzz/yanxu-harness/actions/runs/39378099496/job/118480852806)：ledger 仅 `probe.a/probe.b`，`tool:probe.c` span 为 absent；本机阶段测量：分类+reconciler 约 3ms，预算 50ms | 运行时没有违约：一次 invocation 的绝对 deadline 覆盖 ledger 读取、host reconciler、状态提交、admission 与调用本身。案例把"准备阶段 < 50ms"写成隐含前提，验收器又要求 admission | 新增 v2 案例（预算 2s、固定跨平台；目标 probe 阻塞 10s），新增更严格验收器 `deadline-stops-recovery-v2`（必须已 admission 且未 settled）；v1 冻结进 `HISTORICAL_CASES`，不再运行 | `test_the_recovery_deadline_covers_the_preparation_phase`（准备阶段超时窗口：usage_limited、无 admission、无 trace 行、不误记工具已启动）、`test_the_recovery_deadline_stops_the_blocking_probe_after_admission`（工具内窗口：记录 admission 偏移与 invocation 用时） |
| `long-context-summary-cancel` 间歇失败（50ms） | 历史 Windows/PR job 在固定 50ms 案例之间摆动 | 同一类机器速度假设（projection + 辅助调用 admission 必须挤进 50ms） | 同 v2：预算 2s，summarizer 无限阻塞；成本与 `usage_unknown` 断言不变 | 既有 `test_the_long_context_case_compacts_and_each_repeat_gets_its_own_allowance` |
| 夹具里散布 1s / +5s / +70s / 60s 四个租约数 | 本机子进程约 0.13s，同一案例在 Windows 上约 1.27s（约 9x）；1s lease 的余量最薄 | 未证明（见 7.4）：若子进程某次提交超过 1s，会先触发自身 fence 失败，而旧代码会把这次死亡记成"注入崩溃" | 单一 `FAULT_LEASE_TTL_SECONDS`（子进程与所有评测部署一致）；恢复到达时刻从 store 里真实 lease 行的到期点推导（仍走 store 的 holder/expiry 检查，不绕过、不 sleep） | `test_the_recovery_never_bypasses_the_crashed_childs_lease`（早到必须被 `RunBusy` 拒绝；到期后恢复进入 pause） |

阶段证据的取得方式：本地用一次性探针（未提交）在 `DefaultHarness._run_task`、`_classify_recovered_batch`、
`_converged_in_ledger`、`_reconcile_call` 上记录相对 monotonic 时间。`a3` invocation 于
0.0ms 启动（预算 50ms）、分类 0.9ms 内完成、`probe.c` admission 落在 3ms 附近，随后阻塞直至 50ms 到期；
Windows 上同一段准备超过 50ms。因此 0.9s/1.2s 的案例总耗时与 50ms 预算无关——它包含子进程与第一次恢复，
不能用来解释预算被超用。

### 7.2 评测版本与可比性

`CASES_VERSION` 由 `1` 升为 `2`。`deadline-during-recovery` 与 `long-context-summary-cancel` 的 v2 案例
使用新预算与新验收器；v1 原样保留在 `HISTORICAL_CASES`，digest 仍为 CI 记录过的
`sha256:2ac315287c6ba288005b095edfe94fcacd6eefafee60a2a7c97dd7e95f16fe2e`（测试断言此值），
不再属于任何 suite。因此 v1 结果与 v2 结果**不可直接比较**，比较器按 digest 拒绝配对。
`docs/samples/evals/*` 仍是 v1 的历史记录，未重生成；其 README 已说明该点。
没有为了保住旧 digest 手写摘要，也没有放宽任何断言。

### 7.3 验证记录（提交 `b06c6b2`）

| 项目 | 命令 | 结果 |
| --- | --- | --- |
| 本地完整套件 | `.venv/bin/python -m pytest -q` | **1323 passed / 13 skipped**（93.9s） |
| 本地离线六步门禁 | `scripts/ci_check.py --offline --wheelhouse build-wheelhouse --keep-artifacts` | **6/6**；包测试 1323 passed / 13 skipped；wheel+sdist 树外安装与 12 个离线示例 OK |
| 树外套件（同一提交前一步） | 同上 | 修正前 1 failed / 1387 passed / 29 skipped，失败项为读取检出目录的测试；改为从被导入模块读取后通过（`b06c6b2`） |
| 有界重复抽样 | 4 条路径 × 10 次，真实子进程 + 真实 SQLite | **40/40 passed**：partial-batch 10、drift 10、deadline 10、summary 10。`probe.c` admission 偏移 2–3ms（预算 2s）；invocation 2.011–2.014s；summary 每次 `calls=1`、`usage_unknown=True`；partial-batch 每次 `fault=process-exit`、`fault_exit_code=9`、三条前提成立 |
| 真实 Windows CI | push [run 39384654039](https://github.com/SZWzz/yanxu-harness/actions/runs/39384654039) / [windows job 118501497695](https://github.com/SZWzz/yanxu-harness/actions/runs/39384654039/job/118501497695) | **success**，7/7 job；Windows Python 3.12 走完"源码套件 → wheel/sdist 树外安装 → 离线示例"完整门禁 |
| 同一提交 PR 运行 | PR [run 39384659647](https://github.com/SZWzz/yanxu-harness/actions/runs/39384659647) / [windows job 118501515924](https://github.com/SZWzz/yanxu-harness/actions/runs/39384659647/job/118501515924) | **success**，7/7 job，含 Windows 第二次独立通过 |
| Linux / macOS | 同两个 run 的 ubuntu 3.10/3.11/3.12 与 macos 3.12 job | 全部 success，无回归 |

同一提交的两个 Windows job 各自完整通过，且失败案例不再在固定期限案例之间摆动。此前"重试直到偶然通过"的做法没有沿用：
这两次运行针对同一最终提交，且本地对此前抖动的四条路径做了有界重复抽样。

### 7.4 未证明项与剩余风险

- **1s lease 是嫌疑，不是已证实的根因。** 修复前没有留下子进程早退的原因（异常被吞掉）。现有证据是间接的：
  失败记录停在模型请求、无批次；本机子进程约 0.13s 而 Windows 同一案例约 1.27s。统一 TTL 消除了最薄余量，
  同时子进程现在会写出 phase/类型/traceback，父进程记录子进程存活时间；若再次出现会直接指出原因，而不是再被记成崩溃。
- **Windows job 原始日志不可公开下载。** 未带仓库管理员权限时，`/actions/jobs/{id}/logs` 返回 403；
  因此 Windows 侧证据是 job/step 结论与公开页面链接，通过数量来自本地同一门禁，未伪造成 Windows 数字。
- **TLS 覆盖依赖本机 `openssl` CLI。** 新 wire reader 的 TLS 分支由本地可控 HTTPS 服务覆盖
  （Content-Length 与 chunked 各一次，证书 `CN=localhost` 仅通过 `SSL_CERT_FILE` 信任）；无 CLI 的主机会跳过该测试并给出原因。
- **`docs/samples/evals/*` 记录的是 suite v1**，与新 v2 不可比较；保留为历史。
- **PR #2 未合并**，PyPI 未发布；发布准备提交与历史发行产物未改动。
