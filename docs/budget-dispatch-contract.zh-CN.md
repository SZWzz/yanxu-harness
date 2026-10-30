# B2：真实派发、usage 结算与恢复合同

日期：2026-10-29。

本合同定义 B1 typed reservation 如何接入真实模型、辅助模型和 Gateway 工具派发。B1 的 scope 推导、事务边界与旧 `TaskBudget` 兼容性保持不变。

## 1. 模型请求准入

`TypedDispatchBudget` 可传给 `DefaultHarness(budget=...)`。每个 Runner `request_instance` 对应一次真实模型请求；存储在同一事务中预留 `model_requests=1` 和当前请求所有 hard token/cost 上界。任一适用 scope 拒绝时，整批无写入且 Provider 不会被调用。

`BudgetLimit.hard_bound_ref` 必须能在宿主提供的 `upper_bounds` 注册表中找到。注册表的值是非负整数，或接收当前请求 payload 并返回非负整数的函数。payload 可读 `model`、最终 `messages`、`max_output_tokens`、Task、Run 和 request identity。一个 unit 在所有适用 hard scope 中必须解析到相同上界及一致的 cost 元数据。引用缺失、计算结果无效或不同 scope 上界不一致时，Budget adapter 在预留前以 `ContractError` 拒绝。

策略快照读取发生在批量预留事务之外。预留调用携带读取到的完整 owner→digest 集合；Memory 锁或 SQLite 事务内重新推导完整适用 scope 并比较当前策略摘要。集合不一致（包括策略新增、删除或修改）时，在任何 reservation 写入及 Provider 调用之前拒绝本次派发。该集合摘要也覆盖未生成 usage dispatch 的 hard token/cost 限制，避免只校验 `model_requests`。

上界函数必须对应 Provider 实际执行的约束。例如输出 token cap 必须进入 wire request；估算值或策略自述不能充当上界。首版不提供 tokenizer 或价格表。没有可执行输入 token、输出 token 或 cost 上界时应配置 `observe`，不能声称相应用量受 hard 限制。

当前 Core 把一次 `stream_fn` 调用计为第一次模型 dispatch。内建 OpenAI-compatible transport 的 `send` 合同禁止自动重试。需要在 adapter 内重试的宿主必须把同一个 `TypedDispatchBudget` 和 Task/Run 请求上下文显式传给 adapter；每次后续远端请求都要先调用 `reserve_provider_retry(payload, retry_identity=...)`，响应后用 `settle_auxiliary(...)` 结算。`retry_identity` 在该 Run 内必须稳定且唯一。没有把 adapter 重试接入此门禁时，该 adapter 不能声称 `model_requests` hard limit 覆盖其隐藏请求。

## 2. Usage 与故障窗口

- Provider 返回的 `Usage` 是实际事实；`input`、`output` 与可选 cost 报告分别进入 typed 单位。cost 必须携带与预留一致的币种和 price version。
- 仅在有明确“未进入外部派发窗口”证据时释放。Runner 的部署 guard 位于预留前；预留后、Provider 入口前的取消可释放；一旦进入 stream 函数，超时、取消、异常或无结果都保留为 `unknown`。
- Provider 已返回但没有报告某个 hard token/cost unit 时，该 unit 保持 `unknown` 并占用全部上界；不会填零。`model_requests` 仍结算为 1，因为请求已经返回。
- 实际值可超过预留。存储完整保留 actual；`TypedBudgetUsage.upper_bound_overrun` 统计 hard-bound 预留被实际值超过的总量，超额仍会参与 hard limit 计算并关闭后续准入。
- `record_typed_budget_usage(...)` 用稳定 `report_id + unit` 幂等补记迟到 usage。相同 report 可重放；当 usage unit 就是源 reservation 的 unit 时，同一 settled actual 不能被换用其他 report identity。跨 unit usage report 各自保留 source/report/unit 身份。
- 进程重启后未完成的 reservation 继续占用全部额度。新 Worker 将此前遗留的模型 reservation 标记为 unknown；相同 `request_instance` 不得再次授权外部调用。Gateway 用原 operation identity 读取并结算已有 receipt，receipt 重放不再执行 handler，也不新增 dispatch。

## 3. Gateway 与辅助模型

