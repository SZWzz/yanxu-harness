# Harness 轻量扩展协议与装配层：E0–E3 实施方案

日期：2026-10-20（Asia/Shanghai）。源码核对基线：`9aed198872916c9b3d1496a4647ed24875364ef4`，
包版本 `0.3.0a1`，编制开始时 main 工作树干净。状态：**EXT-R1–R6 已独立复验关闭；EXT-R7 / P1 待修复；AC38（Python 3.10 实跑）BLOCKED**；方案正文保留原设计，实施方命令与逐 AC 证据见[交付报告](extensions-delivery-report.zh-CN.md)，最新结论见[独立复核](extensions-independent-review.zh-CN.md)。
实施者：DeepSeek。交接入口：[DeepSeek 交接说明](extensions-deepseek-handoff.zh-CN.md)，
实施记录：[阶段进度模板](extensions-progress.zh-CN.md)。

新增模块、类型、参数、测试节点和命令均为设计建议，不能当作现有 API。
实施者重新核对 HEAD、适用 AGENTS/RTK 和 CodeGraph；名称及文件拆分可调整，行为契约和验收条件必须覆盖。
常规实现取舍可用 ADR 记录后继续；不能通过删验收条件把未覆盖的行为改记为完成。

## 1. 目标、范围与交付原则

让宿主显式装配一组 tools、skills 与 MCP 工具，获得稳定身份、原子发布、明确资源所有权、
撤权和升级后的恢复拒绝。能力包增加到宿主时，不修改 Core 状态机或增加执行循环。
首轮目标是独立 Harness 的扩展 API，Yanxu 的插件管理页面和 MCP 产品配置不属于本轮。

| 必须交付 | 退出边界 |
| --- | --- |
| E0：扩展清单与绑定契约 | 纯数据解析、兼容判断、稳定身份、宿主授权与公开诊断边界 |
| E1：确定性装配 | tools/skills/MCP 贡献转换成既有接口；完整校验后一次发布，冲突不覆盖 |
| E2：生命周期与恢复接线 | 启动回滚、有界关闭、冻结部署、失效守卫、真实跨进程恢复 |
| E3：参考扩展与交付门禁 | 两种能力包、开发指南、独立探针、base/extra 和树外 wheel/sdist 验证 |

首版采用**宿主显式提供工厂对象和绑定**。不扫描任意插件目录，不根据清单 import Python 路径、
eval 表达式、安装依赖或下载代码；不增加插件市场、远程安装、依赖求解器、任意 Core hooks、
第二份调度器/权限系统/审批存储/操作账本。Provider、ContextController、TaskCoordinator 沿用既有注入接口。

扩展工厂是宿主选择的受信任 Python 代码；本接口不提供任意 Python 沙箱。
清单只描述能力需求。可见性、Principal、审批、子任务委派和预算仍由宿主与既有执行入口决定。
基座继续 Python 3.10+、零第三方运行依赖；MCP 工厂显式打开时才需要已有 `mcp` extra。

实施期间保持 `0.3.0a1`；`0.4.0a1` 是独立复核后的候选里程碑，不在实现中提前改版本、上传包、
推 tag 或切换宿主 wheel。验证构建可以执行并保留证据，不覆盖旧发行目录。

## 2. 已核对接点与特别容易漏掉的行为

