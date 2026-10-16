# 恢复修复、运行诊断与任务评测：独立验收

日期：2026-10-16。核对提交：`745fb3b`。对应[实施计划](runtime-diagnostics-evaluation-implementation-plan.zh-CN.md)与[实施方交付报告](runtime-diagnostics-evaluation-delivery-report.zh-CN.md)。

**结论：原三项恢复缺陷的修复通过专项回归；诊断和离线评测已有实际交付，但本轮独立复核还发现四项 P2，整体验收尚未闭环。** 以下问题均有本地探针证据，不能仅用现有套件通过数代替修复。

本轮只增加验收记录及其文档入口，不修改运行时实现。审阅开始时工作树有两份 README 的说明调整；验证使用独立虚拟环境，保持宿主环境与业务数据隔离。

## 1. 已核实的交付

- PTR-R1：恢复从持久化基线比较当前指令、工具和策略；缺少基线时保守拒绝，拒绝带结构化信息。
- PTR-R2：账本分类、核对、剩余工具与后续模型进入同一个受限驱动；恢复继续使用同一 Runner。
- PTR-R3：已确认失败回执能够结算，unknown/deferred 继续保留核对屏障。
- D 线：存在真实只读查询、全局事件扫描、来源分级、计量覆盖、JSON/Markdown 与 CLI；已有只读和租户隔离测试。
- E 线：八个冻结离线案例经过真实 Harness、SQLite 与独立验收器；故障案例包含真实子进程退出。

真实模型能力、跨平台、多主机竞争和宿主自有账本的适配仍按交付报告的未验证边界处理。本轮没有调用付费模型，也没有执行 StoryIDE 接入。

## 2. RDE-D1 · P2：截断报告的游标跳过未返回事件

