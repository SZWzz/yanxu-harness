# Yanxu Harness 下一版 Alpha 实施计划

日期：2026-10-15。基线：首次导入提交 `321ef4f`；执行前确认当前 HEAD 和工作区，保留后续已有改动。

本文是交给 DeepSeek 的开发任务书，不代表这些功能已经实现或验收。

## 目标与交付范围

让外部开发者从本仓库安装后，通过具体 Harness 完成：

```text
输入任务 → 模型产生工具调用 → 预览修改 → 等待审批并持久化
          → 进程退出 → 新进程读取状态 → 批准并恢复
          → 只执行尚未完成的操作 → 模型收到结果并输出最终答复
```

成功标准是这条链路可以运行、检查、重启恢复，并能从 wheel 和 sdist 安装后复现。基础运行时继续只使用标准库，兼容 Python 3.10–3.12；尚未验证的平台和版本必须如实记录。

本轮不实现多任务调度器、subagent 执行、动态任务图、分布式 worker、插件市场、Web 控制台或生产沙箱。不改写现有双层循环；继续保留单一模型循环实现。只为后续任务编排保留身份、预算、取消和事件的扩展位置。

## 执行纪律

1. 按阶段 1 → 2 → 3 → 4 → 5 推进。每阶段单独提交，较大的阶段拆成可独立检查的小提交。
2. 先运行基线并记录真实结果。行为改动先加有意义的失败测试；不得通过删测试、吞异常、扩大 skip 或降低断言消除失败。
3. 小范围验证通过后，运行该阶段相关检查。最终统一运行完整门禁，不为了凑次数反复执行。
4. 复用现有 Core、Gateway、ApprovalStore、ExecutionBackend、Persistence 和 checkpoint 类型。允许添加适配层和必要端口，禁止复制另一套模型循环、审批账本或执行账本。
5. 不对任意外部副作用承诺 exactly-once；不把租约失效、取消、结果未知转换成成功；不自动重试未知操作。
6. 现有测试或文档与新需求冲突时，说明冲突和兼容方式。兼容层只在有现存调用方或测试依据时添加，避免预建复杂抽象。
7. 不自动更改 GitHub 账单、仓库可见性或上传到公开包索引。本轮先交付本地可安装产物与发布说明。
8. 每阶段报告：改动、命令与结果、剩余限制、对应提交。报告中区分本地验证、远程 CI、真实模型验证。

## 当前已知事实与待核对点

| 位置 | 当前状态 | 本轮处理 |
| --- | --- | --- |
| `runtime/harness.py` | `Harness` 为 Protocol，只有生命周期契约 | 保留端口，新增具体实现 |
| `core/agent_loop.py` | 唯一模型循环；内层处理工具和 steering，外层处理 follow-up | 复用，按需补结果与暂停边界 |
| `providers/protocol.py` 与 `core/agent_loop.py` | Provider 中立事件流与 Core 的 `stream_fn`/`result()` 不是相同接口 | 增加明确适配器并测转换 |
| `core/types.py` 与 `tools/definition.py` | Core 使用 `ToolSpec`，Gateway 使用 `ToolDefinition` | 通过适配器接入，标准入口走 Gateway |
| `runtime/context.py` 与 `core/cancellation.py` | 两种取消令牌 | 明确桥接和清理所有权 |
| `events.py` 与 `core/events.py` | 持久事件和 Core 事件是两套用途不同的类型 | 明确映射，持久事件提交后再发布 |
| `storage/sqlite.py` | 已有 SQLite 状态、事件、CAS、租约能力 | 检查原子迁移能力是否在公共端口暴露 |
| `scripts/ci_check.py` | `REPO = PACKAGE.parents[1]` 仍假定原仓库层级 | 修正工作目录和清理边界 |
| `.github/workflows/harness.yml` | pip 命令的版本约束未引用，shell 会把 `>` 当重定向 | 引用每个约束，核对依赖准备结果 |
| 文档与快照 | 存在旧路径、未实现声明、旧来源描述 | 按当前独立仓库事实修正，保留真实历史 |
| GitHub Actions 首次运行 | 7 个 job 因账户付款或消费上限问题未启动 | 作为外部阻塞记录；本地照常验收，不宣称远程通过 |