| 实际源码 | 现状及接入要求 |
| --- | --- |
| [tools/definition.py](../src/yanxu_harness/tools/definition.py) | `ToolDefinition` 有 version/effects/validator/preview/requires/dispatch_guard；保留同步 preview 和真实 validator |
| [tools/registry.py](../src/yanxu_harness/tools/registry.py) | 注册、依赖闭包和冲突校验可复用；装配必须用私有暂存集合，不能逐项污染宿主 registry 后再报错 |
| [tools/core_bridge.py](../src/yanxu_harness/tools/core_bridge.py) | 既有 `core_tool`、恢复和 guard 接线必须完整保留；扩展 handler 不直接交给 Core 绕过 Gateway |
| [skills/provider.py](../src/yanxu_harness/skills/provider.py)、[skills/memory.py](../src/yanxu_harness/skills/memory.py) | metadata/body/resource/fingerprint 四个接口；现有内存 provider 同键注册会覆盖，扩展组合层必须先拒绝同键冲突 |
| [integrations/mcp/adapter.py](../src/yanxu_harness/integrations/mcp/adapter.py)、[catalog.py](../src/yanxu_harness/integrations/mcp/catalog.py) | 快照与别名已稳定，调用前检查当前可信目录；刷新失败也会失效，包装不得丢失该守卫 |
| [integrations/mcp/client.py](../src/yanxu_harness/integrations/mcp/client.py) | manager 有 async 资源所有权；只在正确 task/loop 退出，不能在任意 handler 内反复进入/退出 |
| [runtime/default_harness.py](../src/yanxu_harness/runtime/default_harness.py) | `fingerprints()` 与 arrangement 在派发前提交；恢复目前比较 config/tools/instructions/policy 和 exposure |
| [runtime/context.py](../src/yanxu_harness/runtime/context.py) | RunIdentity、Principal、取消和 operation_id 由宿主/Gateway 产生，不从扩展清单或模型参数建立 |
| [testing/contract_suite.py](../src/yanxu_harness/testing/contract_suite.py) | 复用 SkillProvider 等协议合同；新增扩展合同检查须覆盖实际调用与拒绝行为 |

三个必须先做 RED 的坑：

1. 现有 `tool_fingerprint()` 不覆盖工具的所有执行语义，例如 `ToolDefinition.version`；
   新增一个 `fingerprints={"extensions": ...}` 字段，也不会自动进入现有恢复比较。
   **只存一个 hash 或改一个字典不能证明漂移守卫已接通。**
2. frozen dataclass 不代表内部 mapping、provider、callable 返回的 schema 都不可变。
   装配后修改输入字典、改变 skill 内容或撤销一个扩展，旧部署必须保持冻结或明确失效，不能静默换行为。
3. 当前 `core_tool` 只将 `dispatch_guard` 传给 ToolSpec，Harness 在恢复时使用它；
   `ToolGateway.invoke()` 没有通用 guard 调用。只给新工具挂该字段，不能保证直接 Gateway 调用在撤权后拒绝。

## 3. E0 应冻结的契约

### 3.1 清单、工厂、宿主绑定分开

建议包目录 `src/yanxu_harness/extensions/`，用标准库 dataclass/Protocol 表达以下三类对象：

| 建议对象 | 内容与边界 |
| --- | --- |
| `ExtensionManifest` | 版本化纯数据：manifest schema、extension API 代次、稳定 ID、扩展版本、需求和贡献声明 |
| `ExtensionFactory` | 宿主显式传入的 callable/对象，创建资源并返回实际贡献；不从 JSON 推导可执行 import |
| `ExtensionBinding` | 宿主允许的贡献子集、namespace、authorization_scope、config_revision、policy_revision、显式资源引用与 limits |
| `ExtensionContext` | 工厂获得的宿主选择资源与作用域；没有权力自行创建 Principal、扩大 grant 或写入 Harness 账本 |
| `ExtensionContribution` | 本地 ToolDefinition、SkillProvider 的受限视图，或已有 MCP 适配器产物；不持有 Core 的可变列表 |
| `AssemblyPlan` / `AssemblySnapshot` | 前者是无资源启动的校验计划；后者是成功打开后冻结的有效部署与来源表 |
| `ExtensionScope` | 同一资源所有者驱动的 async context manager，持有本次部署的资源和失效状态 |

示意清单（字段名待 E0 冻结，以下不是可直接运行的配置）：

```json
{
  "manifest_schema": 1,
  "extension_api": 1,
  "id": "example.reference",
  "version": "1.0.0",
  "requires_capabilities": ["tools.v1", "skills.v1"],
  "contributes": {
    "tools": ["example.reference.lookup"],
    "skills": ["reference_lookup"]
  }
}
```

