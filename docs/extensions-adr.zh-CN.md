# ADR：Harness 轻量扩展协议 E0–E3 的合同取舍

日期：2026-10-20。适用方案：[E0–E3 实施方案](extensions-implementation-plan.zh-CN.md)。
本 ADR 记录实施时冻结的公开名称、等价实现与边界。它不替代 EXT-AC01–40；
验收仍以方案矩阵和交付报告为准。

## ADR-001：清单是数据，工厂与授权是宿主对象

**决定。** `ExtensionManifest` 只包含版本化数据：`manifest_schema`、
`extension_api`、稳定 `id`、`version`、`requires_capabilities`、
`contributes`，以及白名单展示字段 `title`/`description`/`homepage`。
未知字段、秘密、授权决定、Python 表达式一律拒绝。宿主通过
`factories: Mapping[str, ExtensionFactory]` 显式提供可执行代码，通过
`bindings: Sequence[ExtensionBinding]` 显式授权。清单里不存在
`authorization`、`effects`、`read_only` 或 `idempotent` 这类字段；出现即
`ManifestError`。

**理由。** 清单如果能否决宿主授权，模型或配置作者就能提升权限；如果
清单能命名 Python 路径，解析就变成代码执行。两者都不符合“清单描述需求、
宿主决定授权”的边界。

## ADR-002：一个部署只有一个 `(namespace, authorization_scope, config_revision)`

**决定。** 同一 `AssemblyPlan` 内的所有 binding 必须共享这三个值。
不同 namespace 的能力包使用不同部署；组合层不提供跨 namespace 的隐式
共享。`policy_revision` 也属于部署身份，但允许多个扩展各自声明（最终
摘要按排序集合覆盖）。

**理由。** 方案要求“运行时只允许相匹配的 RunContext 使用此部署”，并要求
A namespace 的 global skill 回退不得出现在 B。一个部署内混合 namespace
会把这两个断言变成不可判定的局部规则。独立部署仍是同一进程内的对象，
成本是显式的宿主装配，不是执行循环。

## ADR-003：严格、有界的清单解析

**决定。** `parse_manifest()` 接受 `str`/`bytes`/`Mapping`。文本输入用
`json.loads(object_pairs_hook=...)` 检测任意层级的重复 key，用
`parse_constant` 拒绝 `NaN`/`Infinity`，再按字节数（默认 64 KiB）、深度
（16）和节点数（4000）设限。整数必须是真正的 `int`（拒绝 `bool`），浮点
必须是有限值。`manifest_schema` 与 `extension_api` 只接受本构建已知的
代次；`requires_capabilities` 必须是已知能力名且无重复；`contributes.tools`
使用工具名规则（小写、点/横线/下划线、<=120），`contributes.skills` 使用
稳定 ID 规则。数组中的重复项拒绝。声明顺序会被保留；对象 key 顺序与集合
顺序不影响身份。

**理由。** 重复 key、非有限数和超界文档都是“解析器和校验器看到不同数据”
的入口。把限制放在解析边界，才能让后续纯预检真的没有隐藏工作。

## ADR-004：版本化 canonical 身份，不保留对象地址

**决定。** `plan_fingerprint()` 与部署摘要都用 `deployment_version: 1` 的
canonical 结构：对象 key 排序，集合排序，数组保留声明顺序，JSON 编码使用
既有 `fingerprint()`。身份包含清单 schema/API、扩展 ID/version、绑定
namespace/授权/config/policy revision、有效工具定义（名称、原 version、
描述、效果、依赖、执行模式、幂等、结果上限、冻结 schema、扩展来源）、skill
metadata 与来源指纹、宿主手工工具/skill，以及与行为相关的 limits。不包含
进程 ID、对象地址、loop、连接 ID、连接代次、时间或工厂 `repr`。

**理由。** 相同逻辑部署跨进程必须得到相同摘要；不同扩展版本/绑定 revision
必须得到不同摘要。`repr()`、`hash()` 和 callable 地址都不满足任一条件。

## ADR-005：冻结是深拷贝 + 不可变 JSON 容器

**决定。** 装配时对 schema 等 JSON 数据做深冻结：mapping 变成不可变的
`dict` 子类（拒绝 `__setitem__`/`update`/`pop` 等），list 变成 tuple，标量
保持 JSON 标量。callable schema 在装配时**求值一次**，之后不再调用。工厂
之后修改原 dict、清单输入被外部修改、callable 返回新 dict，都不会改变
已冻结部署的定义或摘要。发布后的 `FrozenToolRegistry` 也不能注册/替换。

**理由。** frozen dataclass 不冻结内部 mapping；只存一个 hash 不能阻止旧
引用继续以新行为执行。不可变 JSON 容器同时保留 `json.dumps` 可用性，避免
`MappingProxyType` 在现有 `ToolSpec` 序列化路径上失败。

## ADR-006：一次发布，失败全体回滚

