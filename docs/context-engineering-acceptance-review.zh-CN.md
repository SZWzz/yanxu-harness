# 上下文工程验收复核：摘要取消与运行预算隔离

日期：2026-10-16。核对提交：`6270dff`；修复后提交：工作区（R1/R2 修复）。状态：**两项问题已修复并有回归测试，待独立复核**。

后续实施任务书：[摘要取消与运行预算隔离修复计划（DeepSeek 实施）](context-summary-fix-plan.zh-CN.md)。包含分阶段改动、兼容要求、并发边界和验收矩阵。

修复记录（本节由实施者填写，独立复核仍需重新运行门禁与探针）：

- R1：`SummaryCompactor.compact` 不再把外部 `CancelledError` 转成回退；辅助任务在语义边界回收、已发生调用按未知开销结算后重新抛出取消。真实 `DefaultHarness` 期限与 `Harness.cancel` 期间主模型调用均为 0。
- R2：摘要额度改为按逻辑运行（`namespace/session/run`）隔离的运行级状态；预算检查与预留先于任何外部 await；`CompactionResult` 携带本次准备的不可变 usage 快照，`context_state.summary` 不再读取共享对象的“最后一次 usage”。
- 取消期间已发生的摘要开销通过 `ContextController.summary_facts(scope)` 进入运行终态记录（`default_harness._record_interrupted_summary`），不因未产生准备结果而丢失。
- 回归测试：[tests/test_context_summary_isolation.py](../tests/test_context_summary_isolation.py)（T1–T14；基线 13/18 失败，修复后 18/18 通过）。
- 已复跑验收探针：`independent_run_budgets` 实际/报告均为 `a=1, b=1`；`deadline_during_summary` 为 `summary_calls=1, main_model_calls=0, status=usage_limited, error_code=max_runtime_exceeded`。

本次核对包含此前持久提交修复和上下文工程交付。原派发前快照仅在内存的问题已修复；上下文 C0–C6 的主体与提交接线已存在，但下述两项使摘要路径尚不能完全验收。

独立复跑 `.venv/bin/python scripts/ci_check.py --offline --wheelhouse build-wheelhouse`：完整包测试 **897 passed / 1 skipped**，本地门禁 **6/6 通过**，包含安装产物与示例检查。这些结果不覆盖下述额外探针。真实模型质量评测与跨平台结论不在本次复核范围。

## R1 · P1：摘要路径吞掉外部取消，运行期限到点后仍请求主模型

位置：[context/compaction.py](../src/yanxu_harness/context/compaction.py) 的 `SummaryCompactor.compact`，基线约 319–325 行。

当前实现捕获 `asyncio.CancelledError` 后返回确定性投影：

```python
except asyncio.CancelledError:
    return self._baseline(baseline, "the summary request was cancelled; ...")
```

影响：`DefaultHarness._run_task` 通过 `asyncio.wait_for` 实现运行期限。期限在摘要请求期间到达时，wait_for 取消驱动任务；上面的分支把取消转换成成功返回，Runner 随后继续派发主模型，wait_for 得到正常结果，而不是超时。

实际复现：

```text
max_runtime_seconds = 0.03
摘要适配器正常情况下等待 3 秒
summary_calls = 1
main_model_calls = 1
最终 status = completed
最终 error_code = null
```

预期：外部运行期限触发后不再派发主模型，运行按既有契约返回 `usage_limited / max_runtime_exceeded`。用户取消同样应保留取消语义。

修复要求：

1. 区分摘要自身的超时与调用者/运行级取消。摘要自身的 `TimeoutError` 可有界回退；外部 `CancelledError` 在必要清理与开销记录后继续向上传播。
2. 保留已开始摘要调用的 usage/未知开销，不因为取消而把额度退回；不把取消解释为摘要成功。
3. 清理辅助请求任务，不留下继续发布结果或写缓存的悬挂任务。
4. 核对直接调用控制器、显式 Harness.cancel、运行期限和摘要自身超时四种路径，不仅测试适配器主动抛异常的替身。

新增回归至少包含：

- 摘要正在等待时，取消 `prepare_async` 任务：取消向上传播。
- 真实 `DefaultHarness(max_runtime_seconds=...)` 在摘要等待期间超时：主模型调用数 0，返回正确限额原因。
- 用户取消期间：主模型调用数 0，结果为 cancelled。
- 仅摘要自身超时且主运行仍有效：允许按配置回退，保留原文与准确开销状态。

## R2 · P2：摘要额度存放在共享 compactor 上，独立运行互相扣减

位置：[context/controller.py](../src/yanxu_harness/context/controller.py) 的 `prepare_async`，以及 [context/compaction.py](../src/yanxu_harness/context/compaction.py) 的 `usage` / `restore`。