上述事实以执行时源码为准。先核对再改，不把候选问题直接当作已复现缺陷。

## 阶段 1：整理独立仓库，建立可信基线

### 工作

1. 在独立虚拟环境中安装开发 extras，运行当前测试、四个示例、构建及 release verifier；保留失败摘要，不引用旧的测试数量作为本次结果。
2. 修正 `ci_check.py` 的根目录、cwd 与清理逻辑。所有清理只能作用于本次创建且有明确归属的产物目录；不得按旧父目录推导删除仓库之外的目录。`--keep-artifacts` 必须实际生效，或明确迁移该选项。
3. 修正 workflow 和脚本文档中的 shell 引用，例如 `'setuptools>=77.0.3'`。构建后端约束与 `pyproject.toml` 一致；开发依赖准备至少满足声明的 extras 和构建依赖，不临时修改基础依赖。
4. 检查构建、离线安装和源目录外运行是否真的隔离。不能依赖 editable 安装、当前目录或外部宿主目录意外可见。
5. 修正文档中不可执行的旧命令，以及错误的“没有远程仓库”“SQLite 尚未实现”等现状说明。保留许可证、上游归属和有用的迁移历史，不全局删除宿主相关文字。
6. 明确 `SNAPSHOT.json` 的角色：保留为历史来源证明，或由更新后的导出流程重新生成当前快照。不得留下“本仓库当前文件仍全部匹配旧摘要”的误导。更新 exporter 的 canonical source 描述，保留白名单和敏感内容扫描。
7. 必要时同步 `.gitignore`、`MANIFEST.in` 和快照白名单，保证新增交付文件被正确打包或明确排除。CI wheelhouse、日志、缓存和构建产物不能进入源码包。

### 验收

- 在不包含原宿主仓库的新目录或干净克隆中，本地 CI 脚本可以运行。
- 开发依赖准备后，`--offline` 门禁不访问索引，缺依赖时明确失败。
- wheel 和 sdist 可从源目录外安装并运行测试和现有四个示例。
- pip 依赖准备不会创建由重定向产生的异常文件。
- 根目录推导与清理范围有针对性验证，特别是仓库外的哨兵文件不受影响。
- 记录真实基线，包括失败、跳过及其原因；若是已有代码缺陷，给出最小修复或明确归入后续阶段，不隐瞒。

建议提交：`fix: make standalone checks and packaging reproducible`。

## 阶段 2：实现具体 Harness，接通执行链路

这是本轮主体，建议拆成 2A–2D 四个提交。新类和文件名可按现有风格调整；不要重命名现有 `Harness` Protocol 来冒充实现。

### 2A：模型与工具适配

- 新增 Provider → Core stream adapter：转换请求、工具声明、文本/思考增量、工具调用、usage、停止原因和失败。
- 增量流与最终 `AssistantMessage` 必须一致；多工具交错参数不能混合。兼容事件缺字段时明确拒绝或降级，不伪造 token 用量和最终答案。
- 私有签名和续传材料与公开事件分离；保留真实续传所需材料，禁止简单丢弃导致恢复不可用。
- 标准入口将 `ToolDefinition` 适配为 Core 工具，但执行必须调用现有 `ToolGateway`；Gateway 仍是审批与 intent/receipt 的唯一入口。
- 保留 `executed/replayed/awaiting_approval/deferred/denied/invalid_args/failed/cancelled/unknown` 的区别，不能统统压成 `is_error=True`。
- 宿主委托执行模式继续通过既有 backend ownership 工作，不重复写账本。
- 核对 provider 当前同步 HTTP 迭代在 async 方法中的行为：用受控慢流验证不会长时间阻塞事件循环。需要修复时采用最小的标准库桥接，明确超时、连接关闭与工作线程清理边界。

