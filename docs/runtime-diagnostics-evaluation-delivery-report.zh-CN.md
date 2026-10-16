# 交付报告：恢复缺陷修复、运行诊断与任务评测

日期：2026-10-16。基线：`2c8a324`。实施计划：[runtime-diagnostics-evaluation-implementation-plan.zh-CN.md](runtime-diagnostics-evaluation-implementation-plan.zh-CN.md)。状态：**已实施**（未执行项单列在 §7）。

本报告只写这次真正执行过的命令与结果。凡是没跑过的（远程 CI、真实模型、其他 Python/平台、历史 checkout 的 eval 对照）都在 §7 明说，不藏进成功总数。

## 1. 提交与阶段

| 阶段 | 提交 | 内容 |
| --- | --- | --- |
| R0 | `e684cef` | 三项缺陷的目标断言先于修复提交；在该树上 **3 failed**（保留为基线证据） |
| R1 | `dccf06b` | 冻结原配置：`ExecutionState.arrangement`（schema 3）在首次派发前提交；恢复从已提交基线重建并比较；无基线拒绝；lease 后重读、租约移交 |
| R2 | `e652656` | 统一受限驱动：分类/reconciler/剩余调用/后续模型同一次受限可取消驱动；删除手工执行分支；暂停在驱动之外提交；顺序批次关闭不再死锁；保留工具自身 `is_error` |
| R3 | `1ee55c9` | 已确认失败按业务分类收敛；`unknown`/`deferred` 仍阻塞 |
| 记录 | `e1b9fbc` | 独立复核记录就地补第 7 节，保留修复前事实 |
| D0 | `0a0d99f` | 版本化报告对象与来源分级 |
| D1 | `4e861f4` | 缺口清单 + 派发窗口/请求完成/逐调用 span/分区计时副本（schema 4） |
| D2–D3 | `b96b8ea` | 只读 reader/投影/JSON+Markdown/CLI；只读连接、身份校验、全局 cursor 分页、白名单输出 |
| E0–E2 | `07683d2` | 冻结案例、包内夹具、独立验收器、真实 Harness 执行器、比较器与 CLI |
| 门禁发现 | `b8813fd` | 修正一个只在负载下暴露的测试断言（见 §6） |
| V | `cce8aa5`、`90cc7cb`、`8f574d9` | 文档同步、发布验证、宿主路径脱敏、干预次数取自审批库、结果携带预算/部署指纹/阶段耗时 |

## 2. R 线：修复前后

三项缺陷由独立复核（`docs/per-tool-settlement-recovery-acceptance-review.zh-CN.md` §6）用可运行探针复现。R0 先把探针变成断言，**修复前的树上失败**：

```text
FAILED tests/test_runtime_recovery_gaps.py::test_r1_an_orphan_recovered_under_drifted_instructions_is_refused
  assert ['notes.a', ..., 'notes.c'] == ['notes.a', 'notes.b']
FAILED tests/test_runtime_recovery_gaps.py::test_r2_the_runtime_limit_covers_the_recovered_handler
  assert RunStatus.COMPLETED is RunStatus.USAGE_LIMITED
FAILED tests/test_runtime_recovery_gaps.py::test_r3_a_confirmed_failed_receipt_converges_without_a_reconciler
  assert RunStatus.SUSPENDED is RunStatus.COMPLETED
```

修复后的同一文件（`tests/test_runtime_recovery_gaps.py`，15 passed）覆盖：

| 目标 | 断言 |
| --- | --- |
| R1 orphan 漂移拒绝 | instructions/config/tools/policy 四个维度各自：`ran_tools == [notes.a, notes.b]`、`stream.calls == 0`、`ContractError` 且指名漂移维度；修复前真跑了 C 并 completed |
| R1 suspended 入口 | SUSPENDED resume 同样四维度拒绝，什么都不派发 |
| R1 无基线 | schema 2 的旧快照被**拒绝**（"no recorded arrangement baseline"），不派发（`tests/test_runtime_recovery_compat.py`） |
| R2 期限 | `max_runtime_seconds=0.03` + 恢复时阻塞的 handler（3s）：`usage_limited` / `max_runtime_exceeded`、`stream.calls == 0`、`called == ["start"]`、被中断调用仍有账本准入行 |
| R2 取消 | `Harness.cancel` 到达恢复重派的 handler：`cancelled`、不请求模型、已准入操作仍在账本 |
| R2 部分提交 | 恢复中第二连接在下一个调用阻塞时读到上一个调用的已提交 observation |
| R3 失败回执 | 账本已 `failed`、Run 未更新、无 reconciler：完成、A/B 不重跑、C 跑一次、模型看到 `is_error` 结果 |
| R3 保守 | `unknown`/`deferred` 行：仍 `suspended` + reconciliation，`ran_tools == [notes.a, notes.b]`，C 无准入 |

