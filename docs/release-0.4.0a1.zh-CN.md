# 0.4.0a1 分支候选记录

日期：2026-10-21（Asia/Shanghai）。分支：`codex/extensions-implementation`。
按用户要求，将 `pyproject.toml` 与 `yanxu_harness.__version__` 同步为 **`0.4.0a1`**。
功能基线为 `ccd5543c9aa258df40a23f5f79f1facefcdf7964`，加本轮版本、文档及生成清单更新。

## 交付范围

- E0–E3 扩展清单、显式工厂/绑定、tools/skills/MCP 原子装配与两个参考包。
- 部署撤权、作用域检查、生命周期清理与真实恢复指纹接线；EXT-R1–R7 已独立复验关闭。
- 原手工 API、Run/checkpoint 等持久化格式版本保持不变。
- README、公开合同、开发指南、兼容断言、变更日志和 SNAPSHOT 同步到当前候选版本。

原实现与独立复核使用的版本是 `0.3.0a1`，证据和产物摘要保留原版本：
[独立复核 §12](extensions-independent-review.zh-CN.md)、[进度](extensions-progress.zh-CN.md)、
[旧发行与宿主采用记录](release-0.3.0a1.zh-CN.md)。以下单独记录 `0.4.0a1` 的验证。

## 0.4.0a1 验证

环境：macOS arm64 / Python 3.12.13。以下为升版后实际运行结果。

| 检查 | 实际结果 |
| --- | --- |
| `pyproject.toml` / 运行时 `__version__` / SNAPSHOT | 均为 **0.4.0a1** |
| 公开 API、构建脚本、快照白名单、扩展兼容断言 | **30 passed**，exit 0 |
| `.venv/bin/python -m pytest -q` | **1445 passed / 21 skipped**，97.08s，exit 0 |
| wheel / base 树外安装 | **122 passed / 8 skipped**；example 13 完成，14 明确报告缺 extra |
| wheel / MCP extra 树外安装 | **129 扩展 + 93 MCP passed / 0 skipped**；example 14 完成，远端 bump=1；原验收探针通过 |
| sdist / base 树外安装 | **122 passed / 8 skipped**；example 13 完成，14 明确报告缺 extra |
| wheel METADATA / sdist PKG-INFO | `Version: 0.4.0a1` |
| 三个安装环境的模块版本 / `importlib.metadata.version()` | 均为 **0.4.0a1**，从各自 `site-packages` 导入 |
| 本轮更新文档的本地链接检查 | **258 个有效** |

本轮标准安装 gate：

```sh
.venv/bin/python scripts/extensions_gate.py --workdir build/extensions-0.4.0a1 --report build/extensions-0.4.0a1/report.json
```

实际结果 **`ok: true`，exit 0**。产物：

- `build/extensions-0.4.0a1/dist/wheel/yanxu_harness-0.4.0a1-py3-none-any.whl`
- `build/extensions-0.4.0a1/dist/sdist/yanxu_harness-0.4.0a1.tar.gz`

这些是本地验证候选，构建于本节最终结果写入之前。旧 `0.3.0a1` 的四组合安装结果仍归属旧版本；
本节没有将该证据改记为 `0.4.0a1` 的 sdist / MCP extra 验证。

## 当前边界

本分支为本地 Alpha 候选；本次范围是版本更新和本地验证。
AC38 初次因缺少 Python 3.10 解释器记为 **BLOCKED**；发布准备中的实际补验和修复结果见下节。
包版本升级不将历史测试重新记为新版本通过，也不代表已发布包索引或已切换宿主。

## 发布启动（2026-10-21）

用户已授权开启 `0.4.0a1` 发布。按仓库[发布流程](pypi-publishing.zh-CN.md)执行：

1. 提交并推送本发布分支，通过 PR 合入 `main`。
2. 等待 `harness` 的 Linux Python 3.10/3.11/3.12、macOS/Windows Python 3.12 全部检查。
3. 在同一 `main` 上执行 `publish.yml` 的只验证流程，核对 wheel/sdist、SHA256 和严格 README 检查。
4. 从通过验证的 main 提交创建 `v0.4.0a1` pre-release，触发 OIDC Trusted Publishing 到 PyPI。
5. 核对 PyPI 文件摘要，在新虚拟环境安装准确版本并运行 smoke 验证，再更新发布结果。

README 的安装版本和持久文档链接已准备为 `0.4.0a1` / `v0.4.0a1`。
当前尚在发布前验证阶段；实际 CI、tag、Release、PyPI 结果随后追加，原 BLOCKED 状态在真实证据到达前保留。

### Python 3.10 实跑与生命周期修复

