# Harness 扩展 E0–E3：阶段进度与证据

更新：2026-10-21。状态：**EXT-R1–R7 全部独立复验关闭；Python 3.12 源码及安装验证通过；AC38（Python 3.10）BLOCKED，E3 部分验证**。
当前分支候选版本：**`0.4.0a1`**。本轮新版本验证见[候选记录](release-0.4.0a1.zh-CN.md)；下文既有验收保留其原 `0.3.0a1` 版本与环境。
最新独立证据：[第三轮复验 §12](extensions-independent-review.zh-CN.md)。
主方案：[E0–E3](extensions-implementation-plan.zh-CN.md)。交接：[DeepSeek](extensions-deepseek-handoff.zh-CN.md)。

## 编制基线

- 源码：`9aed198872916c9b3d1496a4647ed24875364ef4`，版本 `0.3.0a1`。
- 编制从干净 main 开始；计划工作在 `codex/extensions-plan`，只改文档与生成清单。
- 已核对 ToolDefinition/ToolRegistry、SkillProvider、MCP manager/adapter、DefaultHarness 的指纹/恢复比较和 guard 接线。
- 本次没有实现扩展运行时、运行扩展功能测试或调用外部模型。
- 方案编制的链接/验收编号/清单检查在交付时记录；旧 MCP 数字不计作扩展验收通过。

方案静态核对：四份文档的 **40 个本地链接**全部可解析；代码围栏成对；EXT-AC01–40
连续且唯一，E0 的 01–08、E1 的 09–18、E2 的 19–31、E3 的 32–40 分段一致。
本次 `git diff --check` 通过。以下表格随后由实施记录更新。

文档交付检查：`.venv/bin/python -m pytest -q tests/test_public_api.py tests/test_build_scripts.py
tests/test_public_snapshot_whitelist.py` → **20 passed**。这是既有公开 API、构建与文档清单检查，
不计入 EXT-AC 的功能实现证据。`SNAPSHOT.json` 用导出脚本生成。

## 阶段状态

| 阶段 | 当前状态 | 退出范围 |
| --- | --- | --- |
| E0 清单与绑定合同 | **完成** | EXT-AC01–08 |
| E1 原子能力装配 | **完成** | EXT-AC09–18 |
| E2 生命周期与真实恢复 | **R1–R7 全部独立复验关闭** | EXT-AC19–31；AC22 借用绑定子项 PASS |
| E3 示例、文档与发行门禁 | **Python 3.12 安装门禁通过；部分验证，AC38 BLOCKED** | EXT-AC32–40；wheel/sdist × base/extra 四组合通过 |

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

## 实施记录（2026-10-20，pi）

### 接手基线

- 分支：`codex/extensions-implementation`，从 `main`（`077d91b`）新建；
  方案文档以 `f66d112` 带入，未修改 `main`。
- 工作树：干净；包版本 `0.3.0a1`（`pyproject.toml` 与
  `yanxu_harness.__version__` 一致）。
- 解释器：`.venv/bin/python` = Python 3.12.13，运行时从
  `src/yanxu_harness/__init__.py` 导入（editable install，`pip show` 版本
  `0.3.0a1`）；`.venv-mcp/bin/python` 同为 3.12.13。
- Python 3.10：本机没有真实解释器（`python3.10` 不存在，pyenv 仅有 3.12.13），
  EXT-AC38 暂记 **BLOCKED**，不缩验收条件。
- CodeGraph：`.codegraph` 索引 247 文件 / 7,851 节点 / 30,062 边，`codegraph sync`
  已是最新；已用 `impact` 核对 `ToolGateway.invoke`（24 个受影响符号）与
  `DefaultHarness.fingerprints`（23 个受影响符号）的回归面。
- 编译基线：方案编制提交 `ad2ef0a`，源码基线 `9aed198`；实际实施合到
  `main@077d91b`，包含其后的 Windows CI/发行修复。

### E0 记录

