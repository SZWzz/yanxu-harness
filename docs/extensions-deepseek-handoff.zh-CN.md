# DeepSeek 交接：Harness 扩展协议与装配层 E0–E3

日期：2026-10-20。状态：**任务书已准备，代码尚未实施**。

请按 [完整实施方案](extensions-implementation-plan.zh-CN.md) 实现独立 `yanxu-harness`
的轻量扩展机制。编制时源码基线为 `9aed198872916c9b3d1496a4647ed24875364ef4` / `0.3.0a1`。
方案提交在 `codex/extensions-plan` 分支；接手时重新核对工作树和 HEAD，并保留已有修改。
进度与证据写入 [阶段记录](extensions-progress.zh-CN.md)，最后交付逐 EXT-AC01–40 的报告。

## 目标与边界

统一装配 tools、skills、可选 MCP 工具包，提供冻结身份、宿主显式绑定、原子发布、资源生命周期、
撤权/升级后的派发和恢复拒绝。既有 Core Runner、Gateway、审批、唯一操作账本和任务预算继续负责执行。
本轮是库的扩展机制，不实施宿主插件设置页，不修改宿主生产配置或切换固定 wheel。

第一版：宿主显式给清单、工厂对象和绑定；基础包继续标准库与 Python 3.10+，MCP 沿用已有可选 extra。
工厂是受信任宿主代码，接口不提供任意 Python 沙箱。清单不执行代码，不自动扫描/import/安装插件，
不增加插件市场、包依赖求解器、Core reducer hooks、第二个循环/调度器/账本。

实施中保留 `0.3.0a1`。`0.4.0a1` 仅为复核后的候选版本，升版、main 合并、正式发行、宿主采用另行处理。
本任务不调用付费模型、真实凭据、真实业务资料，不上传包索引或推 tag。

## 实施顺序与退出条件

1. **E0：冻结合同。** 读适用 AGENTS/RTK，有 `.codegraph/` 时先用 CodeGraph。
   记录当前版本、源码/安装来源和测试环境；补清单/绑定/兼容/limits/纯预检合同与 ADR。
   EXT-AC01–08 通过后再接资源生命周期。
2. **E1：原子装配。** 私有暂存 contributions，完成 tools/skills/MCP 的最终冲突和依赖校验后一次发布。
   首个工厂成功、后一个失败时清理全部资源，宿主 registry 保持未变。实现组合 SkillProvider 与 MCP 工厂辅助，
   保留原 validator/preview/dispatch_guard/alias。完成 EXT-AC09–18。
3. **E2：实际失效与恢复。** 确定 owner task/loop、启动回滚和有界幂等关闭；扩展身份进入实际 config 比较，
   保留原模型/指令/宿主 policy 指纹；旧工具和 skills 引用也要失效。通过真实 SQLite Harness、审批、
   新进程恢复、部分批次及 ChildCoordinator 验证 EXT-AC19–31。
4. **E3：两个参考扩展与交付。** 标准库工具+skill 包、可选 MCP 包，两个真实示例、扩展合同测试、
   独立探针、base/extra 与 wheel/sdist 树外安装 gate。逐行填 EXT-AC32–40，更新文档和生成清单。

每阶段先运行有意义的行为 RED，再最小实现 GREEN，再做影响面回归，小提交并记录证据。
类型/文件名可按实际复杂度调整，ADR 解释等价实现；不能删验收条件、只改预期或缩支持面后宣布完成。

## 最容易交付成“接口存在但无效”的地方

- `fingerprints={"extensions": ...}` 目前不会自动被 `_compare_arrangement` 比较。
  `tool_fingerprint()` 也不覆盖全部执行语义。必须证明**相同名称/schema、不同扩展版本**在真实恢复时拒绝。
- 当前 `dispatch_guard` 的现有接线主要用于 Harness 恢复；`ToolGateway.invoke()` 没有通用 guard 调用。
  撤权后的实际 bridge 调用、直接 Gateway 调用都要测，不能只测返回 False 的比较函数。
- 启动失败的那个工厂也可能已获得资源；只回收之前“成功登记”的工厂会漏进程。
- 不能靠向现有 SkillProvider 逐个 register 达成原子组合：同键覆盖、global 回退和惰性内容变化都要约束。
- wrapper 必须同时检查扩展失效和 MCP 目录失效；不能为附加一个 guard 把旧 guard 覆盖掉。
- close 只负责资源，不代表远端业务回滚；准入前失效与准入后在途分开测试，UNKNOWN 保留原账本事实。
- 恢复必须覆盖 SUSPENDED、RUNNING orphan/partial batch、child_result，不只覆盖普通 resume。
- 新进程重新连接的对象地址/连接代次变化不应改变逻辑部署身份；相同配置恢复要有 GREEN。
- 没采用扩展的旧接口与旧指纹保持原行为；切成新扩展部署时不能伪造旧绑定基线。
- 整个装配过程不能授予权限、执行 skill 脚本、调用业务工具，或把工厂/凭据传给不受限子任务。

## 验证与最终交付

已有入口、建议新增入口和故障注入点均在完整方案 §8。
基础环境要实际执行纯扩展测试；MCP 环境要实际执行可选路径，零收集和全部 skip 都不是通过。
独立脚本若插入源码搜索路径，不能拿它冒充已安装发行物验证；记录 site-packages 来源。

Python 3.10 必须使用真实解释器验证。机器没有时 AC38 记 BLOCKED，E3 保留候选/部分验证状态，
静态解析和已有 3.10 锁文件不代替实跑。其他未跑平台、远程 CI、真实 Provider 如实记录。

最终提供：

- 分阶段提交、真实公开 API、格式与兼容差异；
- `docs/extensions-delivery-report.zh-CN.md`：逐 EXT-AC01–40 的实际节点/命令/退出码/结果/缺口；
- 两个可运行示例、独立探针、同次构建的安装门禁报告与剩余边界；
- 更新 README 中英文、contract/adapters/recovery/verification、扩展开发指南与 SNAPSHOT。

可直接粘贴给 DeepSeek：

> 请在独立 yanxu-harness 仓库按 docs/extensions-implementation-plan.zh-CN.md 实施 E0–E3，先核对 HEAD/工作树/适用规范与 CodeGraph。保持 0.3.0a1，逐阶段 RED→GREEN、小提交并更新 extensions-progress.zh-CN.md；不上传、不推 tag、不合 main、不切换宿主 wheel、不使用真实资料或付费模型。交付宿主显式清单/工厂/绑定、tools/skills/MCP 原子装配、可撤销且有界的生命周期、真实派发/恢复指纹接线及两个参考包。沿用唯一 Core/Gateway/账本，保留手工 API、scope 与 child 预算，不把清单当授权。特别覆盖直接 Gateway 撤权、部分启动资源回滚、相同 schema 的扩展版本漂移、相同部署新进程恢复、MCP 旧守卫组合及真实 A/B/C 故障窗口。逐 EXT-AC01–40 提供可复核证据；Python 3.10 没有解释器就明确阻塞，不缩验收条件或用 skip 冒充通过。