漂移/无基线拒绝是**结构化**的（`ResumeRefused`：`reason_code`、漂移维度、比较双方指纹、`durable = False`
与 `to_record()`），因为"调用者无法检查的拒绝"等于无法审计；它不写入运行（记录必须保持可恢复状态），
计划 §6.3 允许的这一侧被显式标成 not_durable，而不是留白的副作用。

R 线另外发现并修复的两个真实缺陷（都不在原探针里）：`ToolBatchSession` 在消费者已停止时关闭会永久阻塞（顺序批次的截止/取消正好走这条路，结果是运行无法停止）；core 丢弃工具自身声明的 `is_error`，导致被拒绝的调用以成功到达模型。

## 3. D 线：诊断样例与边界

命令：

```bash
python -m yanxu_harness.diagnostics inspect \
  --runs <state>/runs.db --operations <state>/operations.db \
  --namespace project-a --session s1 --run-id r1 --format markdown
python -m yanxu_harness.diagnostics --help
python -m yanxu_harness.diagnostics inspect --help
```

样例（真实部分批次崩溃 → 二次进程恢复 → 第三次以只读身份读取）：
[`docs/samples/partial-recovery-report.md`](samples/partial-recovery-report.md) 与 `.json`，生成方式见
[`docs/samples/README.md`](samples/README.md)。样例里可以看到四个要求的事实：

```text
- attempts that did the work: `a2`, `a1`      # 做事的 attempt 与读取者 a3 不合并
| c-notes.a | notes.a | settled | observed | …:c-notes.a | receipt: … 'succeeded' |
| c-notes.b | notes.b | uncertain | observed | …:c-notes.b | receipt: … 'running'   |
- waiting for: `reconciliation` on `c-notes.b`
| tokens | unknown | unknown | missing | … | the provider did not report usage … |
| tool:notes.c | unknown | missing | missing | no observation was recorded … |
```

目标命令：`pytest -q tests/test_diagnostics_types.py tests/test_diagnostics_measurements.py tests/test_diagnostics_reader.py` → **47 passed**。其中只读与隔离的部分：

- 读取前后逐字节比对运行库事件行与账本行，且 handler 未再运行；
- 只读连接拒绝 `promote`/`settle`，不创建缺失的库，`SqlitePersistence` 同样；
- 跨租户读取被拒绝（namespace / session 都必须匹配），账本行按 namespace **和** run 过滤；
- 无只读账本能力 → `unsupported`（不是"不存在"）；
- 首页不含目标 run 事件时仍按全局 cursor 续读；`scan_limit_hit`、`truncated`、`next_cursor` 显式返回；
- 载荷里未被白名单覆盖的字段被省略并计数；嵌套映射不整体发布；Markdown 转义自由文本。

计量规则与覆盖度（`known_subtotal` + `unknown` 计数 + `complete`）在样例的 "Measurement" 段与
`docs/diagnostics-facts.zh-CN.md` 的缺口清单里逐条对应。

## 4. E 线：冻结任务、独立验收与新旧比较

命令：

```bash
python -m yanxu_harness.evals run --root build/eval-samples --out docs/samples/evals
python -m yanxu_harness.evals compare <before.json> docs/samples/evals/results.json --format markdown
python -m yanxu_harness.evals --help && python -m yanxu_harness.evals run --help
```

结果：[`docs/samples/evals/summary.md`](samples/evals/summary.md)、`results.json`（事实格式，含框架 commit/dirty、Python/平台、套件与案例摘要、声明的预算、部署自身指纹、逐阶段耗时（未测量者明确标注）、`offline: true`）。