验收：通过本地 HTTP 流式服务器驱动真实 Provider，再经过适配器进入同一 Core，完成一次有工具调用和最终回答的运行。测试不需要外网或真实密钥。

### 2B：具体运行对象与明确结果

- 新增具体实现，例如 `DefaultHarness`，依赖通过构造函数显式注入；从 `runtime` 的公共入口导出。
- 实现现有 `start/resume/cancel/state` 契约。身份由可信宿主传入，不能由模型参数建立身份、权限或扩大工具可见性。
- 明确活动态的表示。当前 `RunStatus` 缺少运行中状态，如新增 `RUNNING`，必须同步状态迁移表、序列化、测试和文档；活动态不能作为已结束的 `RunOutcome` 返回。
- Core 提供机器可读的结束原因。推荐新增内部结果入口并保留现有列表返回 API；选择其他方式也必须显式、可测，不根据最终文本、空列表或事件数量猜测。
- 正常完成映射 `COMPLETED`；轮次耗尽映射 `USAGE_LIMITED` 并带 error code；取消映射 `CANCELLED`；模型失败映射 `FAILED`；等待外部决定映射 `SUSPENDED` 并带 interaction。
- `state()` 从持久状态读取。新实例必须能读取先前运行状态，而非只查询内存中的 Agent。
- `start()` 对同一运行身份的重复提交和冲突输入给出确定规则：相同请求复用既有状态，输入漂移明确拒绝，不覆盖已存在运行。

验收：一个公开入口可启动运行、读状态、区分完成/预算/失败/取消；公共调用方无需了解 Core 私有函数。

### 2C：审批暂停、检查点和重启恢复

- 等待审批时停止继续请求模型，持久化完整工具批次、各调用状态、未回答交互、原始消息及当前指纹。
- 不能把等待审批写成已完成的普通工具结果后，让模型自行继续。批准恢复后必须补齐与原 `tool_call_id` 对应的结果，再调用模型。
- 已有 checkpoint 用已执行/未执行划分批次。明确“等待审批、已有 intent 未结算、已有回执”如何表示；必要时版本化扩展，不能把不确定操作当作可重放的未执行调用。
- 一批多个工具时，记录所有已结算结果；暂停后不再启动新调用，已开始的调用按真实回执收尾。最小实现可在含审批写操作的批次采用串行策略，但必须文档化并保持结果顺序。
- 恢复保持 namespace/session/run，使用新 attempt；验证配置、工具、指令、策略和可见工具清单指纹。变化时按安全规则拒绝或重新确认，返回具体原因。
- 审批决定由可信宿主决定入口写入 ApprovalStore。`resume(resolution)` 不能把任意客户端的 approved 布尔值或 actor 字符串直接当成授权事实。
- 每个本地运行状态迁移，尽可能将状态、checkpoint/interaction 引用、必要的持久事件放进同一事务。如现有 `Persistence` 端口能力不足，补充最小原子迁移端口或能力协议，并覆盖内存/SQLite 实现。
- 审批库、操作库、运行库可能位于不同连接或不同文件。不得宣称它们共享事务；定义恢复时如何根据引用及已有回执补齐状态，并用故障测试验证。
- 持久迁移提交后再对外发布事件。发布失败后从 cursor 重放，不能回滚业务事实或重跑工具来弥补通知。
- `unknown` 继续是操作级结果。运行可挂起等待 reconciliation，并提供明确的 interaction 类型；证据不足不得完成运行或自动重试。核对接口只接受可信业务证据，不能把操作员点击“继续”等同于写入成功。

验收：在一个进程暂停后退出，用新进程和新 Harness 实例恢复同一运行；模型看到原工具调用的准确结果，已有回执不重复执行，未知结果保持未知。

### 2D：取消、事件和身份隔离

