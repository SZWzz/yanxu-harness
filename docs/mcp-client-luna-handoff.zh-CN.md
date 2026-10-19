# Luna 交接说明：实现 MCP Client 适配

日期：2026-10-18。状态：**已制定计划，尚未实施**。

请按 [MCP Client 接入实施计划](mcp-client-implementation-plan.zh-CN.md) 在当前独立 `yanxu-harness` 仓库实施。计划编制基线是 `20731b1d0a5476588fc0a4662ec6d50a03b2b6db` / `0.2.0a1`；接手时重新核对工作树与提交，不覆盖其他改动。当前新增的是任务书，没有 MCP 运行时代码。

## 任务目标

新增可选 MCP Client 适配：Tools、stdio、Streamable HTTP；MCP 工具经过现有 ToolDefinition → core_tool → ToolGateway → 唯一操作账本，不增加第二个模型循环或工具账本。保持基础安装无第三方运行依赖和 Python 3.10+。

首版验证 `2025-11-25`、`2026-07-28` 两协议及两传输四格；SDK 首个候选为固定 `mcp==2.3.0`，先完成 MCP00 验证与依赖锁定。协议/SDK 的默认重试不能突破一次准入一次 tools/call；MCP 断线、超时或取消不能自动重放旧写操作。

Yanxu 已有宿主侧实测，复用它作为后续采用的验收基础，不再建设一套替代实测。此次实施只修改独立包与其测试、文档、构建验证；不切换宿主固定 wheel，不访问真实资料或付费 Provider。

## 开工顺序

1. 读适用 AGENTS/RTK；有 `.codegraph/` 时，理解/定位代码先用 CodeGraph。核对 git 状态、包版本和 0.2.0a1 发行报告。
2. 阅读主计划 §§2–3，完成 MCP00 的 SDK/协议/one-shot/资源上限验证，建立真实 MCP 服务夹具和独立 venv。
3. MCP01 先补中立执行结果和 operation context；普通返回值仍成功、远端错误不能误记成功，host-owned ledger 不能双记。
4. 按 MCP02–08 推进传输、目录/Schema、Gateway、恢复/诊断、真实故障、示例及安装 gate。每阶段 RED→GREEN、小提交、进度与命令记录。
5. 填 MCP-AC01–32，交付 `docs/mcp-client-delivery-report.zh-CN.md` 与独立探针；当前版本保留 `0.2.0a1`，等待独立复核后再处理升级和发行。

## 最容易做错的地方

- `ToolDefinition` 必须有真实参数验证器，写工具必须有宿主同步 preview；目录、annotations、instructions 不授予权限。
- 普通返回 `{"isError": true}` 会被现有后端当作成功，因此必须接通强类型中立结果和 receipt 归一。
- SDK v2.3.0 高层调用存在 HeaderMismatch 自动重试，必须用可验证的公开 one-shot 路径约束，不能只把 max retries 写成零便假定生效。
- 远端工具名保留原始大小写；本地稳定小写别名不能碰撞，服务端自报名称不能作为宿主身份。
- 冻结的工具定义/协议/授权范围参与原 arrangement；恢复不能拿新目录覆盖旧审批。
- 远端写成功而回包丢失要 unknown；重新连接、ping 或工具自称幂等不是业务证据。
- 取消是控制信号；停止本地等待不等于远端工作停止。不要吞掉取消后继续请求模型。
- JSON/输出/日志须有界；后置截断不能充当传输内存上限；完整结果与模型视图分开。
- MCP SDK 必须懒加载，遍历导入所有子模块也不能拉入 Web 依赖或起进程。
- 基础与 extra、wheel 与 sdist 分开验收；MCP suite 因缺依赖 skip 不能算通过。

## 交付口径

以主计划逐项退出条件和 AC 为准，记录真实测试节点、命令、退出码、业务效果、源码来源与未执行项。保留失败历史，不修改断言/跳过测试来换绿色。SDK 候选若有阻塞，先记录可复现 ADR 与影响，保持外部契约，不能静默删掉一格支持。

本轮不要求从头重跑所有已有 Yanxu 实测，不把离线脚本 Provider 叫真实模型；新增 MCP 验收使用真实子进程、真实 HTTP、SQLite 和故障同步。最终停在可审查的代码与报告状态，不上传发行、不推送 tag。