兼容性首先比较明确的 API 代次和已知 capability，不引入基座第三方版本范围解析器。
扩展版本是有界、非空的发布身份，首版不做多个版本自动选优或安装求解。
未知必需字段/能力/API 代次、重复 JSON key、NaN/Infinity、超界数值与不合法类型明确拒绝。
需要忽略的展示字段必须在 E0 明确白名单，不能默默忽略执行相关字段。

清单不得包含秘密值、Python 表达式或授权决定。公开配置只放可展示的选择和 revision；
凭据/私有端点等配置通过宿主不透明引用交给工厂，不序列化到清单/诊断/指纹，**秘密本身的散列也不保存**。
宿主通过 scope/config/policy revision 表达轮换是否改变授权语义。

### 3.2 首版身份、冲突与上限

宿主作用域至少显式表达 `(namespace, authorization_scope, config_revision)`；
运行时只允许相匹配的 RunContext 使用此部署。进程内资源以作用域加生命周期实例隔离，
逻辑部署指纹排除进程 ID、对象地址、loop、连接代次和时间。没有隐式跨 namespace 共享。

扩展 ID 与本地工具名采用既有稳定 ID 风格；保留本地工具原名与 MCP 已有 alias，
不以清单排序或自动重命名解决冲突。重复扩展 ID 即使版本相同也拒绝。
有效工具名冲突、同一有效 skill `(scope, skill_id)` 冲突与手工贡献冲突一律报出双方来源。
工具 `requires` 仍表示既有工具依赖；装配最终集合的缺项和环必须拒绝。
首版不引入扩展之间的自动依赖加载；需要的工厂和资源由宿主显式提供。

建议默认装配上限：32 个扩展、256 个有效工具、256 条 skill metadata、单清单 64 KiB、
清单深度 16、节点 4000、公开部署报告 256 KiB。宿主可以通过有界正数 limits 调整。
最终聚合也检查总量，不能靠每个叶节点合格绕过总上限；底层 MCP/SkillLimits 继续生效。

### 3.3 显式打开和发布

建议体验（伪代码；实现后文档必须换成真实可执行示例）：

```python
plan = plan_extensions(manifests=manifests, factories=host_factories,
                       bindings=host_bindings, local_tools=local_tools)
async with open_extensions(plan) as deployment:
    gateway = ToolGateway(registry=deployment.registry, policy=host_policy,
                          approvals=host_approvals, backend=host_backend)
    harness = deployment.bind_harness(
        stream_fn=stream_fn, persistence=persistence,
        gateway_bindings=GatewayBindings(gateway=gateway, context=context_supplier),
    )
    outcome = await harness.start(request, run_context)
```

`bind_harness` 是普通 `DefaultHarness` 的装配辅助函数，不是另一个 Harness/Runner。
宿主用已发布 registry 和自己的 policy/approval/backend 构造这一个 Gateway；不能在旧 Gateway 外
再套一个独立准入/结算通道。实际 helper 须保证 Gateway 使用同一冻结工具定义，拒绝不一致的注册表，
不能只给 Core 换了新定义却让 Gateway 继续读旧定义。Skills 通过快照提供受限 provider 视图，
由宿主显式接到已有技能加载入口，helper 不自动改 system prompt。
既有手工 `DefaultHarness(...)`、`ToolRegistry(...)`、SkillProvider 和 MCP API 保持可用。

## 4. E1：原子装配和能力贡献

### 4.1 两阶段装配

1. **纯预检**：解析与兼容、重复 ID、绑定完整性、已知静态贡献冲突、上限检查；不调用工厂、不导入 SDK、不连接。
2. **显式打开**：按扩展 ID 的稳定顺序打开宿主提供的工厂；动态 MCP discovery 仅发生于此阶段。
3. **最终冻结**：校验实际贡献与声明/绑定一致、所有依赖、schema/preview、skills metadata 和总量。
4. **一次发布**：所有贡献合格后才生成 READY 快照及可执行视图。后一个扩展失败时不留下前一个的半成品 registry。