- 明确 runtime Cancellation 与 Core CancellationToken 的桥接；取消传给 provider、工具和本次启动的辅助任务。结束后释放传播任务、流、连接和租约，不留下后台任务持续写状态。
- 写操作开始后收到取消，不等于副作用已回滚。执行状态根据回执或未知证据结算，禁止把已发生的写入伪装成“什么都没做”。
- 事件在进入 reducer 前验证运行/attempt 身份及活动状态；迟到回调不能污染新运行。不能先改内存状态再检查是否合法。
- 用公共事件 envelope 或等价方式关联 namespace、run、attempt、顺序和 schema；明确 Core 临时事件与已提交持久事件的边界。
- 本阶段保证同一运行不会被两个恢复者同时驱动。使用租约和 CAS，定义超时、续租或最大运行时间；租约失效后禁止启动新副作用及提交过期状态。
- 存储键、operation key 和缓存/恢复查询必须避免不同 namespace 的 ID 碰撞。若端口只接受 run_id，使用无歧义的复合内部 key 或版本化扩展，并在读取时核对身份与宿主权限。

验收：取消可观测且有界；取消/完成后无迟到写入；并发恢复只有一个有效执行者；跨 namespace 不能读写、批准或回放彼此的运行。

建议提交：`feat: bridge providers and tools to the core`、`feat: implement the harness lifecycle`、`feat: persist and resume approval-bound runs`、`fix: fence runtime events and cancellation`。

## 阶段 3：交付一个贯穿全流程的业务示例

新增一个主示例，例如 `examples/05_durable_approved_edit.py`，保留原四个示例。

### 工作

1. 示例只使用公共 API，通过阶段 2 的具体 Harness 运行，禁止自己手写模型循环或直接代替 Harness 调用 Gateway 来完成主要链路。
2. 采用带 `version` 的小型业务记录。用 SQLite 条件更新或等效原子 CAS 完成版本检查与写入；不能只接收模型给的 expected_version 后直接写文件。
3. 预览展示真实当前值和拟修改值；审批后再次检查真实目标版本，旧版本不能覆盖新数据。
4. 默认使用实现公共 Provider 协议的确定性假模型，明确标注模拟。主示例的多步结果由模型流经 Core 触发，不能用固定成功日志冒充执行。
5. 支持多个独立进程命令完成 `start`、`status`、`approve`、`resume`；在文档写出准确参数。示例的批准命令是可信本地操作员入口，不代表已经具备 Web 用户认证。
6. 提供独立可选的真实模型演示，配置由宿主注入，密钥只从明确配置入口解析。没有密钥时不影响默认示例和 CI；不在自动门禁调用付费外网服务。
7. 现有 verifier 会运行 `examples/*.py`。新增主示例无参数执行时必须自建临时目录、通过真实子进程完成确定性演示并退出；真实模型脚本另放可选目录，或显式调整 verifier 的示例清单，禁止因缺密钥挂住验收。
8. 显示 run/attempt、审批预览、状态、操作回执、结果摘要。重复恢复能证明 handler 没有重复执行。

### 验收

- 开发者照 README 的命令即可完整操作，不需要修改示例源码。
- `start` 停在等待审批，退出后业务记录未变；新进程批准并恢复后，记录只改一次，版本只增加一次。
- 再次执行 resume 不会再次调用写工具或重新生成不必要的模型请求。
- 示例可从安装后的 wheel、sdist 对应环境运行，不能依赖当前源码目录。

建议提交：`feat: demonstrate durable approval and resume end to end`。

## 阶段 4：用故障与对抗测试验证执行边界

前几阶段已经包含随实现新增的测试。本阶段集中补齐跨模块、真实进程和真实存储的组合验证，不重复添加只验证赋值和实现细节的测试。

