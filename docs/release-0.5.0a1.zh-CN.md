# 0.5.0a1 候选与发行记录

日期：2026-10-30，Asia/Shanghai。状态：发行准备；尚未创建 `v0.5.0a1` 或上传 PyPI。

本轮在已发布 `0.4.0a1` 基础上增加可选 Plan/DAG、执行绑定、typed 共享预算、步骤上下文与关联诊断。版本升为 `0.5.0a1`，保持 Alpha。原发行记录、标签和文件保留原归属，不以新源码替换旧制品。

## 功能与修复

- Plan 定义不可变且有 revision，可信批准绑定完整定义和 envelope；步骤、Task、INITIAL 输入与 claim 原子准入。Task completed 与 Step accepted 分开，依赖由验收推进。
- pause/cancel/replan/retry、outbox 和有界 reconcile 沿用既有 Run/Gateway 权威。unknown 副作用保持阻塞；重规划等待静止边界。
- 执行绑定冻结宿主解析出的部署引用；恢复和预算预留后再次检查实际执行范围。宿主仍负责认证、资源权限和隔离。
- 一个规范 dispatch reservation 覆盖 Plan、Task-root 与子 grant 范围；策略摘要集在事务内校验，计量有单位、额度跨 revision 累计，unknown 不自动释放。
- Gateway 持久记录 handler 阶段证据；仅明确未进入 handler 的操作才允许释放派发预留。取消和 receipt 后结算采用可恢复路径，`WorkResult.to_record()` 保留兼容。
- 步骤材料带来源、版本、授权和有界 manifest，经现有 ContextController 接入真实请求路径；完整 transcript 独立保留。
- 新增 Plan/预算只读诊断、可选时间线读端口、确定性时间线 fixtures 和离线示例 15–20。fixtures 不证明真实模型任务质量，通用宿主 source adapter 仍需显式提供。

契约入口：[Plan](plans-contract.zh-CN.md)、[执行绑定](execution-binding-contract.zh-CN.md)、[预算策略](budget-policy-contract.zh-CN.md)、[预算预留](budget-reservation-contract.zh-CN.md)、[预算派发](budget-dispatch-contract.zh-CN.md)、[时间线](diagnostics-timeline.zh-CN.md)。审查与修复证据见[独立复核](harness-next-stage-independent-review.zh-CN.md)。

## 升级和持久化边界

| 范围 | 当前格式 / 开启方式 | 升级行为 |
| --- | --- | --- |
| 原 Run / 普通 Task | 原有入口继续可用 | 不强制采用 Plan、typed budget 或 Step context |
| Plan 定义 | schema 1 | 未知版本拒绝；内容修改创建新 revision |
| Plan 控制存储 | schema 3；`enable_orchestration=True` | 可写旧 v1/v2 按合同事务迁移；只读路径不迁移 |
| typed 预算 | schema 2；`enable_typed_budgets=True` | 显式启用时事务迁移；旧 Task 事实仅归真实 Task root，不伪造 Plan coverage |
| Core 执行快照 | 当前 schema 5 | 依照既有版本读取/拒绝规则；未知未来版本拒绝 |
| receipt 阶段证据 | 可选 `handler_entered` 事实 | 旧记录缺证据时不推定 handler 未运行，不以 neutral 状态自动退款 |

启用新控制 schema 前由宿主备份数据库并在副本验证。迁移不承诺 downgrade；回退旧 wheel 不等于已迁移数据可安全回退。host-owned 后端缺少原子预留、绑定或恢复能力时明确拒绝对应能力。

## 发行门禁

实际运行结果将在通过后追加。历史候选通过数只认证其原源码；本轮必须对升版后的提交重新验证。

1. 包元数据与 `__version__` 同为 `0.5.0a1`；CHANGELOG、README、公开契约和源码快照一致。
2. 当前源码完整离线 CI、wheel/sdist 各自树外安装、复制套件与全部适用示例通过。
3. GitHub `harness` 矩阵：Linux Python 3.10/3.11/3.12，macOS/Windows Python 3.12，以及链接/许可证检查通过。
4. 合入 main 后在同一提交运行发布工作流 dry-run；核对制品 SHA256、元数据与报告。
5. 在已验证 main 提交创建不可变 `v0.5.0a1` 预发布 Release；既有 OIDC 工作流只上传该次验证的制品。
6. 从正式 PyPI 使用无缓存新虚拟环境安装精确版本，核对 distribution/runtime 版本、导入来源，并运行离线示例。

制品哈希保存在实际构建输出的 `SHA256SUMS`，不把自身 sdist 哈希写回同一次构建输入。平台各自构建验证不等于所有平台运行了完全相同的 wheel 字节。真实付费模型、生产多进程容量与跨主机调度不属于本次发行认证。
