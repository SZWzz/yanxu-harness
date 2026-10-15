# yanxu-harness（工作名）

可嵌入的 Python Agent Harness：唯一模型循环、工具四层、Skills 三层、可选决策路由、
上下文预算，以及可持久恢复的审批。基础依赖闭包里没有任何应用框架。

> **状态：本地候选，未公开。** `0.1.0a1` 是本地版本串，不是已发布产物。源码在
> <https://github.com/SZWzz/yanxu-harness>；任何包索引上都没有发布，也没有
> 上传用的发布账户、tag、徽章或包地址。发行名 `yanxu-harness` 与 import 名
> `yanxu_harness` 尚未对索引做名称可用性核实，版本串由本包自己定义。

## 一屏看懂设计

```text
你的应用 ──► yanxu_harness ──► 中立端口（只用标准库）
                 │
                 ├── core/       唯一模型循环、中立消息、流式事件、取消
                 ├── runtime/    运行生命周期、预算、挂起/续跑、运行上下文
                 ├── tools/      定义与注册、部署可见、当轮发现、授权执行
                 ├── skills/     目录元数据、按需正文/资源、会话绑定
                 ├── decisions/  no-op / 规则 / 模型决策路由（全部可选）
                 ├── context/    原始 transcript 归档、模型投影、摘要预算
                 ├── storage/    持久化/工作单元端口、内存与 SQLite 参考实现
                 └── providers/  Provider 协议与显式配置的 transport
```

依赖方向固定：**应用 → harness → 中立端口 / 标准库 / 显式选择的 extra**。
harness 不导入任何应用包、Web 框架、应用存储、前端或 Node 工具链。
缺一个可选 extra 只影响请求它的那个适配器。

## 基础安装

基础包不需要任何第三方运行时依赖：

```bash
python -m venv .venv
. .venv/bin/activate
pip install yanxu_harness            # 有正式发布之后
# 或者从本地检出：
pip install .
```

可选 extra 按适配器分配：`[pydantic]` 参数模型、`[yaml]` 读取 `SKILL.md` 前言、
`[dev]` 测试与构建。

## 一条链路走完的可运行示例

`examples/05_durable_approved_edit.py` 演示端到端场景——任务 → 模型工具调用 → 真实预览 →
暂停 → 进程退出 → 新进程读状态 → 操作员批准 → 新进程恢复 → 只执行未完成的调用 →
模型拿到结果并作答。不带参数运行时，它会在临时目录里用真实子进程把整条链路走完：

```bash
python examples/05_durable_approved_edit.py
```

手动操作时，同样四步各自是独立命令（`--state DIR` 存放运行/操作/审批三本账）：

```bash
python examples/05_durable_approved_edit.py --state /tmp/edit start --task "更新欢迎语"
python examples/05_durable_approved_edit.py --state /tmp/edit status --run edit-1
python examples/05_durable_approved_edit.py --state /tmp/edit approve --approval <binding_key> --actor you
python examples/05_durable_approved_edit.py --state /tmp/edit resume --run edit-1
```

`approve` 是可信本地操作员入口，不是 Web 端点：本包不做用户认证，把审批暴露到 HTTP 的
宿主必须自己完成鉴权。示例中的模型是明确标注的确定性 `Provider` 替身；
`examples/optional/real_model_edit.py` 在宿主显式配置后可用真实 OpenAI 兼容端点跑同一条
链路，并按设计不进自动门禁。

## 本包承诺保持的行为

1. **只有一个循环。** 依赖闭包里只有一份模型循环；兼容 shim 重新导出同一批函数、
   类型和异常，`isinstance` 与 `except` 不会因为两份实现而分裂。
2. **写入由作者控制。** 生成内容保持暂存，直到应用显式授权。批准绑定 namespace、
   调用、完整参数 hash、目标版本、工具合同版本、策略版本和有效期——
   任一项变化都让批准失效。
3. **未知就是未知。** 无法证明的外部副作用一律报告 `unknown`，不升级成成功、
   不自动重试。
4. **技能是不可信指导。** 技能正文、allowed-tools 清单和随附资源不授予任何东西：
   不给权限、不给批准、不执行脚本。
5. **状态带版本。** 事件、checkpoint 和 schema 都有版本与指纹；读到未来版本或
   无法证明安全的旧状态时显式拒绝，不猜测兼容。
6. **原始历史保持原样。** 摘要不改写已归档 transcript；模型投影是另一份有预算的视图。

## 文档

* `docs/contract.md`：公开 API、稳定性级别与逐条行为保证，每条都指到实现它的模块。
* `docs/adapters.md`：六个端口各自要做什么，以及可以对自己实现跑的共享检查。
* `docs/recovery.md`：失败状态、四类崩溃窗口、租约 fencing，以及本包明确**不**承诺的边界。
* `docs/verification.md`：哪些窗口由哪个测试、用哪类证据验证，以及明确**未**验证的部分。

项目文档：

* `SUPPORT.md`：支持的 Python 版本、平台与依赖表（基础安装只用标准库，extras 按适配器拆开）。
* `CONTRIBUTING.md`：范围、环境搭建、必须跑的门，以及本包遵循的规矩。
* `SECURITY.md`：安全模型与报告方式（**尚未**公布报告渠道，不要自行编一个）。
* `CHANGELOG.md`：`0.1.0a1` 包含什么、刻意不做什么。

## 开发

```bash
python -m pytest -q tests                     # 本包自己的测试
python -m build                               # wheel + sdist
python scripts/ci_check.py --prepare-wheelhouse build-wheelhouse   # 需要网络
python scripts/ci_check.py --offline --wheelhouse build-wheelhouse # 门禁
```

本包测试不导入任何其他仓库；嵌入 harness 的宿主也不会导入这些测试辅助。

## 许可证

MIT。见 `LICENSE` 与 `THIRD_PARTY_NOTICES.md`。