| 编号 | 场景 | 必须观察到的结果 |
| --- | --- | --- |
| T01 | 正常对话与流式工具往返 | 同一 Core 被调用，增量与最终结果一致，完整记录结束原因 |
| T02 | 达到 max_turns | 当前获准批次按契约结算，不请求下一轮，返回 usage_limited |
| T03 | 暂停前未批准 | 没有写入、没有未经批准的 intent；运行可从磁盘读取 |
| T04 | 批准后参数/工具/策略/预览/目标版本漂移 | 旧审批失效，有可辨别的原因，历史决定保留 |
| T05 | 目标在最终检查与写入之间变化 | 业务存储的原子 CAS 拒绝过期写入；无部分覆盖 |
| T06 | 等待审批后杀进程并恢复 | 同一 run、新 attempt、原工具 ID 对应正确、无伪造成功结果 |
| T07 | intent 后、effect 前崩溃 | 重启仅凭 intent 无法证明 effect 未发生，默认不重放，进入核对流程 |
| T08 | effect 后、receipt 前崩溃 | 业务侧可见副作用，运行不宣称成功或自动重试，unknown 可查询 |
| T09 | receipt 后、checkpoint 更新前崩溃 | 从持久回执补齐运行记录，不重复执行 handler |
| T10 | 同批混合已完成/待审批/未知工具 | 结果分区完整、顺序正确、既有副作用不重复、未知调用不混入待执行集合 |
| T11 | 两个进程同时恢复相同 run | 只有一个有效持有者可以启动新调用和提交状态，旧 fence 被拒 |
| T12 | 跨 namespace 使用相同 run/call/operation 字符串 | 不串数据、不复用回执、不复用审批；违规查询/恢复被拒 |
| T13 | 重复批准、重复 start、重复 resume | 相同输入按定义幂等，冲突输入明确拒绝；重复恢复不重复写入 |
| T14 | 拒绝或过期批准 | 不执行写入；重新确认遵循显式规则，原决定不被无痕覆盖 |
| T15 | provider 等待期间取消、工具执行期间取消 | 终态准确、清理有界；已发生的外部副作用不被隐藏 |
| T16 | 旧 attempt 的迟到事件/更新回调 | 在改变运行状态前被隔离，不污染下一次运行 |
| T17 | 持久提交后事件消费者失败 | 数据不回滚，cursor 能补读，不重跑副作用 |
| T18 | provider 错误、非法帧、工具参数错误 | 保留可读原因和机器状态，不能伪造完成或 usage=0 |
| T19 | future schema 或损坏 checkpoint | 明确拒绝，不猜测解析，不继续执行 |
| T20 | 公开事件与运行记录 | 无注入的测试密钥/headers/私有签名；必要私有续传材料与公开出口隔离 |

### 测试方法与验收

- 故障窗口通过子进程、落盘哨兵和明确同步点触发；不用依靠随机 sleep 假装确定性覆盖。
- 对 effect 是否发生，用业务存储的实际行/版本/调用计数作证；不能只检查 mock 被调用。
- 对事件顺序、未结算操作和状态写入，用真实 SQLite 与新连接/新实例验证。
- 测试必须有等待超时和子进程回收，失败时输出可诊断信息。
- 运行已有测试和新增用例，确认基础依赖隔离、Python 下界、许可证与安装产物检查仍成立。
- 多平台结果据实记录；GitHub 账单阻塞不等于测试失败，也不等于测试通过。

建议提交：按缺陷与对应测试拆成若干 `test:` / `fix:` 提交，不集中成无法审查的大提交。

## 阶段 5：整理接入体验，交付可安装 Alpha

### 文档和 API

- README 中英文同步：定位、安装、最小运行、审批恢复示例、运行状态、取消、限制与错误处理。
- `docs/contract.md` 按最终实现重写有关可用能力的描述，补标准入口、状态迁移表、恢复规则、身份与审批信任边界。
- `docs/adapters.md` 说明宿主如何接入 provider、工具、业务版本检查、权限、存储与委托网关。
- `docs/recovery.md` 说明 unknown 核对、租约、重复提交、崩溃窗口和跨库一致性边界。
- 不声明工具策略等于操作系统沙箱；不声明审批记录本身完成用户认证；不声明任意外部工具 exactly-once。
- 为以后多任务/subagent 写一小节扩展边界：复用相同 Harness/Core，身份来自宿主、权限不扩大、预算和取消可接入。只保留有当前调用依据的接口，不实现调度器。
- 基础包继续无第三方运行依赖；新文件通过公开导出和 API 测试进入契约。记录发生变更的持久格式与迁移/拒绝规则。