**RED。** 先写 `tests/extensions/test_e0_manifest.py`、`test_e0_plan.py`、
`test_e0_isolation.py`，运行 `.venv/bin/python -m pytest -q tests/extensions`
→ 退出码 **2**，三个模块均 `ModuleNotFoundError: No module named
'yanxu_harness.extensions'`。此时没有改动任何生产代码。

**GREEN。** 新增 `src/yanxu_harness/extensions/{errors,types,canonical,manifest,plan,__init__}.py`：
严格 JSON/映射解析、能力/代次/ID/limits 校验、不可变 JSON 冻结、纯预检、
canonical 计划摘要与有界只读报告。运行同一命令 → **52 passed**，退出码 **0**。

**影响面回归。** `.venv/bin/python -m pytest -q tests/test_import_fence.py
tests/test_public_api.py tests/test_python_floor.py` → **20 passed**，退出码 **0**。

阶段边界：

- EXT-AC01/02/03/05/06/07/08 已有行为测试。
- EXT-AC04 已在 E0 覆盖“清单原始 dict/list 被外部修改不改变已解析计划”与
  `freeze_json` 的不可变语义；工厂返回 dict/callable schema 的部署级冻结在 E1
  发布路径完成并补同一 AC 的部署断言。
- 公开类型只依赖标准库；`mcp_tool_factory` 仍为惰性入口，E3 前不在 `__all__`。
- Python 3.10 未实跑，AC38 仍 BLOCKED。

### E1 记录

**RED→GREEN（装配）。** 新增 `tests/extensions/test_e1_assembly.py` 后首次运行
`.venv/bin/python -m pytest -q tests/extensions/test_e1_assembly.py` → **8 passed,
2 failed**（退出码 1）：一处是深冻结把 JSON array 变成 tuple，测试用 Python dict
相等比较；一处是两个扩展按 ID 排序后失败项先被打开，测试对“第一项已成功”的
假设不成立。修正测试断言（按 JSON 比较、调整扩展 ID 顺序）后 → **10 passed**。

**RED→GREEN（skills）。** `test_e1_skills.py` 首轮有一个 `ExtensionLimits` 的
内联导入写法问题；修正后 `.venv/bin/python -m pytest -q
tests/extensions/test_e1_skills.py` → **8 passed**。覆盖四接口合同、metadata 只列
一次/正文与资源惰性、metadata 总量、路径保护、跨 scope 不泄漏、源指纹变化使部署
失效、host/extension 同键冲突、skill hints 不注册工具。

**RED→GREEN（MCP 显式入口）。** `test_e1_mcp.py` 首轮 **2 passed, 2 failed**：
缺 extra 被包装成 `ExtensionOpenFailed`（消息保留安装提示），以及测试误用
`mcp_tool_factory(adapter=...)` 而实际入口是 `McpExtensionFactory.from_adapter`。
修正后 → **4 passed**。覆盖：base 环境只在显式打开时失败且带 extra 提示；未绑定
MCP 的本地包仍可用；借用 manager 必须显式声明所有权与 namespace；适配器 guard
与扩展失效 guard 组合、不被覆盖。

**阶段总运行。** `.venv/bin/python -m pytest -q tests/extensions` → **74 passed**，
退出码 **0**；`tests/test_import_fence.py tests/test_public_api.py
tests/test_python_floor.py` → **20 passed**，退出码 **0**。

实现落点：`extensions/assembly.py`（两阶段私有暂存、最终一次发布、失败全体回滚）、
`registry.py`（只读发布视图）、`skills.py`（组合 SkillProvider）、
`lifecycle.py`（失效 token、owner、有界逆序关闭）、`mcp.py`（惰性 MCP 工厂）。
`ToolGateway` 仅增加只读 `registry` 属性；执行路径的通用 guard 接线留到 E2。

AC 状态：AC09/10/11/12/13/14/15/16（本地写工具）PASS；AC17 PASS；AC18 的
别名/schema/one-shot/中立结果/真实 MCP 调用证据留到 E3 的 extra 门禁，
当前只验证 guard 组合。AC04 的工厂 dict/callable schema 冻结已在 E1 覆盖。

### E2 记录