| 案例（计划 §8.3 的七类） | 独立成功标准 | 实际结果 |
| --- | --- | --- |
| `locate-bug`（定位错误） | 答案指名**记录的真值**函数并逐字引用出错行，且运行确实读过该文件、未改动它 | passed |
| `fix-calc-tests`（修改实现） | 夹具自带测试通过，且 `test_calc.py` 未被改动 | passed |
| `review-diff`（检查与审阅） | 通过工具跑过项目自带测试，报告的真实退出码与验收器独立运行的结果一致，并指名预置缺陷的文件与行 | passed |
| `approval-write-once` | 跨暂停/恢复恰好写一次，文件内容正确，人工决定 1 次（来自可信存储的历史） | passed |
| `partial-batch-evidence` | 三个探针各跑一次、顺序不变、账本三项 succeeded、运行 completed | passed（真实子进程 `os._exit(9)`） |
| `drift-refusal` | 漂移部署下不派发第三个探针、不请求模型、错误指名 instructions | passed |
| `deadline-during-recovery`（恢复拒绝与期限） | `max_runtime_exceeded`、不请求模型、被中断调用仍有账本准入 | passed |
| `long-context-summary-cancel`（长上下文与摘要取消） | 期限中断辅助调用后停止（`usage_limited`/`max_runtime_exceeded`）、不再请求主模型、原文保留、辅助调用恰好计 1 次且成本为 `usage_unknown`；重复运行各自独立额度 | passed |

比较样例 [`docs/samples/evals/comparison.md`](samples/evals/comparison.md)：同一冻结案例，基线答案错误
（脚本仍然宣称"tests pass"）→ `failed`，候选正确 → `passed`，比较器给出 `changed: yes`、
before/after 通过率 0.0 → 1.0，并把 `infra_error`/`invalid_result` 与任务判定分开；成本只给
`known_subtotal` 与覆盖度，没有合计成"总成本"。

负面对照（`tests/test_evals_offline.py`，40 passed）：错答案失败、改验收夹具的测试失败、
改验收器 → `infra_error`、前提不成立 → `invalid_result`、真实模型案例无适配器 → `not_run`、
重复运行目录互相隔离、比较器拒绝比较不同问题、小样本只标为分布而非显著性；新增三类各有自己的对照：
只说「已完成」的定位答案失败、指错函数失败、把文件「修好」也失败；声称"tests pass"但退出码为 1 的审阅失败、
根本没跑测试的审阅失败；摘要案例的验收器对「已完成且未计费」与「真被中断并计费一次」给出相反结论，
且该案例真的发生了压缩（重复运行各自 `calls == 1`）。

## 4b. CLI 帮助与公开边界（实际输出）

```text
python -m yanxu_harness.diagnostics inspect --help
usage: … inspect [-h] --runs RUNS [--operations OPERATIONS] --namespace NAMESPACE
                 --session SESSION --run-id RUN_ID [--attempt ATTEMPT] [--cursor CURSOR]
                 [--limit LIMIT] [--max-scan-pages MAX_SCAN_PAGES]
                 [--format {json,markdown}] [-o OUTPUT]

python -m yanxu_harness.evals --help
usage: … evals [-h] {run,compare} ...
  run      run the suite offline (or a real-model case explicitly)
  compare  compare two results.json files
```

- 诊断 CLI 必填 `--runs`，`--operations` 可选（缺失时报告仍描述运行，并把每个操作引用标为
  `unsupported`）；库不存在时报错退出（exit 2），**不创建**空库、不迁移 schema；读取用只读连接，
  输出位置由 `-o` 显式选择。
- 评测 CLI 默认离线；`--real-model` 必须与 `--allow-real-model`、`--repeats`、`--budget-seconds`
  同时给出，否则拒绝（exit 2）；没有适配器时案例是 `not_run` 并说明原因。退出码：0 = 全部通过，
  1 = 有任务失败，3 = 有 `infra_error`/`invalid_result`。
- 公开边界：诊断输出按字段白名单（id/状态/计数/本 build 写的短语）；未列入的字段被省略并给出
  `omitted_fields` 计数，嵌套非标量映射不整体发布，Markdown 转义记录中的自由文本（含管道符与换行）。
  原文只给受控引用（`artifacts` 里的引用与摘要）。评测验收器的证据经过路径脱敏与长度上限，
  不携带宿主路径或解释器前缀。

## 5. 门禁与接口

| 验证 | 结果 |
| --- | --- |
| 完整套件（Python 3.12.13，本检出，`.venv`） | **1056 passed / 1 skipped** |
| `scripts/ci_check.py --offline --wheelhouse build-wheelhouse` | **6/6 通过**，包测试 **1056 passed / 1 skipped**（离线安装、包测试、冷导入、源码树外安装 wheel/sdist 并跑 examples + 新增的离线评测案例与只读诊断 CLI、Markdown 链接、来源声明） |
| `scripts/verify_release.py --offline --wheelhouse build-wheelhouse --keep --out build/release` | wheel **1025 passed / 25 skipped**、sdist **1025 passed / 25 skipped**，7 个 example 与新增的两个 CLI 入口在安装产物外跑通；构建时工作树干净（`8f574d9`） |
| 计划 §14 的目标命令（含本轮新增文件与 `tests/core`） | **534 passed** |
| 计划指定的原回归命令 | `pytest -q tests/test_runtime_partial_settlement.py tests/test_runtime_partial_recovery.py tests/test_runtime_recovery_compat.py tests/test_tools_ledger_recovery.py tests/test_runtime_resume.py tests/test_runtime_isolation.py tests/test_runtime_commit_boundaries.py tests/test_context_summary_isolation.py tests/test_events_and_versions.py tests/test_storage_sqlite.py` → **passed**（与新增文件同跑见上） |

