# 扩展开发指南：清单、装配、生命周期与恢复

适用版本：`0.4.0a1`（本分支候选）。**EXT-R1–R7 已独立复验关闭；AC38 本地 Python 3.10.22 实跑通过，
最终发布 CI 进行中**。见[候选版本记录](release-0.4.0a1.zh-CN.md)。

- 方案：[E0–E3 实施方案](extensions-implementation-plan.zh-CN.md)
- 取舍：[ADR](extensions-adr.zh-CN.md)
- 逐 AC 证据：[交付报告](extensions-delivery-report.zh-CN.md)
- 参考实现：[标准库包](../examples/extensions/reference_pack.py)、
  [MCP 包](../examples/extensions/mcp_reference_pack.py)

## 1. 它是什么，不是什么

扩展层把宿主显式选择的 tools、skills 和可选 MCP 工具装配成一个**冻结部署**：

- 清单（manifest）是版本化、有限、纯数据；
- 工厂（factory）是宿主选择的受信任 Python 代码；
- 绑定（binding）是宿主对 namespace、授权 scope、config/policy revision、
  能力和贡献子集的显式授权；
- 计划（plan）是纯预检；打开（open）阶段校验实际贡献后**一次发布**；
- 部署身份进入既有 `DefaultHarness` 的 `config` 指纹，恢复时被真实比较；
- 撤权/关闭后，旧 tool、skill、Harness 引用在派发前拒绝。

明确不提供：任意 Python 沙箱、插件市场、目录扫描、远程安装、依赖求解器、
第二份调度器/权限系统/审批存储/操作账本、Core reducer hooks、后台文件监视。
工厂可以执行任意 Python；扩展接口不改变这个事实。

## 2. 最小可用流程

```python
from yanxu_harness.extensions import (
    ExtensionBinding, ExtensionContext, ExtensionContribution,
    ExtensionManifest, ContributionDeclarations, open_extensions,
    parse_manifest, plan_extensions,
)
from yanxu_harness.tools import GatewayBindings, ToolGateway, core_tool
from yanxu_harness.runtime import DefaultHarness

manifest = parse_manifest({
    "manifest_schema": 1,
    "extension_api": 1,
    "id": "example.reference",
    "version": "1.0.0",
    "requires_capabilities": ["tools.v1", "skills.v1"],
    "contributes": {"tools": ["example.reference.lookup"], "skills": ["reference_lookup"]},
})

class HostFactory:
    async def open(self, context: ExtensionContext) -> ExtensionContribution:
        # 用 context.namespace / resource_refs 构造资源；不要自行创建 Principal。
        return ExtensionContribution(tools=(lookup,), skills=(skill,))

    async def close(self) -> None:
        ...  # 幂等、尽力释放；失败的 close 会进入 CloseReport

binding = ExtensionBinding(
    extension_id="example.reference",
    namespace="project-a",
    authorization_scope="authorized",
    config_revision="cfg-1",
    policy_revision="pol-1",
    capabilities=frozenset({"tools.v1", "skills.v1"}),
    tools=("example.reference.lookup",),
    skills=("reference_lookup",),
)

plan = plan_extensions(
    manifests=[manifest],
    factories={"example.reference": HostFactory()},
    bindings=[binding],
)
async with open_extensions(plan) as deployment:
    gateway = ToolGateway(
        registry=deployment.registry,
        policy=host_policy,
        approvals=host_approvals,
        backend=host_backend,
    )
    bindings = GatewayBindings(gateway=gateway, context=context)
    harness = deployment.bind_harness(
        stream_fn=stream_fn,
        gateway_bindings=bindings,
        persistence=persistence,
    )
    outcome = await harness.start(request, run_context)
```

`bind_harness` 只是普通 `DefaultHarness` 的装配辅助：它注入冻结的 `ToolSpec`
集合与 `deployment_binding`，不创建第二个 Harness。它拒绝一个读取不同 registry
的 Gateway。

## 3. 清单字段与兼容规则

顶层只允许这些字段：