工厂在获得资源后发生错误，自己的 async context manager 必须先清理已获得资源；
装配器再按逆序回收之前成功打开的资源。仅关闭已经登记成功的工厂不足以证明“部分启动”无泄漏。
动态贡献不能在预检时全部获知，最终校验失败仍须全体回滚，不能以“已开始启动”为由发布部分能力。
首版不提供 optional/best-effort 扩展；宿主若要删去失败项，须显式重建一个新计划。

### 4.2 工具与 Skills

- 工具贡献保留真实 validator、schema、effects、preview、target/policy 语义和 execution_mode。
  组合扩展守卫与原 `dispatch_guard`，两者均须通过；不得覆盖 MCP 守卫。
- 扩展身份与宿主 binding 必须参与有效工具 version/审批身份，防止相同名称/schema、不同实现版本复用旧批准。
  建议组合原 version 与版本化绑定摘要，保留原值作来源；MCP 既有 alias 和定义摘要保留。
- `ToolGateway`/`core_tool` 仍执行校验、审批、admit、execute、settle、receipt 复用与 unknown 收敛。
  装配层不调用业务 handler、发模型请求、写 intent 或提供自动重试。
- Skill 组合实现既有四接口，metadata 有界；body/resource 按需读取且核对冻结来源/指纹。
  不能为了聚合清单预读全部资源。可变 provider 的指纹变化要使部署失效或拒绝该次读取，不能悄悄更新冻结值。
- 同一 skill ID 来自不同扩展或手工 provider 时，组合层先做冲突检查，禁止用内存 provider 的覆盖语义隐藏冲突。
  skill 的 `tools` 提示不授予工具权限，正文和资源不自动加入系统指令、不执行脚本。
- 作用域路由由可信调用入口提供。provider 的 `global` 回退也必须受本次宿主 binding 限制；
  A namespace 的扩展不得因这个回退出现在 B。不得伪称该阶段重写了所有宿主的旧 SkillProvider 行为。

### 4.3 MCP 贡献

提供显式 MCP 工厂辅助函数，懒调用既有 McpClientManager/discover_catalog/McpToolAdapter；
沿用 `mcp==2.3.0` 与当前协议/传输支持面，不引入第二份 MCP 实现。
SDK 缺失在显式打开该扩展时给出已有安装提示；未绑定/关闭 MCP 扩展时没有进程和网络。
默认每个 ExtensionScope 使用自己的 manager；外部借用 manager 必须由宿主显式声明所有权，关闭部署不能误关借用资源。
借用资源还须有可核对的 namespace/授权 scope 绑定；不能因底层 manager 的 key 没有 namespace
就隐式复用跨项目 session。没有这份绑定信息时首版拒绝借用，普通自有 manager 路径仍可工作。
manager 的进入/退出遵守其 task/loop 所有权；若需要 supervisor，由 scope 管理且有结束确认，不能遗留后台任务。
MCP 的 schema、one-shot、错误映射、目录失效和结果清洗原样保留。

## 5. E2：生命周期、指纹与恢复

### 5.1 生命周期和失效

首版生命周期建议 `NEW → OPENING → READY → INVALIDATED/CLOSING → CLOSED`，启动失败为 FAILED。
只有 READY 可新建运行和派发能力。`invalidate` 同步撤销使用权，先关 admission 再做任何 await。
关闭可重复调用、实际资源只回收一次；跨 task/loop 非法调用明确拒绝或转交原 owner，不发生错误 task 退出。
启动/关闭设期限，逆序清理；一项 close 失败仍尝试清理其他项并报告失败清单。

工厂需遵守异步取消与清理契约。对于不合作的任意 Python 同步代码，不宣称能硬中断或证明已经停止；
关闭报告区分“已释放”和“无法确认释放”。实际 subprocess 清理由既有传输负责。

撤销/关闭后，已经持有旧 tools、skills、快照或 Harness 的引用也不能继续执行/读取。
不在运行批次中热替换旧定义；新配置创建新部署，旧部署先失效。由宿主发起撤权/替换，
不做后台轮询、文件监视或网络刷新。需要读取当前外部事实时显式 refresh；刷新失败沿用保守失效。