**决定。** 打开阶段按扩展 ID 的稳定顺序依次调用工厂 `open(context)`；成功
打开的工厂登记在临时列表中。最终校验（声明/绑定一致、工具名与 skill 键
冲突、依赖缺项/环、schema、metadata、总量、报告大小）全部通过后才构造
`FrozenToolRegistry`、组合 SkillProvider 和 `AssemblySnapshot`。任一阶段
失败：先对失败工厂调用一次 best-effort `close()`（工厂负责清理自己已获得
的资源），再逆序关闭已登记工厂，状态置 `FAILED`，抛出
`ExtensionOpenFailed`；宿主传入的 registry/工具列表不变。

**理由。** 逐项 register 再报错会把半成品留给宿主；只清理“成功登记”
的工厂会漏掉失败项自己已经启动的资源。首版不提供 best-effort 扩展。

## ADR-007：有效工具 version 组合扩展身份

**决定。** 发布到部署 registry 的每个扩展工具都生成新 `ToolDefinition`：
保留名称、handler、validator、preview、effects、requires、execution_mode、
idempotency、result limit；`version = f"{原version}+ext.{digest12}"`，其中
digest 覆盖清单与 binding。`dispatch_guard` 组合为“扩展失效守卫先、部署
namespace/授权 scope 校验、原 guard 后”，任一非空即拒绝，原 MCP/目录
guard 不被覆盖。`args_schema` 使用 ADR-005 的冻结值。作为 `local_tools`
合入同一部署的宿主工具也组合撤销 guard，但不改原 version。

**理由。** 同名同 schema、不同扩展版本/绑定 revision 必须不能复用旧审批。
只改 `fingerprints` 字段而 `tool_fingerprint()` 不比较 version，仍会在真实
恢复中放行；组合后的 version 进入部署摘要，最终进入被比较的 `config`。

## ADR-008：网关在所有准入之前执行通用 dispatch guard

**决定。** `ToolGateway.invoke()` 在命名空间一致性检查之后、可见性/校验/
preview/审批/admit 之前调用 `definition.dispatch_guard(payload)`。payload
包含服务端已知的 call/tool/operation/model-action/namespace/session/run/
attempt、从 `RunContext` 读取的 `authorization_scope` 与 `resume: False`；
guard 抛异常按拒绝处理。没有 guard 的工具行为不变。审批 `get/supersede/
put` 是异步边界：返回后、构造 intent 前再次执行同一 guard，撤权/scope 变化
在尚未准入时仍为 `denied`。`ToolGateway.review()` 不新增 guard（它是只读
账本查询）；恢复路径由 `DefaultHarness._assert_dispatch_guards` 在派发前
检查，payload 同样带 context scope。

**理由。** 现有实现只在 Harness 恢复时读 guard，直接 `Gateway.invoke()` 能
绕过撤权。把接线放在网关的唯一执行入口，才能同时覆盖 `core_tool` 与直接
调用；放在准入之后会留下 preview/审批/intent 的痕迹。

## ADR-009：生命周期、owner 与有界关闭

**决定。** 状态为 `NEW → OPENING → READY → INVALIDATED/CLOSING → CLOSED`，
启动失败为 `FAILED`。只有 `READY` 可建新运行和派发。`invalidate()` 是同步
操作：先翻转共享失效 token、置 `INVALIDATED`，之后才允许 await 关闭。
关闭逆序、幂等、每项受 `close_timeout_seconds` 约束；一项失败仍继续清理
其余项，并把失败清单放在 `CloseReport`。启动失败时，失败工厂自己的 close
结果（成功/失败/超时/缺 close）也合并进同一个 `CloseReport`，没有先前成功
工厂时同样如此。公开失败项只保留有界 ID 和异常类型/稳定 code，不复制
异常原文。记录打开时的 owner task 与 loop；
在别的 task/loop 调用关闭明确拒绝（`ExtensionOwnerMismatch`），原 owner
仍可正常关闭，避免在错误 task 中退出 anyio/任务组。

**理由。** 跨 task 退出是资源泄漏和错误取消的常见来源。方案允许“明确拒绝
或转交原 owner”；首版选择拒绝，并给出可恢复的原 owner 路径。对不合作的
同步 Python 代码不宣称硬中断，关闭报告区分“已释放”和“无法确认释放”。

## ADR-010：组合 SkillProvider 只在绑定 scope 内可见

**决定。** 每个 `SkillContribution` 有显式 `scope`（缺省为部署 namespace）。
`list_metadata(scope=X)` 只返回 `X == 绑定 scope` 的冻结 metadata；请求别的
scope 返回空，不跨 namespace 回退。`load_body`/`read_resource` 先检查失效
token，再按需调用源 provider；读到的 body/resource 指纹必须与装配时冻结的
来源指纹一致，否则使整个部署失效并拒绝该次读取。`metadata.tools` 只是提示，
不产生授权；正文和资源不进入 system prompt、不执行脚本。同一
`(scope, skill_id)` 来自两个扩展或宿主 provider 时，在发布前报冲突。

**理由。** 内存 provider 的同键注册是覆盖语义；组合层如果在它之上逐个
register，会把冲突变成静默替换。惰性读取 + 指纹核对才能让“旧引用不能继续
读取”，同时不为了列清单预读全部资源。