**生命周期 RED→GREEN。** `tests/extensions/test_e2_lifecycle.py` 首轮 **6 passed,
1 failed**（退出码 1）：测试辅助工厂没有接受 `contribution` 关键字；修正后
**7 passed**。覆盖：owner task 打开/关闭成功、重复关闭只释放一次；另一个 task
关闭被 `ExtensionOwnerMismatch` 拒绝且原 state 不变；另一个真实线程的 loop 关闭被
拒绝、原 owner 随后正常关闭；startup timeout 与取消都回收失败工厂；逆序关闭、
一项抛错仍清理其余项；A namespace 关闭不影响 B；借用 MCP manager 不被部署关闭。

**撤权 RED→GREEN。** `test_e2_revocation.py` 首轮 **2 passed, 3 failed**：SQLite
ledger 没有测试假后端的内存计数属性，改为读 `all_operations()`。修正后
**5 passed**，随后补 skill 视图撤权 → **6 passed**。覆盖：`invalidate` 后直接
`ToolGateway.invoke` 与 `core_tool` 都在校验/审批/admit 前拒绝、零 intent、
零 handler；原 MCP/目录 guard 仍执行且不被覆盖；在途调用返回真实 UNKNOWN
receipt、之后的新调用零新 intent；部署摘要进入实际比较的 `config`，原
model/system prompt/host config 维度仍单独可见。

**恢复 RED→GREEN。** `test_e2_recovery.py` 首轮多个失败（测试 helper 把
`clock=None` 显式传给 `DefaultHarness`、部分批测试未开启 auto_write、跨
`asyncio.run` 关闭被 owner 正确拒绝），逐个修正测试/辅助构造后 **6 passed**：

- 真实 SQLite Harness 暂停后，扩展 `2.0.0` 恢复被 `arrangement_drift` 拒绝
  （drifted 含 `config`），零模型请求、零工具效果；恢复 `1.0.0` 并批准后完成，
  handler 只运行一次。
- 真新进程重建相同逻辑部署：部署摘要一致，进程内只有一处远端效果，已结算
  回执被复用语义保持，模型请求恰好一次。
- `child_result`、`RUNNING` orphan、partial-batch 三个分支在派发前都调用部署
  binding；真实 crash fixture 下 revoked binding 拒绝且 A/B 不被重跑。
- 真扩展部署 A/B/C：子进程 A 结算、B 写入后退出、C 未发；恢复先 UNKNOWN 屏障，
  证据收敛后 C 恰好一次，A/B 不重发，账本 A/B/C 均 succeeded。
- 原 model/system prompt/host config 漂移在扩展部署下仍被真实恢复拒绝，且相同
  宿主 config 不被误判。
- 手工 `DefaultHarness` 的指纹键集合与值与改动前逐字一致。

**子任务。** `test_e2_children.py` → **2 passed**：真 `ChildCoordinator` 只授
显式扩展工具子集；越权工具和超预算 grant 被拒、不创建子任务；借用 manager/
factory 与 binding resource ref 值不进入 relation/authorization 记录。

**阶段总运行。** `.venv/bin/python -m pytest -q tests/extensions` →
**95 passed**，退出码 **0**；完整套件 `.venv/bin/python -m pytest -q tests` →
**1418 passed, 13 skipped**，退出码 **0**（此前一次仅因 `SNAPSHOT.json` 尚未刷新
失败；已用 `scripts/export_public_snapshot.py --refresh-manifest` 重新生成）。

AC 状态：AC19/20/21/22/23/24/25/26/27/28/29/30/31 均为 PASS。AC18 的真实 MCP
两协议×两传输、one-shot 与目录刷新证据，以及 AC32–40 的参考包/门禁/文档仍属 E3。

### E3 记录

**参考包与示例。** 新增 `examples/extensions/reference_pack.py`、
`examples/extensions/mcp_reference_pack.py` 与 `examples/13_extension_tools_skills.py`、
`examples/14_extension_mcp_recovery.py`。E3 首轮 `examples/14` 在 MCP extra 下失败：
MCP 工厂只返回工具，未把声明的 skill 一并贡献；为 `McpExtensionFactory` 增加显式
`skills` 参数后，示例完成一次真实远端 bump、进程内关闭、重建相同逻辑部署、批准并恢复，
输出 `status=completed / bump_count=1 / remote_calls=1 / same_deployment=true`。
base 环境输出 `status=skipped` + 安装提示，退出 0。