### 版本与产物

建议目标版本 `0.1.0a2`；执行时若已有后续版本，选择合适的新 Alpha 并同步版本、changelog、契约及测试，不覆盖已有版本历史。

交付 wheel、sdist、SHA-256 清单和验证报告，放在构建产物目录，不把二进制产物提交进源码。

完善 release verifier，使它验证发布包的真实内容而非 editable 安装；源码包是否包含脚本和示例需要明确约定并实际检查。该约定与 `MANIFEST.in`、导出白名单保持一致。

本轮发布准备完成的含义是“产物可安装并经过验证”。公开 PyPI/TestPyPI 上传、GitHub Release 和标签另按用户指定的渠道执行；私有仓库保持私有。不要因尚无发布凭据而把本地准备工作留空。

### 最终验收

1. 干净克隆 → 新 venv → 开发安装 → 完整测试和示例通过。
2. 准备好依赖 wheelhouse 后，离线 CI 检查通过；外部索引不可用时仍能完成。
3. wheel 与 sdist 分别安装到源目录之外的新 venv，断言 import 来自安装目录。
4. 两种安装产物均能运行端到端审批与跨进程恢复演示。
5. 最低 Python 版本真实运行通过；其余声明支持的版本和平台列出本次证据，未执行者不填写成功。
6. README 中每条必需命令实际执行过；主示例与文档使用一致的公共 API。
7. 报告包含最终提交、测试结果及 skips、产物路径与摘要、已知限制、远程 CI 状态、真实模型验证状态。
8. 工作区没有未说明的临时文件、凭据或构建垃圾；不把当前不完整工作标记为完成。

建议提交：`docs: document the standalone harness alpha`、`chore: prepare the next installable alpha`。

## 建议验证命令

以下从仓库根目录执行；在专用 venv 内运行。wheelhouse 要按解释器/平台分别准备，不能把一种平台的原生 wheel 当作通用依赖。

```bash
python -m venv .venv
# 激活该环境：POSIX 使用 . .venv/bin/activate；Windows 使用对应 Scripts 激活命令。
python -m pip install -e '.[dev,yaml,pydantic]'
python -m pytest -q tests
python examples/01_basic_dialogue.py
python examples/02_approved_data_edit.py
python examples/03_cross_process_recovery.py
python examples/04_nonfiction_tools_skills_decisions.py
python -m pip download --only-binary=:all: 'setuptools>=77.0.3' wheel 'pytest>=7' 'pytest-asyncio>=0.21' 'build>=1.0' 'pyyaml>=6.0' 'pydantic>=2.0' -d build-wheelhouse
python scripts/ci_check.py --offline --wheelhouse build-wheelhouse
python scripts/verify_release.py --offline --wheelhouse build-wheelhouse --keep
```

版本约束以执行时 `pyproject.toml` 为准，必要时同步上面的依赖准备命令。阶段 3 新示例的具体命令由实现确定并补入 README、verifier 与最终报告。不要在本轮规划交付中宣称以上命令已经执行通过。

## 给 DeepSeek 的启动提示词

> 请按 `docs/alpha-implementation-plan.zh-CN.md` 完成 Yanxu Harness 下一版 Alpha。先读取仓库指令，检查当前 HEAD 和未提交改动，再执行阶段 1 建立真实基线。按阶段推进并提交，报告命令与结果。复用现有模型循环、工具网关和持久化能力，优先接通具体 Harness 及跨进程审批恢复。行为改动先用有意义的测试复现，不通过删测试或放宽断言让检查变绿。遇到可自行解决的普通实现选择继续推进；只在关键产品契约确实无法确定时列明问题。GitHub 账单阻塞不影响本地开发；不要改变账单、仓库私有性或上传到公开索引。最终交付经过源目录外安装验证的 wheel/sdist、文档、端到端示例和真实验证报告，并明确尚未完成的项目。