工具拒绝必须覆盖实际 `core_tool.execute` 与直接 `ToolGateway.invoke`，不能只测 guard helper。
若进入调用前已失效，应在 preview/申请新批准/admit 之前拒绝，零新 intent、零业务效果；
含审批等 await 的路径还须在准入前重新检查。准入成功后关闭才发生的调用属于在途调用，
按下面的 grace/效果证据规则处理；不要把它与“关闭后才开始的新调用”混成同一个断言。
允许增加通用可选守卫接线或等价受控包装，普通未声明守卫的工具保持原行为。

在途调用可保留真实 receipt；取消或释放本地资源不能证明远端效果消失。
等待 grace 后仍无法确认终态时，复用既有 UNKNOWN 控制流，不重放或构造 failed/succeeded。
关闭过程不清空审批/账本/Run，也不触发新模型请求；一个 scope 的关闭不改变另一个 scope。

### 5.2 持久身份必须进入实际比较路径

有效部署摘要覆盖：清单 schema/API 代次、扩展 ID/version、实际贡献、有效工具定义及原版本、
skill 来源指纹、宿主选择的 capability 子集、namespace、授权/config/policy revision 与行为相关 limits。
对逻辑无序集合排序，对有语义顺序的值保留顺序；用版本化 canonical 编码，不用 repr()/hash()/callable 地址。
工厂 callable 的字节身份不是可验证发布版本；宿主负责在更换实现时推进扩展版本或配置 revision。
摘要不能证明工厂是受信任代码或替代授权。

建议在 **DefaultHarness 装配边界**增加可选的通用部署绑定端口（如 `deployment_binding`）：

- 端口来自成功冻结的 assembly，包含安全摘要、可信作用域和无 I/O 的当前有效性检查。
- 将 assembly 摘要组合进现有、确实会被比较的 `config` 指纹；保留模型、system prompt 和
  宿主原 config 指纹，禁止用扩展 hash 直接覆盖掉原配置维度。policy/tools/instructions 比较仍保留。
- 没有该端口的手工部署，指纹与行为逐字兼容当前实现。空扩展路径也提供等价的手工装配模式。
- 同时约束“仅工具/skills 被拷走”的入口：工具组合 guard、受限 SkillProvider 视图均持有失效 token；
  documented 高层装配入口不允许漏接恢复指纹。低层端口的宿主责任需写明，不能宣称约束任意直接 Python handler 调用。
- `start`、SUSPENDED resume、RUNNING orphan/partial-batch recovery、`child_result` 等待恢复都要
  在新模型/工具派发前检查绑定作用域与有效性。**只修改普通 resume 分支不足以完成。**
- 子任务只接收显式委派子集，不继承可重新打开全部能力的工厂/服务目录；沿用既有预算、期限与调用身份。

E0 ADR 冻结具体接线方式，允许选择等价实现；必须给出上述所有分支的真实红绿测试。
不能仅新增未被比较的 `extensions` 字段，不能在恢复时从当前配置重建并覆盖旧 arrangement。
首版优先复用现有持久化字段；若确需格式改动，提交格式差异、读旧/拒绝未知版本测试和迁移决策。

### 5.3 旧数据、审批与回执

| 场景 | 要求 |
| --- | --- |
| 未采用扩展的旧手工部署恢复 | 原有行为不变，不凭新增默认值把旧记录全部判为漂移 |
| 扩展部署新进程重建、内容与授权相同 | 指纹相同；新连接/对象代次不造成误漂移；已结算回执复用 |
| 扩展升级、撤销、绑定缩减、policy/config revision 变化 | 明确恢复拒绝，旧等待保留，零新远端调用、零后续模型请求 |
| 手工旧部署切成扩展部署 | 不自动宣称身份等价；无可证明的绑定时保守拒绝旧未完成调用，记录兼容边界 |
| UNKNOWN / 部分批次 | 继续按宿主账本证据收敛，预算和 attempt 不能刷新；不另建扩展专用 operation ID |
| 已结算后关闭旧 scope | 回执和历史保持原事实；关闭不删除已完成效果，也不使旧终态复活 |