| 字段 | 规则 |
| --- | --- |
| `manifest_schema` | 本构建支持 `1`；未知代次拒绝 |
| `extension_api` | 本构建支持 `1`；未知代次拒绝 |
| `id` | 稳定 ID：小写字母/数字/`.`/`-`/`_`，<=120；重复 ID 即使同版本也拒绝 |
| `version` | 有界发布身份：字母/数字/`.`/`+`/`-`/`_`，<=64；不做多版本选优 |
| `requires_capabilities` | 已知能力集合：`tools.v1`、`skills.v1`、`mcp.tools.v1` |
| `contributes.tools` | 声明工具名（小写稳定 ID），保留声明顺序 |
| `contributes.skills` | 声明 skill id，保留声明顺序 |
| `title` / `description` / `homepage` | 白名单展示字段；有界、无控制字符 |

解析规则：文本输入拒绝任意层级重复 JSON key、`NaN`/`Infinity`、非 JSON 值；
默认上限为 64 KiB、深度 16、节点 4000；`Mapping` 输入会被深拷贝。
未知字段（例如 `authorization`、`python_path`、`read_only`）直接拒绝。
清单不能包含秘密、Python 表达式或授权决定。

计划阶段还要求：

- 每个扩展必须有工厂与 binding；没有的拒绝；
- binding 的能力必须**恰好**满足清单需求；binding 不能授予清单未声明的工具/skill；
- 声明 tools 就必须要求 `tools.v1`，声明 skills 就必须要求 `skills.v1`；
- 一个部署只允许一个 `(namespace, authorization_scope, config_revision)`；
- 工具名冲突、skill `(scope, id)` 冲突、静态依赖缺项/环、总量上限都在这里拒绝；
- 纯预检不启动工厂、不导入 SDK、不联网、不写业务状态。

默认 `ExtensionLimits`：32 个扩展、256 个工具、256 条 skill、单清单 64 KiB、
深度 16、节点 4000、公开报告 256 KiB、skill metadata 64,000 字符、启动/关闭
各 30 秒。宿主可显式传入更大的正数；零、负数、布尔值拒绝。

## 4. 工厂、上下文与贡献

```python
class Factory:
    async def open(self, context: ExtensionContext) -> ExtensionContribution: ...
    async def close(self) -> None: ...
```

`ExtensionContext` 提供：扩展 ID/version、namespace、授权 scope、config/policy
revision、绑定的能力与贡献名、opaque `resource_refs`、limits。它**没有**创建
Principal、扩大 grant、写账本或构造 RunContext 的能力。

`ExtensionContribution`：

- `tools: tuple[ToolDefinition, ...]`：必须与 binding 声明的工具集合完全一致；
  多一个（未绑定）或少一个（绑定但未贡献）都拒绝；
- `skills: tuple[SkillContribution, ...]`：`SkillContribution(provider, skill_ids=(), scope="")`；
  缺省 scope 为部署 namespace；provider 必须实现既有四接口。

工具贡献保留真实 validator、schema、effects、preview、`requires`、
`execution_mode`、结果上限和原 `dispatch_guard`。发布时：

- `args_schema` 被深冻结；callable schema 只求值一次；
- 有效 `version` 变为 `原版本+ext.<binding-digest>`，因此同名同 schema、不同扩展
  版本不能复用旧审批；
- `dispatch_guard` 组合为“扩展失效守卫先、部署 namespace/授权 scope 校验、
  原 guard 后”，任一非空即拒绝，原 MCP/目录守卫不被覆盖；
- `local_tools` 合入同一发布 registry 时也组合撤销/scope guard（保留宿主原
  version），因此关闭后旧 registry、Gateway 和 bridge 的新调用同样拒绝。

## 5. 原子发布与失败回滚

`open_extensions(plan)` 按扩展 ID 稳定顺序打开工厂。全部成功的贡献通过最终校验
（名称/依赖/schema/skill metadata/总量/报告大小）后，才构造只读 registry 与组合
SkillProvider。任一步失败：

1. 对失败工厂调用一次 `close()`（工厂必须清理自己已获得的资源）；
2. 逆序关闭之前成功打开的工厂；
3. 把失败工厂与已成功工厂的释放结果合并进唯一 `CloseReport`（成功、失败、
   超时、缺 `close` 都如实记录）；
4. 状态置 `FAILED`，抛出 `ExtensionOpenFailed` 或其具体子类；
5. 宿主原有 registry 不变。

