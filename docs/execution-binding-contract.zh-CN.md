# Plan 执行绑定契约（E0）

状态：**契约冻结，接口实现进行中**。该能力只用于宿主明确启用的 Plan admission；普通 Task 和既有独立 Run 不需要新适配器。

## 目标与信任边界

已批准的 `ExecutionEnvelope` 和 Step override 是权限上限。宿主在准入前显式解析其中的版本引用，再把解析得到的不可变快照与 Step Task 原子绑定。后续 Harness 工厂必须按此快照装配；真实 model/tool dispatch 前还要检查 binding 是否仍有效且实际部署仍覆盖批准范围。

库只验证宿主提供的数据在结构上完整并彼此一致。`RunContext.principal` 和 `ExecutionBindingRequest.principal_ref` 必须来自可信 host ingress；模型、Plan 文本、工具参数和 Task labels 都不是 principal。宿主可以存储稳定的 principal 引用，不得存认证 token 或凭据。

解析器不得调用模型、工具 handler、创建 workspace、打开业务副作用或改变审批状态。解析/授权 I/O 在 Plan control transaction 外执行。任意 Python Harness 工厂都可能谎报其部署描述；宿主装配代码是该描述的可信边界，库不声称提供沙箱或独立安全证明。

## 冻结的值类型

### `ExecutionBindingReferences`

五个带版本引用：`profile_ref`、`workspace_ref`、`policy_ref`、`deployment_ref`、`budget_ref`，字段类型均为现有 `VersionedRef`。它表示本 Step 的**有效引用集合**：Step 上的 profile/workspace override 优先，否则使用 Plan envelope 对应项；其他三项来自 envelope。它不包含解析后的活动对象。

### `ExecutionBindingRequest`

| 字段 | 语义 |
| --- | --- |
| `namespace`, `session_id`, `plan_id`, `revision`, `step_id` | 目标 Plan 和 Step 的稳定身份。 |
| `binding_id` | 按本文件的确定性公式提前得到的 Step binding ID。 |
| `retries_binding_id` | 显式 retry 的来源 binding；普通 admission 为空。 |
| `definition_digest`, `envelope_digest` | 当前已批准 revision 的定义摘要与 envelope 摘要。 |
| `references` | 上述有效、带版本的引用集合。 |
| `principal_ref` | 宿主认证主体的稳定引用，不是凭据。 |
| `schema_version` | 当前固定为 `1`；未知版本必须拒绝。 |

构造器检查 revision、身份、摘要格式、schema 与引用类型，并确保 binding ID 与 Plan Store 既有确定性身份一致。解析器接收该请求的快照，不需要收到 Plan 自由文本、完整凭据或可执行工厂对象。

### `ExecutionDeploymentDescriptor`

仅由可信宿主在实际装配后提供，包含非空的 `profile_fingerprint`、`tool_visibility_fingerprint`、`policy_fingerprint`、`deployment_fingerprint`、`budget_fingerprint`、`workspace_identity`、`workspace_version` 和稳定 capability ID 元组。capability ID 必须唯一并使用小写字母、数字、点、下划线或连字符。字段都必须是有界字符串；不得放工作区路径、密钥、连接、handler、环境变量或其他活动对象。

descriptor 的 capability 只是宿主声明可执行的合同。只有相应适配器真实检查/强制该能力时，宿主才能列出它。workspace 隔离、条件写入和撤权都不能通过添加标签获得。

### `ResolvedExecutionBinding`

由 `ExecutionBindingRequest`、resolver `VersionedRef`、`ExecutionDeploymentDescriptor` 和 schema 版本构成。它是递归不可变的 plain data；`to_record()` 只产生 JSON 兼容的新容器，不暴露活动对象。解析结果必须保留原 request，claim 存储前检查其 request 完全相同。

`digest` 是对规范 JSON record 的域分离 SHA-256，表示为 `sha256:<64 lowercase hex>`。摘要覆盖 schema、Plan/Step/binding 身份、批准摘要、有效引用、principal_ref、resolver ID/version 和完整实际 descriptor。摘要不认证 resolver，也不把不可信宿主变成可信宿主。