安全诊断只给有界 ID/version、能力名称、状态、失败 code、来源与部署摘要；
不输出私有配置、factory repr、原始 traceback、凭据引用内容或 SDK 帧。
身份摘要中任何非公开数据一律排除，诊断不启动工厂、重连或读资源。
已知凭据回显沿用 MCP 清洗或宿主显式提供的安全投影；测试覆盖本层控制的报告、包装结果与错误出口。
不宣称能阻止任意受信任工厂自行打印秘密，也不新增猜测所有未知秘密的全局扫描器。

## 6. 分阶段任务与提交边界

| 阶段 | 建议落点与任务 | 退出门禁 |
| --- | --- | --- |
| E0 | `extensions/types.py`、`manifest.py`、`errors.py`；ADR、纯合同、兼容/limits/JSON 解析、基线记录 | AC01–08，构造/解析零工厂调用与零 I/O；公开类型只依赖标准库 |
| E1 | `assembly.py`、`skills.py`、tools 包装；显式 MCP 工厂辅助；临时 registry 与最终冻结、冲突/依赖/懒资源 | AC09–18；两个无业务副作用的工厂组合成功，后项失败整批未发布 |
| E2 | `lifecycle.py`、通用部署绑定接线、guard 组合、scope/失效/指纹/兼容；真实进程与故障窗口 | AC19–31；实际恢复拒绝与相同部署恢复都成立，关闭及回滚无孤儿 |
| E3 | `testing/` 扩展合同辅助、两个示例包、使用/开发文档、独立探针、安装 gate 与交付报告 | AC32–40；base 与 extra 各自满足真实支持面，默认套件含关键故障回归 |

每阶段至少一个行为 RED→GREEN；按 E0、E1、E2、E3 顺序小提交。
E1 可以先在独立测试中打开资源；在 E2 真实指纹/失效接线完成前，不标记为安全可恢复部署。
提交后更新 [阶段进度](extensions-progress.zh-CN.md)，记录实际节点、命令、退出码与未执行项。

## 7. 验收矩阵 EXT-AC01–40

本表全为**待实施/待验证**。测试文件建议放 `tests/extensions/`；节点名由实施者补实际名称，
矩阵必须逐行记录证据，不用包级测试总数替代行为证明。