首版没有 optional/best-effort 扩展；需要删掉失败项时，宿主显式重建 plan。

## 6. 生命周期、撤权与关闭

```text
NEW → OPENING → READY → INVALIDATED / CLOSING → CLOSED
                     ↘ FAILED（启动失败）
```

- 只有 `READY` 可以新建运行和派发能力；
- `deployment.invalidate(reason)` 同步翻转共享 token、置 `INVALIDATED`，之后才
  允许 `await aclose()`；
- `aclose()` 只允许打开它的 task/loop 调用；其他 task/loop 得到
  `ExtensionOwnerMismatch`，原 owner 状态不被破坏；
- 关闭逆序、幂等、每项受 `close_timeout_seconds` 限制；一项失败仍继续清理其余项，
  `CloseReport` 区分 `released` / `failed` / `unconfirmed`；
- 不合作同步代码无法保证被硬中断；报告如实标记“无法确认释放”。

撤权后的行为：

- 新的 `ToolGateway.invoke` 与 `core_tool.execute` 在 visibility/校验/preview/
  审批/admit **之前**拒绝：`denied`，零新 intent、零业务效果；
- 审批 `get`/`supersede`/`put` 返回后、intent 之前会再次执行 guard；等待审批
  期间撤权或 scope 变化同样 `denied`，只有已经准入的调用保留 receipt/UNKNOWN；
- 部署 namespace/授权 scope 从宿主 `RunContext` 读取并与 binding 比较；缺失或
  不匹配都拒绝，不能由工具参数自行声明；
- 旧的 skill 引用抛 `ExtensionInvalidated`；
- 已拿到旧 Harness 引用的 `start`/`resume` 抛 `DeploymentRefused`；
- 已准入的在途调用完成或按既有规则归为 `UNKNOWN`；关闭不会伪造 `failed`/
  `succeeded`，不会回滚远端效果，不会清空审批/账本/Run；
- 一个 scope 的关闭不影响另一个 scope；借用的 MCP manager 不由部署关闭。

## 7. 部署身份与真实恢复

部署摘要（`deployment_version: 1`）覆盖：清单 schema/API、扩展 ID/version、实际
工具定义及来源、skill metadata/来源指纹、宿主手工工具/skill、binding 的
namespace/授权/config/policy revision、行为相关 limits。它排除进程 ID、对象地址、
loop、连接 ID/代次、时间和工厂 `repr`。

摘要被折入 `DefaultHarness.fingerprints()["config"]`——这是既有恢复比较真正会读的
维度。只在旁边加一个 `extensions` 字段不会生效。宿主传入的
`fingerprints["config"]` 是合成输入，不能被宿主值覆盖；否则固定 config 会
掩盖模型或扩展版本漂移。以下分支在派发前检查部署 scope 与有效性：

- `start`；
- 普通 `SUSPENDED` resume；
- `child_result` 等待恢复；
- `RUNNING` orphan / partial-batch recovery；
- committed-action recovery。

扩展升级、binding 缩减、config/policy revision 变化：`ResumeRefused`
（`arrangement_drift`），零模型请求、零工具效果，旧等待保留。相同逻辑部署在新进程
重建：摘要相同、已确认 receipt 复用、handler 不重跑。`UNKNOWN`/部分批次继续按既有
账本证据收敛，预算与 attempt 不刷新。

## 8. MCP 扩展

```python
from yanxu_harness.extensions import mcp_tool_factory

factory = mcp_tool_factory(config=mcp_config, bindings={
    "echo": McpToolBinding("echo", effects=frozenset({ToolEffect.READ})),
    "bump": McpToolBinding("bump", effects=frozenset({ToolEffect.WRITE}),
                           preview=lambda ctx, args: {"action": "bump", "key": args["key"]}),
})
plan = plan_extensions(
    manifests=[manifest], factories={"example.mcp": factory}, bindings=[binding],
)
```