`ToolGateway(dispatch_budget=...)` 在准入、审批和执行绑定检查后、handler 入口前预留 `dispatches=1`。预留可能等待持久存储；返回后 Gateway 会再次检查执行绑定，再检查取消，之后才允许调用执行后端。撤权时记录 `handler_entered=False` 并释放本次预留。若协程在这次异步绑定检查中被取消，Gateway 会先持久化 `CANCELLED/handler_entered=False` 并结算预留，再向上传播取消。

工具 receipt 通过 `handler_entered` 记录实际阶段：`True` 的 dispatch 无论最终 receipt 是 succeeded、failed、rejected、cancelled、deferred 还是 unknown 都计入一次；`False` 才可释放；`None`（包括旧 receipt）不作退款证据，预算保持 unknown。SQLite operation ledger 将该阶段与 receipt 一起持久化，重放时恢复；receipt 状态本身不能证明 handler 未执行。

取消必须尊重 intent/admission 阶段：准入前已取消的调用只返回无 receipt 的 `CANCELLED`，不创建 ledger 行；intent 已提交、handler 尚未进入时的取消则持久化 `CANCELLED/handler_entered=False` 并释放已取得的 reservation。取消落在异步 budget reservation 中时，Gateway 等待该预留操作给出确定结果；若已取得 reservation，先写入未派发 receipt 并释放。若 reservation 已提交但 adapter 在返回 handle 前以取消或异常退出，Gateway 写入未派发 receipt 后按稳定 dispatch identity reconcile 已有 reservation；找不到时保持零 reservation，不因恢复补建预留。pre-reservation 的绑定拒绝 replay 使用同一 reconcile 规则。

receipt 写入和对应预算结算属于一个需完成的 durable work 边界。Gateway 对 handler 后结算、handler 前拒绝/取消、unknown 预算结算及 review reconciliation 使用 shielded settlement；调用方在此期间取消时，Gateway 等持久结算完成后再传播取消。SQLite receipt 与 typed budget 仍是两个独立存储，shield 不能把它们变成跨库事务；进程崩溃窗口保留给 receipt replay / review reconciliation 处理。

`TypedBudgetStore.reserve_typed_budget_dispatch(es)` 是供可信 host adapter 写入计量事实的存储接口：它从当前 scope policy 检查传入金额是否有额度，但不执行 `hard_bound_ref` resolver，也不能证明 token/cost 金额来自真实 Provider 请求。模型派发必须走 `TypedDispatchBudget`，由它从当前 payload 解析全部 hard usage 上界，并在同一事务校验完整策略摘要集合后才调用 Provider。直接使用低层 store API 的 host 必须自行保证计量事实可信；仅传入 digest 不会验证上界计算本身。

`DefaultHarness` 将 `TypedDispatchBudget` 传到其 `ContextController` 的摘要路径。摘要请求与主模型共享同一 Task/Plan/grant scopes 和 `model_requests` 额度。摘要成功但没有 usage 时，请求数会结算，hard token/cost coverage 保持 unknown；异常/取消在外部调用入口之后保持 unknown。摘要 cache hit 不派发，也不消费预算。

`ModelDecisionRouter` 接受可选 `dispatch_budget` 与宿主 `dispatch_context(requests)`。配置后必须为每次实际模型调用提供 Task/Run context，并在 host call 前预留；host failure 或取消保留 unknown。可由 `usage_extractor` 从 host response 提取 usage；没有 usage 时不会伪造零值。决策 cache hit 和本地输入长度/调用次数拒绝都不会派发。

没有配置 typed dispatch budget 的路径仍按其原有宿主合同运行；若策略宣称 typed hard budget，调用主模型、摘要模型、决策模型和 Gateway 的组件必须全部接入同一 typed adapter。

## 4. 可验证入口

- `examples/19_typed_budget_dispatch.py`：离线 fake Provider、辅助调用迟到 usage 和 Gateway receipt replay；不连网络。
- `tests/test_typed_budget_dispatch.py`：主模型与 hard usage 上界、拒绝/unknown/overage、辅助摘要/决策、Gateway 拒绝/replay、预留及结算期间取消、pre-admission 无痕迹、admission 后取消与迟到 usage。
- `tests/test_typed_budget_dispatch.py::test_provider_retry_gets_a_new_budgeted_request_identity`：首次请求结果未知时，后续 Provider retry 仍需要单独额度；重复 retry identity 不再授权第二次调用。
- `tests/test_typed_budget_store.py`：批量预留回滚、跨进程额度竞争、SQLite 重开、unknown 与 report identity 幂等。