| AC | 阶段 | 必须证明的行为 |
| --- | --- | --- |
| 01 | E0 | 清单严格解析、字段/类型/ID/API 校验；未知代次、重复 JSON key、非有限数和超界文档拒绝 |
| 02 | E0 | 不兼容能力明确拒绝；版本不自动选优，未知工厂 ID 不触发 import/安装/网络 |
| 03 | E0 | 同一语义不同 mapping/set 输入顺序产生同一计划与摘要；语义顺序变化有区别 |
| 04 | E0 | 改清单原始 dict/list、工厂返回 dict 或 callable schema 的后续返回值，不静默改变已冻结部署 |
| 05 | E0 | 宿主 binding 缺失/需求未满足/重复扩展 ID 拒绝；清单自称授权、只读、幂等不能提升权限 |
| 06 | E0 | 构造、纯预检、列清单与只读诊断均零 factory start、零进程、零网络、零业务写入 |
| 07 | E0 | 聚合扩展/工具/skill/report 总量及嵌套上限有效，不能靠逐项切片或遗漏项假装完整 |
| 08 | E0 | 新模块全部冷导入无 MCP/JSONSchema/Web 依赖泄漏；既有基座零运行依赖 |
| 09 | E1 | 组合本地工具与原手工工具，仍通过实际 Gateway；validator/preview/效果/顺序/identity 完整 |
| 10 | E1 | 扩展间、扩展与手工贡献工具名冲突，skill 同键冲突均拒绝，无后项覆盖 |
| 11 | E1 | 最终工具依赖缺项/环拒绝；动态 MCP 贡献造成冲突同样回滚，无半注册可执行项 |
| 12 | E1 | 第二个工厂失败、第一项已打开时，已有项与失败项的半启动资源都回收；旧宿主 registry 未变 |
| 13 | E1 | 一个贡献未获绑定即不可声明/发现/执行；宣称能力与实际贡献不符拒绝 |
| 14 | E1 | Composite SkillProvider 通过四接口合同，metadata 有界、资源惰性、路径保护与来源指纹有效 |
| 15 | E1 | Skill scope/global 回退不跨 namespace 泄漏；加载正文不能注册工具或授予批准 |
| 16 | E1 | 本地和 MCP 写工具仍先具体预览审批，再 admit/execute；delegated 后端不双记 |
| 17 | E1 | MCP 未绑定/关闭不需 SDK；缺 extra 仅在显式打开时明确失败，本地包仍可用 |
| 18 | E1 | MCP 原别名/schema/one-shot/中立结果/目录守卫完整；一次准入远端 tools/call 最多一次 |
| 19 | E2 | 同 owner 打开关闭成功；跨 loop/错误 owner 调用拒绝且不破坏原所有者状态，随后正确退出无资源残留 |
| 20 | E2 | startup 失败/超时/取消均回滚；close 逆序、有界、幂等；一项 close 抛错不漏清理其余项 |
| 21 | E2 | invalidate/close 后旧 tool/skill/Harness 引用失效；实际 bridge 和直接 Gateway 新调用零 intent/效果，旧等待不丢失 |
| 22 | E2 | 一个 namespace/授权 scope 关闭不影响另一个；共享/借用资源仅由实际 owner 关闭 |
| 23 | E2 | tool 的扩展守卫与原 MCP 守卫都有效；远端目录刷新成功/失败后的旧引用均不能绕过失效 |
| 24 | E2 | 同名同 schema 但扩展版本/绑定 revision 改变：真实 SQLite Harness 审批后恢复拒绝，零工具与后续模型请求 |
| 25 | E2 | 普通 SUSPENDED、RUNNING orphan/partial batch、child_result 恢复均检查原绑定；scope 错误在派发前拒绝 |
| 26 | E2 | 原模型/system prompt/宿主 config/policy 的漂移检查仍生效；扩展摘要不覆盖原配置证据 |
| 27 | E2 | 真新进程重建相同逻辑部署可恢复；连接 ID/对象地址不同不误漂移，已确认 receipt 不重新调用 handler |
| 28 | E2 | 在途写后关闭/取消且终态未证明仍为 UNKNOWN；审批、回执、attempt/预算和已发生效果保留 |
| 29 | E2 | A 已结算/B UNKNOWN/C 未发：新进程先阻塞；证据收敛后 C 一次，A/B 不重发、不刷新额度 |
| 30 | E2 | 无扩展的旧手工部署无指纹/行为回归；改用扩展的旧未完成调用不自动伪造等价基线 |
| 31 | E2 | 真 ChildCoordinator 只给显式子集；越权拒绝、secret 不传递、父子 budget/deadline 不增加；子任务拿不到重开全集的工厂 |
| 32 | E3 | 两个参考包与两个真实可执行示例：纯标准库 tools+skills；可选 MCP tools+skill；无需改 Core 即装配 |
| 33 | E3 | MCP 扩展路径保留两协议×两传输真实调用；错误/审批/漂移/UNKNOWN 至少各有实际集成证据 |
| 34 | E3 | 独立探针覆盖冲突原子性、部分启动回滚、撤权旧引用、版本漂移拒绝和相同部署跨进程恢复 |
| 35 | E3 | 诊断只读零启动；嵌套秘密与异常 canary 不进公开报告、模型、事件或持久记录，报告总量有界 |
| 36 | E3 | 新旧 API、持久化格式、导出/包内容及手工装配示例回归通过；格式新增有读旧/未知版本测试 |
| 37 | E3 | base/extra 独立环境、wheel/sdist 树外安装与新示例成功；各环境应跑的扩展节点不得全 skip 或零 collected |
| 38 | E3 | Python 3.10 真实解释器运行扩展基础合同与生命周期/恢复关键用例；缺解释器标 BLOCKED，不以静态解析替代 |
| 39 | E3 | 原 MCP 专项、独立 R1–R5 探针、普通工具/skills/任务恢复及全套回归无新增失败 |
| 40 | E3 | API/开发指南、阶段进度、逐 AC 交付报告与 SNAPSHOT 齐全；实际版本/环境/失败/skip/blocked 如实记录 |