- 未绑定/未打开 MCP 时不导入 SDK、不建进程、不联网；
- 缺 extra 只在显式打开时失败，消息包含 `pip install 'yanxu-harness[mcp]'`；
- 默认每个部署拥有自己的 `McpClientManager`；
- 借用宿主资源（manager、已构建 adapter、connection 或 snapshot）必须显式提供
  `borrowed_namespace`，并让资源实际的 authorization scope/config revision 与部署
  binding 一致；adapter 路径同样校验，不能提前返回绕过；
- 若同时声明 manager，它必须与 adapter 的实际 manager 是同一对象；缺少或无法
  核对 namespace/scope/revision 时拒绝借用；只有自有 manager 会被关闭；
- 沿用 `mcp==2.3.0`、两代协议、两种传输、目录冻结/失效、one-shot、错误映射和
  结果清洗；不实现第二份 MCP 客户端；
- 扩展失效守卫、部署 scope 校验与 MCP 目录守卫组合，三者都必须通过；错误
  scope 在远端调用前拒绝，不产生远端写入。

## 9. 子任务与权限

扩展部署不会把工厂或“可重开全集的服务目录”传给子任务。`ChildCoordinator`
仍只授予 host policy ∩ 父 scope ∩ 显式 proposal 的工具子集；越权工具、超预算
grant、超出 workspace 前缀都拒绝。skill 的 `tools` 字段只是提示，不授予工具或审批。

## 10. 诊断与秘密

`describe_plan()`、`deployment.report()`、`CloseReport` 只包含有界 ID/version、
能力名、状态、贡献名、有效工具 version、来源扩展、摘要、失败 code 和释放状态。
它们不包含 `resource_refs` 值、私有配置、factory `repr`、traceback、异常原文
或 SDK 帧；失败 code 有长度上限。
报告超过 `max_report_bytes` 时发布失败，而不是截断后假装完整。工厂异常消息不会被
复制进公开错误（MCP 缺 extra 的固定安装提示除外）。

## 11. 测试与门禁

```bash
.venv/bin/python -m pytest -q tests/extensions
.venv-mcp/bin/python -m pytest -q tests/extensions tests/mcp
.venv/bin/python scripts/probe_extensions_acceptance.py --report /tmp/extensions-acceptance.json
.venv/bin/python scripts/extensions_gate.py \
    --workdir build/extensions-gate --report build/extensions-gate/report.json
```

门禁在 base / extra / wheel / sdist 四个环境里从**树外副本**运行扩展套件和示例，
记录实际导入位置、收集节点、通过/跳过/失败数与独立探针退出码。base 的 MCP 节点
缺 extra 时只允许该类 skip；extra 环境要求零 skip。Python 3.10 没有真实解释器时
AC38 记 **BLOCKED**，不用静态解析或锁文件替代。

宿主可以把扩展合同检查直接接进自己的测试：

```python
from yanxu_harness.testing import check_extension_deployment_contract

checks = await check_extension_deployment_contract(
    deployment, scope="project-a", skill_ids=("reference_lookup",)
)
```

## 12. 公开 API 与兼容边界

从 `yanxu_harness.extensions` 导入：

- 数据：`ExtensionManifest`、`ContributionDeclarations`、`ExtensionBinding`、
  `ExtensionContext`、`ExtensionContribution`、`SkillContribution`、
  `ExtensionLimits`、`ExtensionState`、`CloseReport`、`AssemblyPlan`；
- 入口：`parse_manifest`、`plan_extensions`、`describe_plan`、`open_extensions`、
  `mcp_tool_factory`；
- 运行时：`ExtensionDeployment`（`AssemblySnapshot` 别名）、`FrozenToolRegistry`、
  `CompositeSkillProvider`；
- 错误：`ManifestError`、`ExtensionRefused`、`ExtensionConflict`、
  `ExtensionLimitExceeded`、`ExtensionOpenFailed`、`ExtensionInvalidated`、
  `ExtensionOwnerMismatch`、`ExtensionStateError`。

手工 `DefaultHarness(...)`、`ToolRegistry(...)`、SkillProvider 与 MCP API 保持可用。
没有 `deployment_binding` 的旧部署指纹/行为逐字不变；旧记录不会因新增默认字段被
判漂移。手工部署切成扩展部署时不伪造旧基线：没有可证明绑定的旧未完成调用保守
拒绝。`0.4.0a1` 复用现有持久化字段，不新增必需格式版本。