**E3 测试 RED→GREEN。**

- `test_e3_mcp_extension.py` 初次收集失败（示例目录不在 `sys.path`）；修正后
  **5 passed**：两代协议 × 两种传输的真实 echo/bump，以及 explode 错误、目录失效后
  guard 拒绝、`bump_then_die` 的真实 UNKNOWN 与一次远端效果。
- `test_e3_reference_pack.py` → **3 passed**；`test_e3_diagnostics.py` → **3 passed**；
  `test_e3_compat.py` → **7 passed**；`test_e3_diagnostics` 首轮中“异常 canary”用例
  的工厂没有真正携带 canary，改成显式 `RuntimeError(canary)` 后通过。
- 新增 `testing/extensions.py#check_extension_deployment_contract`，接入
  `yanxu_harness.testing`，并被参考包测试实际调用。

**独立探针。** `scripts/probe_extensions_acceptance.py` 首次运行通过，退出码 0；
覆盖冲突原子性、部分启动回滚、撤权旧引用、版本漂移、跨进程相同部署恢复和诊断
零启动/秘密 canary。

**安装门禁。** `scripts/extensions_gate.py` 首次运行 base/extra 通过，但 extra 的
独立探针退出 2：树外副本只复制了 `tests/` 与 `examples/`，没有 `scripts/`。补复制
`scripts/` 后全门禁通过：
base wheel `109 passed, 6 skipped`、extra `114 passed, 0 skipped` 且 `tests/mcp`
`93 passed, 0 skipped`、sdist `109 passed, 6 skipped`，两环境 example 13/14 与探针
均成功，导入位置为树外 `site-packages`。报告：
`build/extensions-gate/report.json`。

**最终运行。**

> 以下是独立复核前的原始 E3 记录；R1–R6 修复后的数字见下一节。

- `.venv/bin/python -m pytest -q tests/extensions` → **109 passed, 6 skipped**，退出码 0。
- `.venv-mcp/bin/python -m pytest -q tests/extensions tests/mcp` →
  **207 passed, 1 skipped**，退出码 0（唯一 skip 是 base-only 缺 extra 负例）。
- `.venv/bin/python scripts/probe_extensions_acceptance.py` → `ok: true`，退出码 0。
- `.venv/bin/python scripts/extensions_gate.py` → `ok: true`，退出码 0。
- `.venv-mcp/bin/python scripts/probe_mcp_independent_review.py` → `passed: 4, failed: 0`；
  `probe_mcp_failed_refresh.py` → `passed: true`；退出码均 0。
- 完整套件 `.venv/bin/python -m pytest -q tests` → **1432 passed, 19 skipped**，
  退出码 0（刷新 `SNAPSHOT.json` 后）。

**AC 状态。** EXT-AC01–37、39、40 PASS（逐项证据见
[交付报告](extensions-delivery-report.zh-CN.md)）；EXT-AC38 **BLOCKED**：本机没有
Python 3.10 解释器，未用静态解析或锁文件替代。E3 因此保持“候选/部分验证”，不宣称
全解释器完成。

**交付边界。** 保留 `0.3.0a1`；未上传包索引、未推 tag、未合 main、未切换宿主 wheel。
新增文档：`docs/extensions.zh-CN.md`、`docs/extensions-adr.zh-CN.md`、
`docs/extensions-delivery-report.zh-CN.md`；README 中英文、contract/adapters/recovery/
verification 与 `SNAPSHOT.json` 已更新。后续 `0.4.0a1` 决策留给独立复核。


### 独立复核修复记录（EXT-R1–R6）

独立复核在 `6cd2b41` 上复现 4 项 P1、2 项 P2；本轮按同一验收门槛修复，未删除或
放宽任何条件。详细复现见[独立复核与修复交接](extensions-independent-review.zh-CN.md)。