### `ExecutionBindingRefused`

解析器以此异常报告稳定 `reason_code` 和不超过 2048 UTF-8 bytes 的说明。reason code 使用稳定小写标识符；不得把异常堆栈、prompt、凭据或未筛选资源内容放进 detail。结构化拒绝发生在 admission 事务和外部 dispatch 之前。

## 端口

```python
class ExecutionBindingResolver(Protocol):
    async def resolve(
        self, request: ExecutionBindingRequest
    ) -> ResolvedExecutionBinding: ...


class ExecutionBindingGuard(Protocol):
    async def check(
        self,
        binding: ResolvedExecutionBinding,
        actual: ExecutionDeploymentDescriptor,
        *,
        boundary: ExecutionDispatchBoundary,
    ) -> ExecutionBindingGuardResult: ...
```

resolver 是有界只读解析器，外部授权检查可以 await。guard 的 `boundary` 当前限于 `model_dispatch` 和 `tool_dispatch`；每次新的真实 dispatch 都调用它，receipt 重放、已有事实读取、取消与结算不属于新 dispatch。`ExecutionBindingGuardResult` 的 `allowed=True` 不带拒绝字段；拒绝结果必须携带稳定 code 和有界说明。

## 确定性 binding ID

普通 binding identity 为以下规范 JSON 对象：

```json
{"identity_version":1,"namespace":"…","plan_id":"…","revision":1,"step_id":"…"}
```

显式 retry 时使用 `identity_version: 2` 并增加 `retries_binding_id` 字段。对象使用 UTF-8、`ensure_ascii=False`、按键排序、紧凑分隔符编码；binding ID 为 `binding-` 加该字节序列的 SHA-256 十六进制摘要。该公式与现有 Plan Store admission identity 一致，E 阶段由一个共享 helper 实现并由契约测试固定。request digest 仍由控制存储按其既有 principal 与 request identity 合同维护。

## 原子性与边界

宿主先读 Plan snapshot、构造 request 并 await resolver。随后存储事务重验 current revision、definition/envelope digest、approval/revocation、control version、principal_ref 和确定性 binding ID；再将 resolved binding、Step binding、Task、INITIAL 输入及 claim 一起提交。不能在 SQLite 写事务内等待外部解析器。

resolver 完成和事务提交之间，外部策略可能变化；control transaction 不与外部授权系统构成全局事务。admission 后的暂停/撤销阻止新 dispatch，已提交的 dispatch 不被宣称可回滚或瞬时中止。model/tool dispatch 的 guard 尽可能在各自实际派发边界重新核验。

## 不变量与拒绝

- request 身份、revision、approved digest、effective refs 或 principal_ref 任一不匹配时拒绝，不得按当前部署回填历史 binding。
- resolver 输出如果和 request 不同、含未知 schema、超界字符串、重复 capability 或活动对象，则拒绝。
- 工厂实际 descriptor 与 resolved descriptor 不一致时，在首个外部派发前拒绝。
- guard 返回拒绝、异常、缺少已启用能力或结果未知时，不发新模型请求或工具 handler。
- 持久化只保存可安全记录的标识、引用、版本和摘要；密钥、workspace 路径与运行时对象由宿主持有。
- 未启用执行绑定的既有 Run/Task 路径维持原行为；一旦 Plan admission 明确要求绑定，后端不支持持久化或校验该合同就必须在 admission 前拒绝。

## 后续阶段的职责

E0 交付这些类型、端口、摘要和确定性 ID 合同。E1 实现 Memory/SQLite 原子存储，接到 `LocalWorker` 与 `DefaultHarness`，在 model/tool dispatch 做 guard。E2 补 Gateway 资源版本与审批边界。E3 通过 SQLite/Gateway 的宿主示例覆盖进程退出、部署漂移和合法恢复。
