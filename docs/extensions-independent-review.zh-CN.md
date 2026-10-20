# Harness 扩展 E0–E3：独立复核与修复交接

日期：2026-10-20（Asia/Shanghai）。基线：`codex/extensions-implementation@6cd2b41`，
开始时工作树干净，版本 `0.3.0a1`。依据：[实施方案](extensions-implementation-plan.zh-CN.md)、
[ADR](extensions-adr.zh-CN.md)、[实施方交付报告](extensions-delivery-report.zh-CN.md)。

**最新结论（第二轮，`d4b02a4`）：EXT-R1–R6 已独立复验关闭；新增 EXT-R7 / P1（借用 adapter 绕过资源绑定），整体验收仍未通过。详见 §10。**

**首轮结论（`6cd2b41`）：独立验收未通过，复现 4 项 P1、2 项 P2。** 以下 §1–§8 保留首轮历史。
本次只新增探针、复核文档、状态说明和生成清单；没有修复生产代码或更换宿主发行物。

## 1. 已执行的验证

环境：macOS arm64 / Python 3.12.13；源码导入实际来自本 checkout 的 `src/yanxu_harness`。
MCP 环境使用现有 `.venv-mcp`。以下为独立执行结果，不以实施方记录代替。

| 检查 | 本轮结果 |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/extensions` | 109 passed / 6 skipped，exit 0 |
| `.venv-mcp/bin/python -m pytest -q tests/extensions tests/mcp` | 207 passed / 1 skipped，41.30s，exit 0 |
| `.venv/bin/python -m pytest -q` | 1432 passed / 19 skipped，100.15s，exit 0 |
| 实施方 `probe_extensions_acceptance.py` | 现有场景全部 ok，exit 0 |
| 新独立探针，base | **0 passed / 6 failed，exit 1** |
| 新独立探针，`--with-mcp` | **0 passed / 7 failed，exit 1**；额外场景验证同一 scope 缺口的真实远端效果 |

前述 skip 单列，不计作功能通过。新探针失败均为行为断言不成立，没有缺模块、收集失败或 fixture 报错。
每项先用临时脚本复现，再用[仓库内独立探针](../scripts/probe_extensions_independent_review.py)复跑。
使用真实 Gateway、审批、SQLite、Harness 恢复和本机 stdio MCP 子进程；模型回答由脚本提供，
只使用合成秘密标记，不访问外部模型或真实凭据。

本轮没有重跑 wheel/sdist 安装 gate、Windows 或 Python 3.10。当前未找到 `python3.10` 或 `uv`，
AC38 继续 **BLOCKED**。现有 Python 3.12 的 MCP 正常关闭用例通过，不据此推断 Python 3.10 的 owner 行为。

交接文件写入后，公开 API / 构建脚本 / 快照白名单检查 **23 passed**；本次四份文档的
**25 个本地链接**有效，`git diff --check` 通过，SNAPSHOT 已用导出脚本重新生成。

复跑命令（脚本 exit 1 表示仍有合同缺口；修复后应 exit 0）：

```sh
.venv/bin/python scripts/probe_extensions_independent_review.py --report /tmp/extensions-review-base.json
.venv-mcp/bin/python scripts/probe_extensions_independent_review.py --with-mcp --report /tmp/extensions-review-mcp.json
```

## 2. EXT-R1 / P1：审批 await 期间关闭，仍新准入并执行

位置：`src/yanxu_harness/tools/gateway.py:519–543`。本次新加的 dispatch guard 只在入口执行；
`_require_approval()` 会 await `get/supersede/put`，返回后没有再次检查，直接构造 intent 并调用 `_admit()`。

复现：先生成并批准真实写审批；第二次调用阻塞在审批 `get()`，此时还未准入；
在部署 owner 中完整 `aclose()`，再释放审批读取。结果为 **`executed`、写效果 1、部署 `closed`**。
这不是准入后才关闭的在途调用。违反方案 §5.1 和 AC21。

修复验收：

- 在审批等异步边界后、实际准入前重查有效性及原 guard；该窗口必须 DENIED、零新 intent、零 handler 效果。
- 同时保留准入后关闭的真实 receipt / UNKNOWN 语义；不要把所有在途写改报 FAILED。
- 补阻塞审批读取与新审批写入的真实 Gateway 回归，不能只调用 guard helper。

## 3. EXT-R2 / P1：直接 Gateway 路径绕过部署 scope

位置：`src/yanxu_harness/extensions/assembly.py:541–550`，及 Gateway guard payload。
组合守卫只检查 token 和原 guard，没有核对绑定的 namespace / authorization scope。
`_DeploymentBinding.check(context)` 只接在 Harness 上，直接 Gateway 不经过它；
Gateway 原有的检查仅要求 invocation 与 context 彼此一致。

本地复现：project-a / authorized 的部署，以 project-b context 调用写工具，或在 project-a 中用
`unauthorized-b` 调用，**两次均 executed，handler 两次执行**。
真实 MCP 复现：project-a 的部署和连接被 project-b / unauthorized-b 调用，远端 `bump` **实际写入 1 次**，
Gateway 返回 executed。下层 MCP 的 catalog guard 核对的是 adapter/config/connection 内部一致性，不能补上部署与调用者的比较。

修复验收：

- 将部署 scope 校验接入真实 Gateway/bridge 边界；校验基于宿主提供的 context，不能由工具参数自行声明。
- namespace 错误、授权 scope 错误、scope 缺失分别拒绝，零新 intent / 远端调用；正确 scope 的正例继续通过。
- 保留手工、无扩展的既有调用行为。关联方案 §§3、4.4、5.1，AC21/25 的绑定边界。

## 4. EXT-R3 / P1：合入部署的 local_tools 没有撤权守卫

位置：`src/yanxu_harness/extensions/assembly.py:515–522`。
扩展工具经过 `_bind_definition()` 注入 token guard，但 `local_tools` 只冻结 schema。

复现：把 `host.read` 作为 `local_tools` 合入同一发布 registry，保存真实 Gateway 引用；
完整关闭部署后开始一个新调用，结果仍为 **executed，handler 执行 1 次**。
这违反方案 §5.1 / AC21 对已发布旧工具和 Gateway 引用的撤权要求。

修复验收：部署发布的本地工具也组合部署 guard 和原 guard；关闭/失效后旧 registry、Gateway 和 bridge
新调用均拒绝，零 intent / handler 调用。无扩展的手工 ToolRegistry 保持原行为，不强制改版本或授权语义。

## 5. EXT-R4 / P1：宿主 config 指纹覆盖合成指纹，模型漂移恢复成功

位置：`src/yanxu_harness/runtime/default_harness.py:507`。
虽然先把 deployment、model、system prompt 和宿主 config 合成 `values["config"]`，
随后 `values.update(_extra_fingerprints...)` 又允许宿主的 `config` 键整体覆盖它。
额外的 `deployment` 键不能代替既有 arrangement 真正比较的 config。

真实 SQLite 复现：model-a + `fingerprints={"config": "fixed-host-config"}` 启动并等待写审批，批准后关闭；
重建相同部署，改用 model-b 和同一宿主 config 值恢复。前后被比较的 config 完全相同，
恢复 **completed，写效果 1 次，后续模型请求 1 次**，没有 ResumeRefused。违反 AC26；合成部署摘要也存在被覆盖路径。

修复验收：

- 扩展路径将宿主提供的 config 值作为合成输入，不能再覆盖合成结果；按现有兼容要求保留无扩展路径行为。
- 使用实际 `fingerprints["config"]` 的 SQLite 恢复回归覆盖模型、扩展版本/绑定 revision 和宿主 config 漂移；
  变化必须拒绝且零工具 / 模型请求，相同配置仍能恢复。
- 现有测试仅使用 `host_config` 键，补该键不足以覆盖本问题。

## 6. EXT-R5 / P2：失败工厂清理未确认，CloseReport 却报告 ok

位置：`src/yanxu_harness/extensions/assembly.py:246–260` 和启动异常汇总分支。
失败工厂的 close 异常只进入私有 `_display_errors`，没有合并进 `close_report`；
只对先前成功工厂汇总报告也会漏掉本次失败工厂。

复现：第一个工厂获得资源后 open 抛错，close 也抛错，资源计数仍为 **1**；
公开 `close_report` 为 **state=new、ok=True、failed=()、unconfirmed=()**。
随后再 `aclose()`，`ok` 仍为 True。调用者无法从公开端口获知部分启动资源未确认释放，违反 §5.1 / AC20。

修复验收：将失败工厂的释放/失败/超时事实合并到唯一 CloseReport；没有成功工厂时同样有效。
关闭失败、超时、缺 close、已有成功工厂四种路径均如实记录；保留原启动错误及逆序清理其余工厂的行为。
不要求把工厂不合作的资源伪报已释放。

## 7. EXT-R6 / P2：CloseReport 复制工厂异常原文

位置：`src/yanxu_harness/extensions/lifecycle.py:116–117`。
`failed.append(f"{name}: {type(exc).__name__}: {exc}")` 直接把异常原文复制进公开报告，
也没有对该出口应用报告总量限制。

复现：工厂持有的合成私有配置标记只出现在 close 异常消息中；公开 CloseReport 的 `failed` 随即包含该标记。
这是库自身复制异常文本的出口，属于方案 §5.4、AC35、ADR-013 明确控制的范围。

修复验收：公开端口只保留有界扩展 ID、失败类型/稳定 code 和释放状态；不复制任意异常原文。
同时补 close、失败启动清理、factory 抛 HarnessError 等错误出口的 canary 测试和长度边界。
内部调试信息若保留，应与公开/持久化/模型出口分开。

## 8. 给 DeepSeek 的执行顺序与复核门槛

1. **R1–R3**：统一发布能力的撤权和 scope 校验，补实际审批 await、local_tools、MCP Gateway 回归。
2. **R4**：修复合成 config 被覆盖的路径，补真实 SQLite 拒绝与相同配置恢复正例。
3. **R5–R6**：统一启动回滚和正常关闭的公开报告；错误 code、释放事实与长度边界一起验收。
4. 本文独立探针 base **6/6**、extra **7/7** 应通过；保留并运行原套件、原验收探针和 MCP R1–R5 回归。
5. 最终候选重新生成 SNAPSHOT，运行树外 wheel/sdist 的 base/extra 门禁；Python 3.10 缺实跑继续 BLOCKED。
   将提交、真实命令/结果、skip/blocked、资源/效果计数追加到进度与交付记录，再交独立复核。

本次把 AC21、AC26、AC35 标为 **FAIL**，AC20 的清理报告子项标为 **FAIL**；
原有绿色正例保留为正例证据。E2 未完成验收，E3 仍待修复及 AC38，不能用全量绿色覆盖这些失败。
R2 的直接 Gateway scope 缺口独立记录，不据此声称原有 Harness scope 拒绝测试失败。


## 9. 实施方修复记录（待本轮独立复核确认）

实施方已按本交接修复 R1–R6；以下只是修复方声明，不改变 §2–§7 的原始复核结论。
复跑同一独立探针应得到 base **6/6**、extra **7/7**，并保留原套件与 MCP R1–R5。

| 项 | 修复位置与行为 | 回归 |
| --- | --- | --- |
| R1 | `ToolGateway.invoke` 在审批 await 后、intent 前重查 guard；准入后不重查。 | 两个阻塞审批真实 Gateway 测试；探针 `approval_wait_revocation`。 |
| R2 | 组合 guard 增加部署 namespace/授权 scope；scope 仅来自 `RunContext`；直接 Gateway、bridge、MCP 共用。 | `test_a_direct_gateway_call_must_match_the_deployment_scope`、MCP 错误 scope 零远端效果测试；探针。 |
| R3 | `local_tools` 也组合撤销/scope guard（version 不变）。 | 关闭后直接 Gateway + bridge 拒绝测试。 |
| R4 | 宿主 `config` 只作为合成输入，合成后不允许覆盖。 | 真实 SQLite：model 漂移拒绝、相同配置恢复；探针。 |
| R5 | 失败工厂 close 结果与先前工厂合并进唯一 `CloseReport`；成功/失败/超时/缺 close 均记录。 | 5 个失败启动清理回归；探针。 |
| R6 | close 失败条目只保留有界 ID + 异常类型/稳定 code；仅包内 `public_safe` 固定提示保留原文。 | close/factory HarnessError/ExtensionRefused canary 与超长类型名回归。 |

修复后实测：

- `tests/extensions`：**122 passed, 7 skipped**，exit 0。
- `tests/extensions tests/mcp`：**221 passed, 1 skipped**，exit 0。
- 独立探针 base：**6/6**；`--with-mcp`：**7/7**；均 exit 0。
- 完整 `tests`：**1445 passed, 20 skipped**，exit 0。
- `scripts/extensions_gate.py`：`ok: true`（base/sdist 122/7，extra 128/0 + 93 MCP/0）；
  Python 3.10 仍 **BLOCKED**。

修复没有升版本、上传、推 tag、合 main 或切换宿主 wheel。请按 §8 的顺序重新独立复核，
由复核者决定是否更新 §1 的结论；实施方不自行宣布通过。

## 10. 第二轮独立复验（`d4b02a4`）：原六项关闭，新增 EXT-R7

核对提交：`d4b02a4421ab2a9f582f69e1dad2057986185cf7`，修复提交 `26ba99b`。
开始时工作树干净，分支仍为 `codex/extensions-implementation`，版本 `0.3.0a1`。
本轮没有修改生产代码或实施合并、升级、发行；只补独立证据和修复交接。

### 10.1 原 EXT-R1–R6 关闭

本轮重新独立运行，未使用实施方结果代替：

| 检查 | 实际结果 |
| --- | --- |
| 原独立探针 base | **6/6 passed，exit 0** |
| 原独立探针 `--with-mcp` | **7/7 passed，exit 0**；错误 scope 的远端效果 **0** |
| 扩展 + MCP 套件 | **221 passed / 1 skipped，41.93s，exit 0** |
| 基础环境全量 `pytest -q` | **1445 passed / 20 skipped，96.82s，exit 0** |
| 原 `probe_extensions_acceptance.py` | **ok=true，exit 0** |
| MCP R1–R4 独立探针 | **4/4 passed，exit 0** |
| MCP R5 failed-refresh 探针 | **passed=true，exit 0**；零远端和后续模型调用 |

已核对审批 await 后重查、local_tools guard、直接 Gateway scope、合成 config 防覆盖、
失败工厂清理汇总和 close 错误脱敏的接线。原 R5 的“不合作工厂资源仍为 1”现在被
准确报告为失败，不再伪报释放成功。原六项可关闭，不因下述新发现重写原结论。

本轮未重复构建 wheel/sdist 或跨平台安装；Python 3.10 / AC38 仍 **BLOCKED**。

### 10.2 EXT-R7 / P1：`from_adapter()` 跳过借用资源的 namespace/scope/revision 校验

位置：`src/yanxu_harness/extensions/mcp.py:114–117`，`McpExtensionFactory.open()`。
`from_adapter()` 传入已有 adapter 后，`open()` 在该分支直接返回贡献；
下方仅针对 manager 路径的 `borrowed_namespace`、`authorization_scope`、
`config_revision` 校验完全不执行。constructor 还允许 adapter 存在时同时省略 manager 和借用 namespace。

R2 修复后的调用守卫只能证明“调用 context 匹配新部署的 binding”；它没有证据证明
“新部署的 binding 匹配实际借用的 MCP 资源”。底层 catalog guard 同样只验证原
adapter/config/connection 内部一致性，因此不能阻止该映射。

真实复现使用同一个宿主拥有的 stdio manager / adapter：原资源为
`project-a / scope-a / cfg-a`；分别创建新工厂并装入新部署，通过真实 Gateway、SQLite 和远端 bump 调用。

| 场景 | 预期 | 实际 |
| --- | --- | --- |
| 声明借用 `project-a`，部署绑定 `project-b` | 装配拒绝，零效果 | **executed，远端效果 1** |
| 部署授权 scope 改为 `scope-b` | 装配拒绝，零效果 | **executed，远端效果 1** |
| 部署 config revision 改为 `cfg-b` | 装配拒绝，零效果 | **executed，远端效果 1** |
| `from_adapter(adapter)` 省略 manager / 借用 namespace | 显式借用不足，拒绝 | **executed，远端效果 1** |
| 完全匹配的正例 | 成功，效果一次；部署关闭不关闭宿主 manager | **executed，远端效果 1，manager 仍 live** |

负例部署关闭后原 manager 也均保持 live。问题是跨绑定使用资源，并非测试因共享连接被关闭而失败。
本轮临时最小复现和[独立借用探针](../scripts/probe_extensions_borrowed_adapter.py)均复现，
正式探针 **1/5 passed、4 failed，exit 1**。违反方案 §4.4 的可核对借用绑定要求、ADR-011，以及 AC22 的资源作用域边界。

```sh
.venv-mcp/bin/python scripts/probe_extensions_borrowed_adapter.py --report /tmp/extensions-review-r7.json
```

### 10.3 给 DeepSeek 的修复与复验要求

1. 在贡献返回前统一检查所有 MCP 工厂路径的资源绑定，包括已有 adapter、已提供
   connection/snapshot 和 manager；不得用提前返回绕开 namespace/授权 scope/config revision 校验。
2. 借用声明缺失或无法核对时拒绝，或要求可核对的显式绑定；声明的 manager 与 adapter 实际 manager
   也应一致。不能只复制新部署的 scope 标签，就把外部连接认定为属于该部署。
3. 四个负例必须在装配/打开阶段拒绝，零新 intent / 远端效果；完全匹配正例成功一次，
   借用部署关闭或拒绝后宿主 manager 仍可用；自有 manager 的关闭合同保持。
4. 增加真实 Gateway/MCP 回归，独立借用探针应 **5/5、exit 0**；原独立探针
   base **6/6**、extra **7/7** 和现有正例不得退化。再运行相关回归并追加实际证据。

当前：**EXT-R1–R6 CLOSED；EXT-R7 OPEN / P1；AC22 借用绑定子项 FAIL；AC38 BLOCKED。**
先完成 R7，再进行最终候选安装门禁与独立验收。

第二轮交接文件检查：**30 个本地链接有效**；公开 API / 构建脚本 / 快照白名单
**23 passed**；`git diff --check` 通过；SNAPSHOT 已重新生成。


## 11. 实施方对 EXT-R7 的修复记录（待第三轮独立复核确认）

以下只是实施方声明，不改变 §10 的本轮结论。

修复位置：`src/yanxu_harness/extensions/mcp.py`。`open()` 在 adapter 提前返回之前、
在创建自有 manager 之前调用 `_validate_resource_binding(context)`：

- 外部 adapter / manager / connection / snapshot 必须显式声明 `borrowed_namespace`，
  且与部署 `context.namespace` 一致；
- 从 configuration、catalog snapshot、connection 收集实际 authorization scope 和
  config revision，逐项与 binding 比较；缺少任一事实则拒绝；
- 声明的 manager 与 adapter 的实际 manager 不同则拒绝；`from_adapter()` 缺少
  namespace 在构造阶段即拒绝；
- factory 自建 manager 不要求外部 namespace，原有自有 manager 关闭合同不变。

回归：新增 `test_a_borrowed_adapter_must_match_its_resource_binding`，覆盖错误
namespace、错误 scope、错误 revision、缺声明、manager 不一致和匹配正例；负例零
远端效果、借用 manager 保持 live，正例远端效果一次。

修复后实测：

- 借用 adapter 独立探针 **5/5 passed，exit 0**；
- 原独立探针 base **6/6**、`--with-mcp` **7/7**，exit 0；
- `tests/extensions`：**122 passed, 8 skipped**；
- `tests/extensions tests/mcp`：**222 passed, 1 skipped**；
- 完整 `tests`：**1445 passed, 21 skipped**，exit 0；
- `extensions_gate.py`：`ok: true`（base/sdist 122/8，extra 129/0 + 93 MCP/0）；
  Python 3.10 仍 **BLOCKED**。

请按 §10.3 重新复核；R7 当前标记为“已修复，待独立复验”。