已在 `build/` 内安装独立 CPython **3.10.22**，以 `build/venv-release-310` 安装当前候选与 MCP/dev extras。
没有用静态解析或跳过用例替代最低版本实跑。

首次运行 `pytest -q tests/extensions -x`：**1 failed / 118 passed / 1 skipped**。
真实 stdio MCP 往返成功，但 `CloseReport` 记录 RuntimeError。原因是 Python 3.10 的
`asyncio.wait_for(coroutine)` 另建任务，工厂进入与退出 anyio task group 的任务不同。
新增正常关闭/启动回滚两个 owner 回归，在修复前均实际失败（**2 failed**）。

修复：startup 和 reverse close 共用保持当前 owner task 的协作式超时封装。
Python 3.11+ 使用原生 timeout scope；3.10 使用当前任务的定时取消。
普通外部取消仍传播 `CancelledError`，超时仍进入原回滚或 unconfirmed 报告路径。

修复后实际运行：

- Python 3.10.22：`pytest -q tests/extensions` → **131 passed / 1 skipped**。
- Python 3.10.22：R7 独立探针 **5/5**，R1–R6 extra 探针 **7/7**，均 exit 0。
- Python 3.12.13：`pytest -q tests/extensions tests/mcp` → **224 passed / 1 skipped**。
- 首次并发运行的 3.10 扩展 + MCP 合集为 223 passed / 1 skipped / 1 failed；失败为
  旧示例测试扫描全机进程，撞上另一个正在运行的验证任务。该 PID 随后已退出；MCP 套件将串行复跑确认，原失败记录保留。

**AC38 的 Python 3.10 基础合同、生命周期/恢复关键用例已实跑通过。**
完整跨平台发布门禁仍以 PR/main CI 的最终结果为准；尚未创建发布 tag 或上传。

### Windows 发布门禁修复

`1033cd4` 的 [Windows PR job](https://github.com/SZWzz/yanxu-harness/actions/runs/39516891828/job/118951412338)
发现：跨进程恢复测试按 `/src/` 字面检查 import 路径，Windows 返回反斜杠路径而误判；
另有 example 14 虽输出 completed，却在退出时返回非零，原验证器遗漏了 stderr。

修复了原生 `Path.parts` 的来源判断。示例用 `ExitStack` 注册两个阶段的全部 SQLite 存储，
在退出前释放所有句柄，同时检查 MCP 的 close report；验证器在示例失败时额外输出 stderr。
新增真实 MCP/SQLite 清理回归在修复前 **1 failed**：六个存储仅显式关闭两个，四个未关闭。
修复后定向回归为 **32 passed**；Windows CI 将重新确认。本机 Python 3.10 示例已完成，bump=1、exit 0；Python 3.12 优化模式示例也 exit 0。

Python 3.10 MCP 套件已串行复跑为 **93 passed / 0 skipped**，此前并发进程扫描失败关闭。
3.11 的旧 CI 也确认四个 MCP close 用例失败；owner-task 修复后的 Linux 3.10/3.11 job 已通过。

### 主分支发布复验：并发测试时序修复

PR #3 的矩阵与分支矩阵均完整通过后，合并到 `main@fe46fc0`。
该提交的发布 dry-run [39520754161](https://github.com/SZWzz/yanxu-harness/actions/runs/39520754161)
通过；主分支 [Windows job](https://github.com/SZWzz/yanxu-harness/actions/runs/39520742870/job/118963692798)
源码为 **1446 passed / 23 skipped**，wheel 安装套件为 **1520 passed / 30 skipped**，
sdist 安装套件出现 **2 failed / 1518 passed / 30 skipped**，因此未创建 tag 或上传。

两项失败已在本机用受控 250 ms 延迟确定复现（**2 failed**）：

- 孤儿运行测试固定 sleep 100 ms 后直接读取 `_active["r1"]`，未确认首次提交和 driver 已就绪。
- worker 竞争测试假设两者必然同时选中任务；若一个已完成，另一个正确返回 None，也被误判。

修复后，恢复及相邻 live-lease 测试等待真实 model-result 进入信号；竞争测试在真实 SQLite
claim 前用屏障确保两个 worker 都到达竞争边界。仍断言一方 held_elsewhere、一方 completed，
且 attempt_count=1、初始输入已应用，不放宽一次执行合同。两项各增加零延迟/250 ms 延迟组合。

本地 Python 3.12 与 Python 3.10.22 定向两套件各 **26 passed**；额外受控延迟探针 **4 passed**。
最终发布须等待新 main 提交的完整矩阵和 dry-run；`fe46fc0` 的产物仅保留为历史验证。
