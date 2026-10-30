# 0.5.0a1 候选与发行记录

日期：2026-10-30，Asia/Shanghai。状态：已创建 `v0.5.0a1` GitHub 预发布，并已发布到正式 PyPI；公开安装验证通过。

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

## 准备期失败记录

首轮升版门禁在扩展兼容测试发现包版本仍被断言为 `0.4.0a1`，结果为 1 failed / 1,795 passed / 22 skipped。已将候选版本断言更新为 `0.5.0a1`，RunRecord/checkpoint 持久版本断言保持原样。该轮本地及远端任务已停止，不计作发行通过证据。

## 最终发布结果

发行源码为 `c6f8dca24b435c3ee7bab5802c3b1a02b7b0f712`，源码提交时工作树干净。通过 [PR #4](https://github.com/SZWzz/yanxu-harness/pull/4) 将同一提交快进合入 main，标签 `v0.5.0a1` 固定指向该源码。发布之后的本页证据补记属于后续文档提交，不移动标签、不重建或覆盖已发布文件。

| 检查 | 实际结果 |
| --- | --- |
| 本地聚焦门禁 | 217 passed；修正版本断言后的扩展/打包聚焦 26 passed |
| 本地完整离线 CI | macOS arm64 / CPython 3.12.13，六步全部通过；源码 1,796 passed / 22 skipped；wheel/sdist 分别树外安装、复制套件和示例通过 |
| 可选 MCP 源码回归 | MCP SDK 2.3.0，102 passed；不外推为所有平台的 MCP 认证 |
| 候选跨平台完整矩阵 | [40988868905](https://github.com/SZWzz/yanxu-harness/actions/runs/40988868905)，七个 job 全部成功；Linux Python 3.10/3.11/3.12、macOS/Windows Python 3.12，各自构建并安装 wheel/sdist、运行套件与示例；Linux/macOS 源码 1,796 passed / 22 skipped，Windows 1,795 passed / 23 skipped |
| main 同提交复跑 | [40991379313](https://github.com/SZWzz/yanxu-harness/actions/runs/40991379313)，main 快进后对相同 `c6f8dca` 的七个 job 再次全部成功 |
| 发布 dry-run | [40990710450](https://github.com/SZWzz/yanxu-harness/actions/runs/40990710450)，同一干净提交，Linux CPython 3.12.15，制品安装、文档/许可证、`twine check --strict` 与产物归档通过；手动触发没有上传 PyPI |
| 正式发布流程 | [40991940827](https://github.com/SZWzz/yanxu-harness/actions/runs/40991940827)，标签源码重新构建/验证，build 和 OIDC publish 两个 job 均成功 |
| 公开安装 | 从正式 PyPI 禁用缓存，在全新虚拟环境安装精确版本；runtime/distribution 均为 0.5.0a1，模块来自新环境 site-packages；基础示例 07/15/20 exit 0，随后安装 MCP extra 并运行示例 14 exit 0 |

已发布 [GitHub prerelease](https://github.com/SZWzz/yanxu-harness/releases/tag/v0.5.0a1) 与 [PyPI 0.5.0a1](https://pypi.org/project/yanxu-harness/0.5.0a1/)。Release 附带本次实际上传的 wheel/sdist、`SHA256SUMS`、`verification-report.md` 与 `pypi-install-smoke.md`。官方 PyPI 返回的两份文件摘要已逐一与这些制品比对。

| 制品 | bytes | SHA-256 |
| --- | ---: | --- |
| `yanxu_harness-0.5.0a1-py3-none-any.whl` | 749530 | `7628bdf6d6133855289d616f1b07ed4e2257a0b80d24248b5536fe5a2efad5ee` |
| `yanxu_harness-0.5.0a1.tar.gz` | 1810527 | `6ccf0a924de1a918045691f0323c2677a224fc12aace35a9bdbc3fd2451826cb` |

上表摘要属于标签源码产生的实际发行制品，记录时制品已构建并上传。本页更新不属于该次 sdist 的构建输入，因此不构成自引用摘要。各 CI 平台自行构建的文件和 dry-run 文件可能具有不同构建时间/压缩字节，不以它们替代实际上架文件。

安装命令：

```sh
python -m pip install --index-url https://pypi.org/simple/ 'yanxu-harness==0.5.0a1'
```

本次没有调用付费模型，也没有认证真实模型任务质量、生产容量、跨主机调度或宿主隔离。原 0.4.0a1 的发行文件、标签和验证记录保持原归属。