**R1 审批 await 后撤权仍执行。** `ToolGateway.invoke` 在审批 `get/supersede/put`
返回后、构造 intent 前再次执行同一 dispatch guard；已经准入的调用不重查，保留真实
receipt/UNKNOWN。新增两个真实 Gateway 回归：阻塞已批准审批的 `get`、阻塞新审批的
`put`，关闭后均为 `denied`、零 intent、零 handler 效果。

**R2 直接 Gateway 绕过部署 scope。** 组合 guard 增加部署 namespace/授权 scope
校验；scope 从宿主 `RunContext` 读取，工具参数不能自行声明，缺失/不匹配都拒绝。
MCP 原目录 guard 仍在其后执行。新增直接 Gateway 的 namespace/scope/缺失 scope/
正确 scope 回归，以及 MCP 错误 scope 在远端调用前拒绝且远端效果为 0 的回归。

**R3 `local_tools` 无撤权守卫。** `_freeze_local` 与扩展工具共用部署 guard（保留
宿主原 version）。新增关闭后旧 registry、直接 Gateway 和 `core_tool` bridge 均
`denied` 的回归。

**R4 宿主 config 覆盖合成指纹。** 扩展路径把宿主 `fingerprints["config"]` 作为
`config_payload["host"]` 输入，合成后再写回 `config`，不再允许宿主值整体覆盖。
真实 SQLite 回归：model-a + 固定 host config 暂停、批准；model-b + 同一 host config
恢复必须 `arrangement_drift`，零模型请求、零效果；model-a + 同一 host config 可完成。

**R5 失败工厂清理未进 CloseReport。** 失败工厂的 close 结果与先前成功工厂合并进
唯一 `CloseReport`；成功、失败、超时、缺 close 四条路径均记录。新增失败工厂 close
成功/失败/超时/缺 close、已有成功工厂、以及失败项资源计数回归。

**R6 CloseReport 复制异常原文。** 公开失败项只保留有界扩展 ID 与异常类型/稳定
code；只有包内明确标记 `public_safe` 的辅助错误（MCP 缺 extra 固定安装提示）保留
原文。`failed` 字符串长度有界，不复制异常 message。新增 close canary、失败启动
canary、factory `HarnessError` canary、factory `ExtensionRefused` canary、超长异常
类名和缺失 close 回归。

**修复后实际运行。**

- `.venv/bin/python -m pytest -q tests/extensions` → **122 passed, 7 skipped**，exit 0。
- `.venv-mcp/bin/python -m pytest -q tests/extensions tests/mcp` →
  **221 passed, 1 skipped**，exit 0。
- `.venv/bin/python scripts/probe_extensions_independent_review.py` →
  **6/6 passed**，exit 0。
- `.venv-mcp/bin/python scripts/probe_extensions_independent_review.py --with-mcp` →
  **7/7 passed**，exit 0（含真实 MCP 错误 scope 零远端效果）。
- `.venv/bin/python -m pytest -q tests` → **1445 passed, 20 skipped**，exit 0。
- `scripts/extensions_gate.py` → `ok: true`；base/sdist 122 passed/7 skipped，extra
  128 passed/0 skipped + 93 MCP passed/0 skipped；树外 `site-packages` 导入、examples
  13/14、原验收探针均通过；Python 3.10 仍 **BLOCKED**。
- `scripts/probe_extensions_acceptance.py`、MCP R1–R4（4/4）、R5（true）均 exit 0。

状态：**R1–R6 已修复，待独立复核**。未升 `0.4.0a1`，未上传、未推 tag、未合 main、
未切换宿主 wheel。

### 第二轮独立复核（基线 `d4b02a4`）

原 EXT-R1–R6 独立探针 base **6/6**、extra **7/7** 通过，六项关闭。
扩展 + MCP **221 passed / 1 skipped**；基础全量 **1445 passed / 20 skipped**；
原扩展验收探针及 MCP R1–R5 探针全部通过。