## 8. E3 示例、故障窗口与命令

### 8.1 两个参考扩展

1. **ReferencePack**：一个受限内存或临时目录检索工具、一条按需 skill 和一个资源；
   另带需要审批的临时文件写工具用于撤权/恢复示例。base 环境可运行，不读真实资料。
2. **McpReferencePack**：复用当前 MCP 夹具的读/写工具与一条使用指导 skill，宿主显式给定配置和绑定。
   同时提供无 extra 的明确失败示例；退出无孤儿进程，不用付费模型、真实凭据或外部服务。

建议 `examples/13_extension_tools_skills.py` 与 `examples/14_extension_mcp_recovery.py`。
脚本化 Provider 可以固定模型输出；Harness、Gateway、SQLite、审批、进程、MCP 连接和远端效果必须真实。

### 8.2 故障注入边界

用精确的 factory acquire、registry publish、intent admit、receipt settle、Run commit 边界注入失败，
不靠固定 sleep 猜窗口。跨进程测试必须让另一进程重新读取 Run 与唯一账本，并计数真实效果。
E2 至少覆盖：启动取得资源后失败；批次第一项成功后第二项失败；审批暂停后撤权/换版本；
远端写已发生但 receipt 未确认；receipt 已确认而 Run 未更新；父任务等待 child_result 时部署漂移。
复用既有夹具和故障辅助，但须新增扩展实际装配路径的证明，不能只引用纯比较函数或旧通用测试。

### 8.3 验证入口

现有可用入口：

```bash
.venv/bin/python -m pytest -q tests
.venv/bin/python -m pytest -q tests/test_import_fence.py tests/test_python_floor.py
.venv-mcp/bin/python -m pytest -q tests/mcp
.venv-mcp/bin/python scripts/probe_mcp_independent_review.py --report /tmp/ext-mcp-r1-r4.json
.venv-mcp/bin/python scripts/probe_mcp_failed_refresh.py --report /tmp/ext-mcp-r5.json
.venv/bin/python scripts/export_public_snapshot.py --refresh-manifest
git diff --check
```

E3 建议新增入口（目前不存在，实施者实际创建并验证后填入交付记录）：

```bash
.venv/bin/python -m pytest -q tests/extensions
.venv-mcp/bin/python -m pytest -q tests/extensions tests/mcp
.venv-mcp/bin/python scripts/probe_extensions_acceptance.py --report /tmp/extensions-acceptance.json
.venv/bin/python scripts/extensions_gate.py --workdir build/extensions-gate --report build/extensions-gate/report.json
```

gate 可复用现有构建/安装工具，但验证对象是同一次构建的实际已安装 wheel/sdist。
注意独立脚本会主动插入源码目录；树外验证要断言运行时来自 site-packages，并避免脚本重新切回源码。
base 纯扩展测试必须实际运行；可选 MCP 节点的缺 extra skip 单列，extra 环境必须零该类 skip。
不硬编码“通过多少项才算成功”，须检查应有节点被收集、执行和退出状态。
3.10 没有解释器时，AC38 保持 BLOCKED，E3 交付只能标候选/部分验证；不能自己缩小 Python 声明后宣布全完成。

## 9. 交付要求

DeepSeek 最终交付 `docs/extensions-delivery-report.zh-CN.md`，逐 EXT-AC01–40 给出实际节点、
命令、返回码、结果及缺口；同时记录源码提交、包版本、公开 API、旧数据行为、安装来源、
故障窗口证据、资源清理结果、已知失败和未执行范围。
更新 README 中英文、contract、adapters、recovery、verification、开发指南和生成清单；
历史 `0.3.0a1` / MCP 门禁保留原版本与通过数字。新库功能不声称已完成宿主产品接入。

本方案不要求重新做 Yanxu 已有的真实模型实测，也不以其结果代替扩展新边界的验收。
完成实现后停在可独立复核状态，再决定 `0.4.0a1` 升版、main 合并、正式发行和宿主采用。