`SummaryBudget` 声明是每个逻辑运行的绝对预算，但 `ContextController` 持有一个 `SummaryCompactor`，后者只有一个 `self.usage`。新运行没有 prior 时，`restore(None)` 得到 0 次；因小于已有次数而被忽略，于是沿用上一运行额度。

同一控制器，两个独立 scope，分别允许摘要 1 次，实际结果：

```text
run-a：实际调用摘要 1 次，报告 calls = 1
run-b：实际调用摘要 0 次，报告 calls = 1
run-b fallback = "the summary budget of 1 call(s) for this run is spent"
```

影响：新任务失去自己的摘要机会，并将其他运行的开销写进自己的 context_state。并发运行和交错恢复也会共享这个可变计数。

修复要求：

1. 摘要 usage 由逻辑运行身份隔离，不能由部署级控制器的单个计数承担。复用每个 run 已持久保存的 context_state，或者建立明确的运行级 compaction session。
2. 不要仅在每次 prepare 开头清零全局 usage；那会让同一运行多轮/恢复重新获得额度，也无法解决并发。
3. 计量恢复校验 scope，不能以“计数取最大值”代替身份核对。
4. 同一运行的并发准备有明确预算预留/结算规则；缓存可共享，但不能因此共享每个运行的花费计数。
5. 缓存/运行对象清理不能使未完成逻辑运行获得新额度；已确认花费与未知花费均需保留。

新增回归至少包含：

- 同一控制器依次运行 A、B：每个独立运行各可调用一次，各记录自己的 usage。
- A、B 并发准备：互不扣减，彼此输出与事实不串。
- 同一 run 下一轮与新 attempt 恢复：已用额度保留，不因重新构造对象而重置。
- 高已用次数的 run 与低已用次数的 run 交错恢复：不互相污染。

## 可直接运行的基线复现

在仓库根目录运行。只使用脚本化模型、现有测试夹具和内存运行存储，不调用外部模型：

```bash
.venv/bin/python - <<'PY'
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path.cwd() / 'src'))
sys.path.insert(0, str(Path.cwd() / 'tests'))
import test_context_summary as fixture
from yanxu_harness.context import (
    ContextController, ContextWindow, HeuristicTokenEstimator, SummaryBudget,
)
from yanxu_harness.core.types import AssistantMessage, StopReason, TextContent
from yanxu_harness.runtime import (
    DefaultHarness, Principal, RunContext, RunIdentity, RunRequest,
)

def controller(summary):
    return ContextController.compose(
        estimator=HeuristicTokenEstimator(), converter=fixture.real_convert,
        window=ContextWindow(limit_tokens=fixture.TIGHT, output_reserve=16),
        summarizer=summary,
        summary_budget=SummaryBudget(max_calls=1, timeout_seconds=5),
    )

class AnswerStream:
    def __init__(self): self.calls = 0
    async def __call__(self, *args, **kwargs):
        self.calls += 1
        return self
    def __aiter__(self): return self
    async def __anext__(self): raise StopAsyncIteration
    async def result(self):
        return AssistantMessage(
            content=[TextContent(text='done')], stop_reason=StopReason.STOP,
        )

async def main():
    summary = fixture.RecordingSummarizer()
    shared = controller(summary)
    a = await shared.prepare_async(
        fixture._Payload(fixture.history()), scope='n/s/run-a',
    )
    after_a = summary.calls
    b = await shared.prepare_async(
        fixture._Payload(fixture.history()), scope='n/s/run-b',
    )
    print(json.dumps({
        'probe': 'independent_run_budgets',
        'actual_a': after_a,
        'actual_b': summary.calls - after_a,
        'reported_a': a['context_state']['summary']['calls'],
        'reported_b': b['context_state']['summary']['calls'],
        'fallback_b': b['context_state']['summary'].get('fallback'),
    }, ensure_ascii=False))

    delayed = fixture.RecordingSummarizer(delay=3)
    stream = AnswerStream()
    harness = DefaultHarness(
        stream_fn=stream, context_controller=controller(delayed),
        max_runtime_seconds=0.03,
    )
    ctx = RunContext(
        identity=RunIdentity(
            namespace='n', session_id='s', run_id='deadline-probe', attempt_id='a1',
        ),
        principal=Principal(subject='operator'),
    )
    outcome = await asyncio.wait_for(harness.start(
        RunRequest(prompt='finish', messages=fixture.history()), ctx,
    ), timeout=2)
    print(json.dumps({
        'probe': 'deadline_during_summary',
        'summary_calls': delayed.calls,
        'main_model_calls': stream.calls,
        'status': outcome.status.value,
        'error_code': outcome.error_code,
    }, ensure_ascii=False))

asyncio.run(main())
PY
```

先把这些观察转成有明确期望的红测，再实施修复。保留现有单一 Runner、Gateway 账本、原始历史与确定性投影契约；修复后运行相关上下文/运行测试以及本地完整门禁，并更新交付记录中的取消与预算隔离结论。