接口/格式变化：

- `ExecutionState` schema **3 → 4**（1–3 仍可读）。v3 增 `arrangement`（部署基线），v4 增
  `request_instance`/`request_started_at`/`last_request_measurement` 与分区内 `started_at`/`duration_ms`。
  旧版本记录不补值：缺基线→拒绝，缺测量→unknown，缺调用声明→交给账本。
- 事件新增 `model_request_dispatched`、`model_request_completed`、`tool_call_observed`（公开载荷只含 id/状态/数字）。
- 新增只读打开能力 `read_only=True`（`SqlitePersistence`、`SqliteExecutionBackend`）；`all_operations()` 增加
  identity 字段（namespace/run_id/call_id/model_action_id）。
- 新增只读报告/评测格式，各自版本化：`DIAGNOSTIC_SCHEMA_VERSION=1`、`EVAL_SCHEMA_VERSION=1`，未知版本拒绝。
- 新 CLI：`python -m yanxu_harness.diagnostics inspect …`、`python -m yanxu_harness.evals run|compare`。
  两者默认离线；诊断拒绝创建缺失的库，评测拒绝在没有适配器时用脚本顶替真实模型案例。

保持的不变量：单一 Runner、纯 reducer（不读时钟、不做 I/O）、Gateway 唯一账本、派发前提交、
Python 3.10 + 标准库基础安装（新包只依赖标准库，冷导入检查通过）、生产包不导入 pytest/不读凭据。

## 6. 门禁发现的一个测试缺陷

`tests/test_context_summary_isolation.py::test_t10_one_run_with_two_sources_starts_at_most_one_call`
在一次离线门禁中失败，但行为正确：它断言"恰好一个准备报 `used=True`"，这只在第二个准备先于释放到达准入时成立；
当它**合流**到已在飞的调用（本 build 的单飞路径）时，一次外部调用、两个视图都用它的文本。确定性复现：
60 次里 10 次走合流分支。测试改为断言两种合法分支都成立的不变量（一次外部调用、一次计费、至少一个视图使用已付费文本、
未使用者的 fallback 必须点名预算），并另加一个不靠竞态的强制拒绝测试。

## 7. 未执行（如实登记）

| 项 | 原因 |
| --- | --- |
| 真实模型任务评测 | 本 build 未配置真实模型适配器；`--real-model` 必须显式给出模型/重复次数/预算，无适配器时案例记为 `not_run`（不是 pass）。 |
| 历史 `2c8a324` checkout 的 eval 对照 | 这些入口依赖本轮才有的只读诊断层（`read_only=True`、reader），在旧源码上无法执行；修复前事实改由独立复核在该树上的探针与 R0 红测记录。 |
| 远程 CI | 本轮未运行。 |
| Python 3.10 / 3.11 | 本机无对应解释器与 wheelhouse；静态下限门禁仍通过（`tests/test_python_floor.py`）。 |
| Linux / Windows | 未运行；实测平台仅 macOS arm64。 |
| ~~`scripts/verify_release.py`~~ | **已执行**：`8f574d9`、构建时工作树干净 —— wheel **1025 passed / 25 skipped**、sdist **1025 passed / 25 skipped**；安装产物外跑通全部 7 个 example，并跑通本轮新增的**离线评测案例**与**只读诊断报告**；产物 `yanxu_harness-0.1.0a2-py3-none-any.whl`（89 文件）与 `.tar.gz`（209 文件），摘要见 `build/release/SHA256SUMS` 与 `verification-report.md`。 |
| 宿主自有账本（`ownership=="host"`）的自动恢复 | 需要宿主实现等价的只读能力；未提供时恢复明确阻塞。普通执行路径不受影响。 |
| 多主机并发准入 | 只用两条真实连接同进程竞速验证过。 |
| 未放宽的既有边界 | `promote` 仍只接受未证明行（open/unknown），因此对 `deferred` 回执的证据会被拒绝——deferred 仍需要宿主按网关提出的问题作答。 |