## ADR-011：MCP 只做显式辅助，借用资源必须声明所有权

**决定。** `mcp_tool_factory()` 是显式辅助：`open()` 内才导入
`integrations.mcp`、创建/进入 `McpClientManager`、`connect()`、
`discover_catalog()`、构造 `McpToolAdapter` 并取定义。没有绑定 MCP 扩展时
不导入 SDK、不建进程、不联网。默认每个部署拥有自己的 manager；借用宿主
manager 必须显式传入 `owner="host"`，并绑定同一
namespace/authorization scope/config revision，否则拒绝借用。关闭只关闭自有
manager。SDK 缺失在 `open()` 时给出既有 `McpExtraMissing` 安装提示；MCP
schema、one-shot、错误映射、目录失效、结果清洗全部沿用原实现。

**理由。** 第二份 MCP 实现会分裂恢复语义；不声明所有权会在关闭部署时误关
宿主的连接。借用检查把“底层 key 没有 namespace”这一已知风险显式化。

## ADR-012：恢复指纹接在 config，不在旁边加一个没人比较的字段

**决定。** `DefaultHarness` 新增可选 `deployment_binding` 端口：
`fingerprint`、`summary`、`check(context)`。存在绑定时，`fingerprints()["config"]`
在原有 `model`/`system_prompt` 之外加入 `deployment` 摘要，并额外暴露
`deployment` 键；宿主传入的 `config` 指纹是合成输入，不允许在合成后覆盖
`config`。没有绑定时，指纹表达式与当前实现逐字一致。`start`、
`resume`、`_resume_child_result`、`_recover_orphan`、`_recover_partial_batch`
在派发新模型请求/工具之前调用 `check(context)`；失败抛
`DeploymentRefused`（`reason_code` 区分 namespace/授权 scope/失效）。仓库内
`AssemblySnapshot.bind_harness()` 负责注入绑定与冻结 registry，并拒绝一个
使用不同 registry 的 Gateway。

**理由。** `fingerprints={"extensions": ...}` 不会自动进入
`_compare_arrangement`；只有组合进确实被比较的 `config`，扩展升级才会在
真实恢复中拒绝。原模型/指令/宿主 policy 维度保持不变，避免扩展摘要覆盖
原证据。

## ADR-013：诊断与报告只暴露可公开事实

**决定。** `describe_plan()`、`deployment_report()`、`CloseReport` 只包含
有界 ID/version、能力名、状态、贡献名、冻结工具 version、来源扩展、摘要、
失败 code 和“已释放/无法确认”标记；不包含 resource ref 值、私有配置、
factory `repr`、traceback、异常原文或 SDK 帧。失败 code 有长度上限；报告
超过 `max_report_bytes` 时发布失败（`ExtensionLimitExceeded`），而不是截断
后假装完整。

**理由。** 诊断不能启动工厂、重连或读资源；截断报告会让调用者误以为看到
完整部署。已知凭据清洗继续由 MCP 层和宿主投影负责，本层不宣称能阻止任意
受信任工厂自行打印秘密。

## ADR-014：兼容边界

**决定。** 手工 `DefaultHarness(...)`、`ToolRegistry(...)`、SkillProvider 与
MCP API 保持可用；没有 `deployment_binding` 的旧部署指纹和行为不变。扩展
部署使用新部署记录；未采用扩展的旧记录不因新增默认字段被判漂移。手工部署
切成扩展部署时不伪造旧基线：没有可证明绑定的旧未完成调用保守拒绝。包版本
保持 `0.3.0a1`，持久化格式首版复用现有 `config`/`arrangement` 字段，不新增
必需格式版本。

**理由。** 扩展机制是新能力，不应把旧部署强行改写成“与扩展部署等价”。
真正需要的格式扩展留到 `0.4.0a1` 复核后决定。


## ADR-015：独立复核 R1–R6 的修复边界

**决定。** 独立复核发现的行为缺口按以下边界修复：

- R1：审批 await 之后、intent 之前重查同一 dispatch guard；已准入调用保持
  原 receipt/UNKNOWN 语义。
- R2：部署 namespace/授权 scope 校验放进组合 guard；scope 来自宿主
  `RunContext`，缺失/不匹配均拒绝；直接 Gateway、bridge 和 MCP 共用。
- R3：`local_tools` 与扩展工具一样组合撤销 guard，但不改宿主原 version。
- R4：宿主 `fingerprints["config"]` 只作为合成输入；扩展路径合成后再赋值，
  不允许覆盖。
- R5：失败工厂的 close 结果合并进唯一 `CloseReport`；成功/失败/超时/缺
  close 都如实记录。
- R6：公开 close 失败只保留有界 ID、异常类型或稳定 code；不复制异常原文。
  只有包内明确标记 `public_safe` 的辅助错误（MCP 缺 extra 的固定安装提示）
  可保留原文。

**理由。** 这些修复都收紧现有边界，不增加 Core 循环、账本、审批存储或第二
调度器；无扩展的手工路径保持原指纹和行为。修复证据由独立探针和新增回归
测试复跑，最终是否通过仍由独立复核决定。