新增 **EXT-R7 / P1**：已有 adapter 的提前返回绕过借用资源绑定校验。
真实 stdio 独立探针 **1/5 passed / 4 failed**：错误 namespace、错误授权 scope、
错误 config revision、缺借用声明均产生远端效果 1；完全匹配正例通过，宿主 manager 保持 live。
详见[独立复核 §10 与修复交接](extensions-independent-review.zh-CN.md)。

当前 R7 待修复，AC22 借用绑定子项 FAIL，AC38 仍 BLOCKED；整体验收尚未通过。


### 第二轮独立复核修复记录（EXT-R7）

第二轮复核确认 R1–R6 关闭，新增 R7：`McpExtensionFactory.from_adapter()` 的 adapter
提前返回绕过借用资源绑定校验。修复在 `open()` 返回任何 contribution 之前统一执行
`_validate_resource_binding()`：

- 外部 adapter/manager/connection/snapshot 必须显式声明 `borrowed_namespace`；
- 配置、目录快照、连接上的实际 authorization scope/config revision 必须与部署
  binding 一致，三处都提供的逐项比较；
- 声明 manager 时必须与 adapter 的实际 manager 同一对象；缺少或无法核对的借用拒绝；
- factory 在本次 `open()` 中自建的 manager 不需要外部 namespace 声明，自有 manager
  关闭合同不变。

新增真实 MCP 回归 `test_a_borrowed_adapter_must_match_its_resource_binding`，覆盖错误
namespace、错误 scope、错误 revision、缺借用声明、manager 不一致五个拒绝场景和
完全匹配正例；拒绝均零远端效果，正例效果一次，部署关闭后宿主 manager 仍 live。

**R7 修复后实际运行。**

- `.venv/bin/python -m pytest -q tests/extensions` → **122 passed, 8 skipped**，exit 0。
- `.venv-mcp/bin/python -m pytest -q tests/extensions tests/mcp` →
  **222 passed, 1 skipped**，exit 0。
- `.venv-mcp/bin/python scripts/probe_extensions_borrowed_adapter.py` →
  **5/5 passed**，exit 0。
- 原独立探针 base **6/6**、extra **7/7**，exit 0。
- `.venv/bin/python -m pytest -q tests` → **1445 passed, 21 skipped**，exit 0。
- `scripts/extensions_gate.py` → `ok: true`；base/sdist 122/8，extra 129/0 + 93 MCP/0；
  Python 3.10 仍 **BLOCKED**。

状态：**R7 已修复，待第二轮独立复验**。未升 `0.4.0a1`、未上传、未推 tag、未合 main、
未切换宿主 wheel。

### 第三轮独立复验（2026-10-21，基线 `ccd5543`）

**EXT-R1–R7 全部关闭**。R7 独立探针 **5/5**，四个负例零远端效果，匹配正例效果一次；
宿主 manager 均保持 live。原独立探针 base **6/6**、extra **7/7**，MCP R1–R5 探针全部通过。
源码 extra 套件 **222 passed / 1 skipped**，基础全量 **1445 passed / 21 skipped**。

独立构建 wheel/sdist 并完成 base/extra 四组合安装验证；base 各 **122 passed / 8 skipped**，
extra 各 **129 扩展 + 93 MCP passed / 0 skipped**，两个 extra 的独立探针均 **7/7 + 5/5**。
两种安装产物各 119 个 Python 源文件匹配 checkout。完整证据见[独立复核 §12](extensions-independent-review.zh-CN.md)。

当前 Python 3.12 验证通过，本轮未发现新增阻断缺口。AC38（Python 3.10）仍 BLOCKED，E3 保持部分验证。

### 分支升版（2026-10-21，0.4.0a1）

按用户要求将包元数据、运行时版本、当前指南和兼容断言同步为 **`0.4.0a1`**。
升版后基础全量 **1445 passed / 21 skipped**，针对性检查 **30 passed**；
新版本 wheel/base、wheel/MCP extra、sdist/base 的安装 gate 均通过，运行时与安装元数据版本一致。
实际命令、产物和仍 BLOCKED 的 AC38 见[候选版本记录](release-0.4.0a1.zh-CN.md)。