位置：[reader.py](../src/yanxu_harness/diagnostics/reader.py#L284)，尤其是 `_read_events` 的截断与 `next_cursor` 计算。

`last_collected` 在扫描时持续更新；超过 `max_events` 后，`matched` 被裁剪，但 `last_collected` 没有回退到裁剪后最后一条事件。因此游标指向最后扫描的匹配事件，而非报告实际携带的最后一条。

实测：真实完成运行有 7 条匹配事件，序号为 0–6。设置 `event_limit=2, max_scan_pages=100, max_events=2`：

```text
first_carried = [0, 1]
first_truncated = true
next_cursor = 6
second_carried = []
skipped_sequences = [2, 3, 4, 5, 6]
```

影响：调用者按报告给出的游标续读，仍然拿不到被截断的工具、请求或 usage 事件。默认配置下，匹配事件超过 2000 且继续被扫描时也存在同类问题。

修复要求：截断后从实际携带的最后一条事件计算续读位置；扫描高水位与返回游标分别保留。优先在达到收集上限时停止积累，避免先收集再裁剪。增加多页、多 Run 交错、末尾无匹配、连续续读的测试，断言拼接后不遗漏、不重复。

## 3. RDE-E1 · P2：验收真值与测试命令未进入案例摘要

位置：[EvalCase.digest](../src/yanxu_harness/evals/types.py#L169) 与 [FixtureSpec.digest](../src/yanxu_harness/evals/types.py#L83)。

`EvalCase.digest` 不包含 `expectations`，但定位、审批、恢复等验收器直接使用它决定通过与否。`FixtureSpec.digest` 不包含 `baseline_command`，而该命令同时影响前提检查和最终验收。

实测一：仅修改 `locate-bug.expectations['line']`，模型脚本、夹具和实现都不变：

```text
same_digest = true
baseline_verdict = passed
candidate_verdict = failed
comparable_rows = 1
incomparable_rows = 0
```

实测二：将修复任务的测试命令替换为 `python -c 'raise SystemExit(1)'`，夹具内容不变：fixture/case 摘要均相同，结果由 passed 变为 failed，比较器仍生成可比行。

影响：验收标准改变，会被误报为框架或模型回归；反向放宽真值也可能被误报为能力提升。这不满足“相同任务与验收条件才比较”的契约。

修复要求：将验收真值、实际测试 argv 等会影响执行和判定的配置纳入规范化摘要，检查其他遗漏字段。明确旧摘要的兼容/版本规则，不用新算法悄悄覆盖历史摘要。覆盖只改变真值、只改变测试命令、配置字典顺序变化等正反例。

## 4. RDE-E2 · P2：重复样本匹配复用基线，膨胀样本数与成本

位置：[compare_reports](../src/yanxu_harness/evals/compare.py#L158)。

找不到相同 `repeat_index` 时，比较器回退到 `candidates[0]`，且没有消费/去重该基线样本。候选有多个重复、基线缺少部分重复时，同一个基线结果会被加入 `matched_before` 多次。

实测：基线只有一个结果（10 个 input tokens、1 次耗时观测），候选有索引 0、1、2 三个结果：

```text
actual_baseline_results = 1
reported_baseline_cases = 3
reported_baseline_tokens = 30.0
reported_baseline_elapsed_samples = 3
comparable_rows = 3
incomparable_rows = 0
```

影响：报告制造不存在的样本和成本，质量分母也会受到基线重复权重影响。样本不完整时尤其容易误导版本评估。

同一位置还存在小样本说明问题：`repeats` 使用整份候选报告的结果数计算。5 个案例各只有 1 个观测时，`caveats=[]`，与“每案例样本数”的说明不符。

修复要求：明确逐次配对与分布比较的不同语义。逐次比较使用完整样本键、一对一匹配，缺项列为不可比/未运行；分布比较独立汇总两边原始样本，不复用样本补齐。拒绝或显式处理重复键；按案例统计真实模型样本数和缺失覆盖。增加不等重复数、缺失索引、重复键、多案例少样本的回归。

## 5. RDE-E3 · P2：命令输出仅在结束后裁剪，采集仍无上限

位置：[run_baseline](../src/yanxu_harness/evals/fixtures.py#L529)。

`subprocess.run(..., capture_output=True)` 先把 stdout/stderr 全部读入内存；`summarise_command_output` 在命令结束后才裁剪为 2000 字符。该路径被前提检查、工具测试命令和验收器复用。

低负载探针仅打印 256 KiB，实测：

```text
exit_code = 0
captured_stdout_chars = 262145
captured_stderr_chars = 0
published_summary_chars = 2000
```

影响：公开报告虽然短，评测进程仍可能被大量测试日志占满内存；运行超时不会限制超时前已经采集的字节。实施计划 §9 明确要求命令输出上限，当前实现及文档中的“bounded output”只覆盖事后报告长度。

修复要求：采集阶段即限制保留字节，持续排空或在超限时终止并回收命令；明确超限的结构化结果、是否截断、保留量/丢弃量。stdout 与 stderr 都要覆盖，保留 argv、cwd、超时与编码规则。沙箱仍由宿主提供，不能把这一修复描述为 OS 隔离。

## 6. 可运行复现

在仓库根目录、已经安装 `.[dev,yaml,pydantic]` 的独立环境中执行。下述脚本只使用临时目录，不修改生产数据或源码；分页部分复用已有测试夹具建立真实运行。

```python
import asyncio
from dataclasses import replace
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path.cwd() / 'tests'))
import test_diagnostics_reader as helpers
from yanxu_harness.diagnostics import RunInspector
from yanxu_harness.storage import SqlitePersistence
from yanxu_harness.evals import compare_reports
from yanxu_harness.evals.cases import case_by_id
from yanxu_harness.evals.runner import run_case
from yanxu_harness.evals.types import EvalCaseResult, EvalOutcome, FixtureSpec
from yanxu_harness.evals.fixtures import run_baseline
from yanxu_harness.evals.verifiers import summarise_command_output

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    paths = helpers.completed_run(root / 'pagination')
    store = SqlitePersistence(paths.runs, read_only=True)
    try:
        async def pagination():
            reader = RunInspector(
                persistence=store, event_limit=2,
                max_scan_pages=100, max_events=2,
            )
            first = await reader.facts(helpers.identity())
            second = await reader.facts(
                helpers.identity(), cursor=first.boundary.next_cursor,
            )
            print('pagination', [e['sequence'] for e in first.events],
                  first.boundary.next_cursor,
                  [e['sequence'] for e in second.events])
        asyncio.run(pagination())
    finally:
        store.close()

    original = case_by_id('locate-bug')
    changed = replace(original, expectations={
        **original.expectations, 'line': 'DIFFERENT ACCEPTANCE TRUTH',
    })
    before = run_case(original, root / 'original')
    after = run_case(changed, root / 'changed')
    compared = compare_reports(
        {'results': [before.to_record()]},
        {'results': [after.to_record()]},
    )
    print('truth', original.digest == changed.digest,
          before.outcome.value, after.outcome.value,
          len(compared['comparable']))

    def sample(index):
        return EvalCaseResult(
            case_id='same', case_version='1', case_digest='same-question',
            attempt='a1', outcome=EvalOutcome.PASSED, repeat_index=index,
            elapsed_seconds=1.0,
            usage=({'unit': 'input_tokens', 'quantity': 'actual', 'amount': 10},),
        ).to_record()

    compared = compare_reports(
        {'results': [sample(0)]},
        {'results': [sample(i) for i in range(3)]},
    )
    print('repeat counts', compared['quality']['before']['cases'],
          compared['cost']['before']['units']['input_tokens']['known_subtotal'])

    spec = FixtureSpec(
        name='output-probe', version='1', files={},
        baseline_command=('python', '-c', "print('x' * 262144)"),
    )
    completed = run_baseline(spec, root)
    print('output', len(completed.stdout),
          len(summarise_command_output(completed.stdout, root)))
```

## 7. 验证记录与验收门槛

- R/D/E 与既有恢复、账本、审批、隔离、上下文和存储专项合计：**248 passed**，独立环境执行，耗时 21.30s。
- 第一次使用原工作区虚拟环境时出现包未安装引起的子进程导入失败；重建独立环境后上述命令全部通过，该环境问题不计为本轮实现缺陷。
- 首次完整离线门禁的源码测试：1058 passed / 1 skipped / 1 failed。唯一失败为两份 README 说明调整后 `SNAPSHOT.json` 尚未同步；清单属于生成文件，补齐本轮验收文档后统一刷新，再验证。
- 刷新清单后复跑完整离线门禁：**6/6 通过**，源码套件 **1059 passed / 1 skipped**（58.23s），冷导入、wheel/sdist 源码树外安装与运行、文档链接和来源声明均通过。首次失败保留为文档清单同步问题，不归入四项实现缺陷。
- 最终门禁在追加本条结果前执行；随后只补验收文字、重新生成清单，并补查文档链接、清单一致性和导出白名单。
- 四项额外探针均复现本文所述问题；既有套件通过不代表这些边界已经覆盖。

后续先为四项问题补红测，再分别修复，更新样例摘要及兼容说明，并跑本轮专项和发行门禁。独立复核通过后再将整轮标为验收完成。持久 inbox、编排与 subagent 仍按原路线图推进，不提前混入本轮修复。

**下一轮统一任务书**：[持久输入、任务编排与诊断评测缺陷修复实施计划](durable-input-orchestration-implementation-plan.zh-CN.md)。将四项修复与方向四放入同一份交接计划，保留“先通过缺陷修复门禁，再接线编排能力”的实施顺序；本文的独立复核结论保持不变。

## 8. F 线复核闭环（实施方记录，2026-10-16）

本节是**追加**记录：§2–§5 的修复前事实、§6–§7 的复现步骤与验收门槛原样保留，不改写历史。
四项 P2 按 [实施计划](durable-input-orchestration-implementation-plan.zh-CN.md) §4 分项修复，
每项一个可审阅提交，F0 的红测先于修复落地。

| 编号 | 红测（修复前失败） | 修复提交 | 修复要点 |
| --- | --- | --- | --- |
| RDE-D1 分页 | `5b1a147`：`carried == expected` 失败，`[0, 1]` 对 `[0, 1, 2, 3, 4, 5, 6]` | `6baec42` | 收集上限在扫描途中到达即停在那里；`next_cursor` 落在**实际返回**的最后一条匹配事件；`high_water` 单独记录扫描高水位；`ReadBoundary` 新增 `exhausted`（读到存储末尾）与 `truncated`/`scan_limit_hit` 三事分述；`limit`/`max_scan_pages`/`cursor` 参数校验 |
| RDE-E1 摘要身份 | `5b1a147`：改 `expectations['line']` 前后同为 `sha256:73fdf708…`；改测试命令后 fixture 摘要不变 | `2ff6f6a` | 摘要算法版本化（`EVAL_DIGEST_ALGORITHM = 2`）；`EvalCase.digest` 纳入 `expectations`、`instructions_ref`、`command_policy`；`FixtureSpec.digest` 纳入 `baseline_command`；嵌套数据不可变；算法 1 记录可读但比较时列 `legacy` 不比较；未知算法在读取时拒绝；结果 digest 与套件记录交叉核对；`_match_key` 真正包含算法 |
| RDE-E2 样本匹配 | `5b1a147`：`quality.before.cases == 3`（实为 1）、`known_subtotal == 30.0`（实为 10）、3 条可比行 | `e9b394b` | 按冻结样本键（案例身份 + `repeat_index`）一对一配对，删除 `candidates[0]` 回退；重复键两侧都判 invalid；缺失项进 `incomparable` 并给出实际分母；`samples`（listed/valid/paired/duplicate/not_run/infra_or_invalid + `per_case`）与 `distribution`（各侧原始样本）分列 |
| RDE-E3 命令输出 | `5b1a147`：262145 字符被保留、1 MiB 输出保留 1048577、3.2 MB 输出保留 3276800 | `53d344b` | 三条命令路径共用一个有界执行器 `evals/commands.py`：`read1` 分块持续读取、按策略上限保留并计数丢弃；`keep-bounded-drain` 与 `stop-on-limit` 两种显式策略；`min(policy, caller deadline)`；有界清理与逐平台声明的后代清理；`readers_finished: false` 表示剩余输出**未知**；不落临时文件；`command_policy` 进入案例摘要，`protocol.execution` 记录声明值与实际执行值并不一致即 `invalid_result` |

修复过程中另外发现并修掉的两个真实缺陷（都不在原四项内）：F4 第一版用 `BufferedReader.read`
读取管道，会阻塞到缓冲填满或 EOF，于是"打印一行后保持管道打开"的子进程其输出全部留在内核里
（改用 `read1`）；F4 的反向核对最初用 `policy.digest` 与自身 `to_record()` 比较，而 `to_record()`
又包含 `digest`，导致每个案例都被判成 `invalid_result`（改为 `digest` 即 `_policy_digest`）。
F2 的提交信息曾把 `_match_key` 说成已包含算法而当时并未包含——摘要本身带算法作用域所以行为等价，
但该说法不准，已在 `e9b394b` 修正并以测试固定。

样例与兼容：摘要算法升级后 `docs/samples/evals/*` 与只读诊断样例全部重新生成（`6d46eae`、
`510d882`、`587bc23`、`36ee3ce`），生成命令见 `docs/samples/README.md` 与
`docs/samples/evals/README.md`；生成脚本 `scripts/regenerate_eval_samples.py` 使"错误答案基线"的
比较样例可复现。算法 1 的旧样例仍可读，比较器按 §F2 的规则把它标为 `legacy`，不宣称可比。
`docs/samples/partial-recovery-report.*` 现同时给出 `boundary.exhausted`。

F 门禁实测（干净 worktree，提交 `210dda7`，`.venv` Python 3.12.13）：

- 本计划 §14 的原回归命令逐字列表（15 个文件）→ **248 passed**；`tests/core/test_core_agent.py` → **43 passed**；
  本轮新增 `tests/test_acceptance_review_gaps.py`（四项红测 + F1–F4 验收用例）→ **52 passed**；
- 完整套件 → **1111 passed / 1 skipped**；
- `scripts/ci_check.py --offline --wheelhouse build-wheelhouse` → **6/6**（含 wheel/sdist 源码树外安装并运行示例、冷导入、文档链接与来源声明）。

未执行或未支持的边界仍然照旧：真实模型 provider、远程 CI、Python 3.10/3.11 实际运行、Linux/Windows
的进程组清理（仅声明 `windows-taskkill`，未在该平台执行）、多主机并发。F 线不改变这些结论，也不把
它们藏进通过总数。
