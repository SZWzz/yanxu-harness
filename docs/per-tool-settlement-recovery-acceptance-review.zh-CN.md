# 逐工具结算与恢复：独立验收复核

日期：2026-10-16。核对提交：`2c8a324`。开始复核时工作树干净。

结论（复核当次）：**T0–T6 主体已接线，现有门禁通过；额外探针复现三项缺陷，尚不能全部验收。** 本次只增加验收记录，没有修改生产实现。

后续：三项缺陷已按 R1–R3 修复，见文末[第 7 节](#7-r-线修复记录2026-10-16)。本节及第 2–6 节保留的是**修复前**的事实与复现，不改写。

下一轮统一实施入口：[恢复缺陷修复、运行诊断与任务评测计划](runtime-diagnostics-evaluation-implementation-plan.zh-CN.md)。三项缺陷分别对应 R1–R3，关闭后再衔接诊断与评测交付。

实施依据：[逐工具结算与恢复实施计划](per-tool-settlement-recovery-implementation-plan.zh-CN.md)。本轮包含 `5cd274f`、`efb4ae9`、`2f0608a`、`8b60eed`、`2590dcf`、`e2191f1`、`2c8a324` 等提交。最后一次提交已补上真实调用链的 model action 身份，不能把此前已修的问题重复计入本次缺陷。

## 1. 独立执行的验证

| 验证 | 本次实际结果 |
| --- | --- |
| 部分提交、部分恢复、兼容、账本、示例及摘要隔离专项测试 | **55 passed in 1.87s** |
| 完整离线门禁中的包测试 | **952 passed / 1 skipped in 42.99s** |
| 完整离线门禁 | **6/6 通过**；含安装 extras、冷导入、wheel/sdist 源码树外安装与示例、Markdown 链接和来源声明 |
| 三项额外探针 | 下述三种不符合预期行为均可复现；真实子进程退出、新 SQLite 连接恢复，使用脚本化模型 |

专项命令：

```bash
.venv/bin/python -m pytest -q tests/test_runtime_partial_settlement.py tests/test_runtime_partial_recovery.py tests/test_runtime_recovery_compat.py tests/test_tools_ledger_recovery.py tests/test_examples_partial_recovery.py tests/test_context_summary_isolation.py
.venv/bin/python scripts/ci_check.py --offline --wheelhouse build-wheelhouse
```

现有测试已覆盖并通过：兄弟调用未结束时单个观察可落盘、原子准入与回执冲突、A/B/C 真实进程恢复、receipt 已提交但 Run 未更新、全部观察已提交但尚未整批收尾、成功/失败的直接业务核对，以及部分旧格式兼容。下面的缺陷来自这些场景之外的组合边界，不能因门禁全绿忽略。

本次不增加真实模型质量、远程 CI 或其他 Python/平台的验证结论。

## 2. PTR-R1 · P1：orphan 恢复用当前配置重建旧指纹，漂移检查失效

位置：[default_harness.py](../src/yanxu_harness/runtime/default_harness.py) 的 `_recover_partial_batch`，基线 1377–1385 行；相关转换为 [batch.py](../src/yanxu_harness/runtime/batch.py) 的 `batch_from_snapshot`。

恢复通过 `batch_from_snapshot(..., fingerprints=self.fingerprints(), exposure_manifest=self.exposure_manifest())` 创建批次，接着 `_assert_arrangement` 再用同一个当前部署的值校验。这比较的是“当前与当前”，没有冻结的旧值可供比较。

实测：真实子进程在 A 已完成、B 已发起时退出；独立业务证据先确认 B；新 Harness 使用与原部署不同的 `system_prompt`，其他工具不变。结果：

```text
instructions_changed = true
handler trace = notes.a, notes.b, notes.c
main_model_calls = 1
status = completed
error = null
```

预期：在剩余 C 派发前明确拒绝 config/instructions 漂移，C 不执行，主模型不调用。本探针独立确认的是 system_prompt 漂移；tools、policy、exposure 的同类边界还需加入修复回归矩阵。

修复要求：

1. 首次派发前持久保存该批次所绑定的原始配置/工具/指令/策略/可见清单事实；恢复从可信旧记录读取，而非以当前值补齐旧值。
2. 新快照与 recorded batch 的转换保持同一来源，旧记录缺少足够事实时采用明确兼容/拒绝规则。
3. 领取 lease 后重读记录，并基于该最新记录重新取快照、核对身份、取消与部署安排；不拿领取前的旧对象开始执行。
4. 回归至少覆盖真实 RUNNING_BATCH orphan 的 instructions/config/tools/policy/exposure 变化，以及未变化时恢复正常。不能只测试已有 SUSPENDED batch 的漂移拒绝。

## 3. PTR-R2 · P1：恢复剩余 handler 不在受限驱动任务内，运行期限失效

位置：[default_harness.py](../src/yanxu_harness/runtime/default_harness.py) 的 `_run_recovered_batch`，基线 1423–1453 行；实际 handler 在 `_resolve_blocking_calls` 的 1574–1580 行执行。

恢复先 `await _resolve_blocking_calls(...)` 执行剩余工具，执行完成后才通过 `_run_task(...)` 启动 `Runner.resume_batch`。因此 `max_runtime_seconds` 的计时与可取消 Task 只覆盖后半段，没有覆盖前面的工具执行。SUSPENDED resume 也保留了相似的手工执行分支，需要一并核对。

实测：复用真实部分批次崩溃记录，确认 B 后恢复 C；C 的异步 handler 等待 0.16 秒，Harness 声明 0.03 秒运行上限：

```text
limit_seconds = 0.03
elapsed_seconds ≈ 0.170
C = start, finished
main_model_calls = 1
status = completed
error_code = null
```

预期：期限覆盖恢复驱动及其工具等待；超限后不再发主模型，返回 `usage_limited / max_runtime_exceeded`。已开始工具的副作用按 receipt/unknown 证据保留，不能把 Task 取消直接当作业务效果未发生。

修复要求：

1. 将剩余工具执行交回同一 Runner 的批次会话，整体置于 Harness 的受限/可取消驱动任务内；不在 Runner 启动前手工跑完所有剩余 handler。
2. 正常启动、orphan 恢复和审批恢复共用执行、单调用提交与取消路径；避免再次出现两套工具调度。
3. 补真实 Harness 的恢复期限测试，以及阻塞 handler 期间 `Harness.cancel` 的任务中断测试；同时核对 reconcile/ledger 等外部 await 的有界契约。
4. 在恢复剩余多调用时，以新连接验证第一项完成、下一项阻塞期间进度已提交；不能只验证新 start 的逐调用提交。

## 4. PTR-R3 · P2：失败已在账本确认、Run 尚未更新时，恢复仍保持 uncertain

位置：[default_harness.py](../src/yanxu_harness/runtime/default_harness.py) 的 `_converged_in_ledger`，基线 1355–1357 行。

该方法只接纳 `EXECUTED` / `REPLAYED`。但 `ToolGateway.review` 对 `EffectState.FAILED` 返回 `InvocationStatus.FAILED`，因此有证明的失败回执被忽略，恢复再次调用 reconciler；如果本次没有 reconciler，就继续暂停。

实测：先把部分批次恢复为 B uncertain 的暂停；在独立操作账本中提交 B 的 failed 证据，模拟“账本收敛已提交、Run 更新前崩溃”；重建 Harness 和存储连接，不再提供 reconciler：

```text
ledger B = failed
Run status = suspended
handler trace = notes.a, notes.b
main_model_calls = 0
interaction 仍称 intent 无回执、结果未证明
```

预期：读取已确认失败，为 B 生成失败结果，不再次询问业务证据；C 按许可继续，最终批次可以完整交给模型。工具失败结果不等于整个 Run 必须失败。

修复要求：

1. 按现有业务结果分类接纳所有可证明的终结结果，至少包括 FAILED；不要只允许成功的 invocation status。
2. 不要仅依赖 `receipt.settled` 判断可继续：现有 EffectState 词汇里 UNKNOWN 也可能被计作 receipt 已落定，但业务仍需核对。
3. 成功/失败分别覆盖“ledger 已收敛，Run 未提交”的窗口；无 reconciler 时仍应恢复，有 reconciler 时断言不会重复调用。
4. 保留审批等待、unknown、账本不可用与冲突状态的保守处理。

## 5. 下一次验收

先把以下探针转换成有目标断言的红测，再修复。完成后执行新增回归、原专项测试和同一个完整门禁，记录实际数量和提交号。不要预先把 952 当作修复后的固定总数。

正确方向已经落地：调用级事实与结果可以持久保存；本轮待补的是恢复路径中的旧配置来源、统一驱动边界和失败回执分支。修复不要削弱 unknown 屏障、原子准入、稳定 operation_id、结果源顺序或旧格式拒绝策略。

## 6. 可运行复现

在仓库根目录，将下方 Python 保存为临时脚本后使用 `.venv/bin/python` 执行。只调用本地 SQLite、脚本化模型与现有故障测试夹具；每个场景使用独立临时目录，不需要真实模型服务。

```python
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path.cwd() / 'src'))
sys.path.insert(0, str(Path.cwd() / 'tests'))
import test_runtime_partial_recovery as fixture
from durable_edit import ScriptedStream, final_message, build_partial, run_context
from yanxu_harness.runtime import DefaultHarness
from yanxu_harness.storage import SqliteExecutionBackend, SqlitePersistence
from yanxu_harness.tools import EffectState, ToolReceipt


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def promote(paths, state):
    ledger = SqliteExecutionBackend(paths.operations)
    try:
        fixture.run(ledger.promote(
            ToolReceipt(
                operation_id=fixture.operation_id(fixture.B), state=state,
                result={'content': [{'type': 'text', 'text': 'confirmed earlier'}]} if state is EffectState.SUCCEEDED else None,
                detail='confirmed earlier',
            ), evidence={'source': 'audit business evidence', 'detail': 'confirmed earlier'},
        ))
    finally:
        ledger.close()


def failed_receipt(directory):
    paths = fixture.crash_in_a_partial_batch(directory)
    first, _ = fixture.rebuild(paths, ScriptedStream(final_message()), attempt='a2')
    fixture.run(first.resume(fixture.RUN_ID, {}, run_context('a2')))
    promote(paths, EffectState.FAILED)
    stream = ScriptedStream(final_message())
    second, _ = fixture.rebuild(paths, stream, attempt='a3')
    outcome = fixture.run(second.resume(fixture.RUN_ID, {}, run_context('a3')))
    emit({'probe': 'failed_receipt_after_ledger_only_convergence',
          'ledger_b': fixture.ledger_states(paths)[fixture.operation_id(fixture.B)],
          'status': outcome.status.value, 'main_model_calls': stream.calls,
          'tools': fixture.ran_tools(paths), 'interaction': outcome.pending_interaction})


def drift(directory):
    paths = fixture.crash_in_a_partial_batch(directory)
    promote(paths, EffectState.SUCCEEDED)
    stream = ScriptedStream(final_message())
    old, _ = fixture.rebuild(paths, stream, attempt='a2')
    changed = DefaultHarness(
        stream_fn=stream, tools=old.tools,
        persistence=SqlitePersistence(paths.runs), system_prompt='a changed deployment instruction',
        clock=lambda: time.time() + fixture.LEASE_PAST,
    )
    try:
        outcome = fixture.run(changed.resume(fixture.RUN_ID, {}, run_context('a2')))
        result = {'status': outcome.status.value, 'error': None}
    except Exception as exc:
        result = {'status': None, 'error': f'{type(exc).__name__}: {exc}'}
    emit({'probe': 'orphan_instruction_drift',
          'instructions_changed': old.fingerprints()['instructions'] != changed.fingerprints()['instructions'],
          'tools': fixture.ran_tools(paths), 'main_model_calls': stream.calls, **result})


def runtime_limit(directory):
    paths = fixture.crash_in_a_partial_batch(directory)
    promote(paths, EffectState.SUCCEEDED)
    called = []
    async def slow_c(context, args):
        called.append('start')
        await asyncio.sleep(0.16)
        called.append('finished')
        return {'content': [{'type': 'text', 'text': 'C completed'}]}
    stream = ScriptedStream(final_message())
    wired, _ = build_partial(paths, stream, attempt='a2', handlers={'notes.c': slow_c},
                             clock=lambda: time.time() + fixture.LEASE_PAST)
    harness = DefaultHarness(
        stream_fn=stream, tools=wired.tools, persistence=SqlitePersistence(paths.runs),
        system_prompt='be brief', max_runtime_seconds=0.03,
        clock=lambda: time.time() + fixture.LEASE_PAST,
    )
    start = time.monotonic()
    outcome = fixture.run(harness.resume(fixture.RUN_ID, {}, run_context('a2')))
    emit({'probe': 'runtime_limit_while_recovering_c', 'limit_seconds': 0.03,
          'elapsed_seconds': round(time.monotonic() - start, 3),
          'c_calls': called, 'main_model_calls': stream.calls,
          'status': outcome.status.value, 'error_code': outcome.error_code})


with tempfile.TemporaryDirectory(prefix='yanxu-per-tool-review-') as temp:
    base = Path(temp)
    failed_receipt(base / 'failed')
    drift(base / 'drift')
    runtime_limit(base / 'deadline')
```


## 7. R 线修复记录（2026-10-16）

本节只新增修复后的证据；上文第 2–6 节是修复当次（`2c8a324`）的原始记录，未改动。

| 缺陷 | 修复提交 | 修复后的实际行为 |
| --- | --- | --- |
| PTR-R1 漂移检查失效 | `dccf06b` | 部署指纹在**首次派发之前**写入 `ExecutionState.arrangement`（schema 3，1/2 仍可读），恢复从该已提交基线重建批次并与当前部署比较；无基线的旧记录**明确拒绝**而不是用当前值补齐；orphan 与 suspended 两个入口都覆盖 instructions/config/tools/policy，且租约领取后重读记录、把租约直接交给恢复（不再释放再领取） |
| PTR-R2 期限不覆盖恢复 | `e652656` | 分类、reconciler、剩余调用重派发与后续模型请求处于**同一次**受限可取消的驱动内；剩余调用交给同一个 `Runner.resume_batch` 的批次会话（`_resolve_blocking_calls` 已删除）；暂停在受限驱动**之外**提交，超时不丢已证明的证据 |
| PTR-R3 失败回执被忽略 | `1ee55c9` | 按业务分类接纳所有已终结结果（executed/replayed/failed/denied/invalid_args/cancelled）；`unknown` 与 `deferred` 仍阻塞 |

修复过程中另外发现并修复的两个真实缺陷（都不是探针里的三项，单独记录）：

* `ToolBatchSession` 在消费者已停止时关闭会**永久阻塞**在 `queue.put(None)`：顺序批次的截止/取消恰好会走到这条路，结果是运行无法停止（R2 之前的探针没走到这里，因为那时 handler 在受限驱动之外）。现在 `aclose` 之后以非阻塞方式投递结束标记。
* core 丢弃了工具自己声明的 `is_error`（`_execute_prepared` 恒返回 `False`），因此被拒绝的调用会以“成功”到达模型。现在 `result_is_error` 保真。

修复后的实测（同一台机器、本检出）：

| 验证 | 结果 |
| --- | --- |
| 三项原探针的目标断言（`tests/test_runtime_recovery_gaps.py`） | **15 passed**：三项探针 + 四个漂移维度 × 两个入口 + 未知/延迟回执仍保守 + 期限、取消、第二连接可读的恢复提交 |
| 完整套件 | **967 passed / 1 skipped / 0 failed** |
| 完整离线门禁 | **6/6 通过**（含源码树外安装后运行 examples） |

R 线仍未验证/未支持的部分（如实登记）：

* 只声明了 policy 指纹的宿主才能检出 policy 漂移；没有声明 policy 指纹的部署在 `resolve_resume` 里被当作“无此维度”，这是既有语义，不是本次放宽。
* `SqliteExecutionBackend.promote` 只接受未证明的行（open/unknown）：reconciler 对 `deferred` 回执给出的证据会被拒绝，而不是被应用。deferred 仍需要宿主按网关提出的决定回答；本build不额外放行。
* 真实模型/远程 CI/其他平台与解释器仍未在本轮执行（见 [verification.md](verification.md) 的未执行项）。
