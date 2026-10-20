# 公开源码整理与敏感内容检查

日期：2026-10-20（Asia/Shanghai）。包版本：`0.3.0a1`。
远端检查基线：`origin/main` 的 `9aed198872916c9b3d1496a4647ed24875364ef4`。
本记录描述公开后的整理与检查，不改变已有发行产物及其 SHA256，也不代表完整安全审计。

## 1. 公开文档与入口

- README 中英文以源码安装和无需 API Key 的恢复演示开场，补齐持久 inbox、本地调度、父子任务及 MCP 能力和示例。
- [文档索引](README.md)区分当前接入契约、发行证据与历史方案；保留原始失败复现和修复记录。
- [支持范围](../SUPPORT.md)区分当前版本实测、旧版本历史实测、静态检查和未验证平台。
- [安全政策](../SECURITY.md)与贡献指南使用真实仓库地址；包元数据增加仓库、文档、问题与安全政策链接。
- GitHub **Private vulnerability reporting** 已在仓库设置启用，并在 Security 概览核对为 Enabled。
  私密入口：[Report a vulnerability](https://github.com/SZWzz/yanxu-harness/security/advisories/new)。
  Issue 入口也提供该链接，避免把漏洞报告引向公开讨论。
- 移除已跟踪的空文件 `src/yanxu_harness/errors.py.new`。它没有内容，不承担实现或兼容职责。

## 2. Git 历史与当前文件

已先执行 `git fetch origin --prune`，并用 `git ls-remote --heads --tags origin` 核对远端引用。
当时远端有 `main` 与 `codex/release-0.2.0a1` 两个分支、无 tag。本地全部可达引用共
**137 个提交、1,122 个唯一 blob（26,875,055 bytes）**，包括未推送的本地分支。

| 检查 | 方法与结果 |
| --- | --- |
| 历史补丁中的疑似密钥 | Gitleaks **8.30.1**，默认规则，`--all --full-history`，输出脱敏；扫描 137 个提交，1 条命中，人工核对为测试哨兵 |
| 提交消息、作者和提交者元数据中的疑似密钥 | Gitleaks `stdin` 扫描，0 条命中 |
| 所有可达历史 blob 与文件名 | 补充检查私钥头、URL 内嵌账号密码、凭据文件名和本机绝对路径；未发现私钥、URL 凭据、凭据文件或个人 home 路径。卷路径仅命中导出脚本自身的防泄漏规则定义 |
| 整理后的当前文件 | 复制 Git 跟踪文件及未忽略的新文件（共 313 个）后用 Gitleaks `dir` 扫描；同一测试哨兵 1 条命中，没有新增疑似密钥 |

唯一 Gitleaks 命中是提交 `3626c1fc21e96900eff97daddb554542510fe547` 中
`tests/mcp/test_http.py:33` 的 `SECRET` 常量。该字符串由测试明确设为 loopback HTTP
夹具的预期 token，用来断言公开描述、错误、facts 和 stderr 不泄漏凭据；它不是从用户环境
加载的真实服务凭据。不删除该测试、不改写历史、不把扫描器规则整体关闭。

可复现的历史命令（在源码根目录运行；原始报告留在忽略的 `build/` 中）：

```bash
mkdir -p build/public-audit
gitleaks git --redact --no-banner --log-opts='--all --full-history' \
  --report-format json --report-path build/public-audit/gitleaks-history.json .
```

上述命令会因这条人工核对的测试哨兵返回 **1**；不能把该退出码记录为零命中或执行失败。
Git 历史扫描和模式检查不能证明不存在所有形式的敏感数据，也不覆盖他人 clone/fork、
不可达/已删除的远端引用或 GitHub 后台缓存。真实凭据一旦发现，应先撤销/轮换，再评估清理范围。

## 3. GitHub Actions 历史

分页能力为 `per_page=100`；API 返回全部 **6 次运行**，每次仅 1 次 attempt，共 **42 个 job**。
逐 job 读取步骤和日志，并逐 run 检查 artifacts：

| Run | 结局 | Job 数 | 已列出的执行步骤 | 日志结果 | Artifact 数 |
| --- | --- | --- | --- | --- | --- |
| [39340985433](https://github.com/SZWzz/yanxu-harness/actions/runs/39340985433) | failure | 7 | 0 | 7 个均为 404 / BlobNotFound | 0 |
| [39340473140](https://github.com/SZWzz/yanxu-harness/actions/runs/39340473140) | failure | 7 | 0 | 7 个均为 404 / BlobNotFound | 0 |
| [39223179637](https://github.com/SZWzz/yanxu-harness/actions/runs/39223179637) | failure | 7 | 0 | 7 个均为 404 / BlobNotFound | 0 |
| [38932316579](https://github.com/SZWzz/yanxu-harness/actions/runs/38932316579) | failure | 7 | 0 | 7 个均为 404 / BlobNotFound | 0 |
| [38932316339](https://github.com/SZWzz/yanxu-harness/actions/runs/38932316339) | failure | 7 | 0 | 7 个均为 404 / BlobNotFound | 0 |
| [38704090465](https://github.com/SZWzz/yanxu-harness/actions/runs/38704090465) | failure | 7 | 0 | 7 个均为 404 / BlobNotFound | 0 |

**没有取得可供内容扫描的历史日志字节。** 这说明本轮检查时日志不可用，不能表述为“42 份日志已扫描且安全”，
也不能仅凭 404 推断每条日志从未产生或泄漏。没有发现需要删除的可下载日志或 artifact；未删除运行记录。
这些执行前失败也不构成 Linux/Windows 或 Python 矩阵的验证证据。

最新历史运行 `39340985433` 的网页 Annotations 也逐项说明 7 个 job 被 GitHub 账户运行限制拦截，
未启动。此次整理不更改账户账单或支出设置；新的公开仓库运行结果需另行核对。

## 4. 验证记录

第一轮基础套件：**1305 passed / 13 skipped**（Python 3.12.13 / macOS arm64）。
第一轮 `ci_check.py --offline` 的安装、基础测试、冷导入、文档链接与来源许可通过；发行步骤失败，
原因是缓存准备只枚举固定的 `dev/yaml/pydantic`，而发行安装会安装产物声明的所有 extras，遗漏了 MCP。

本轮修正 `scripts/verify_release.py::declared_requirements`，从 `pyproject.toml` 读取所有 extras
供准备命令和离线缺项检查使用，避免每新增适配器再维护一份固定列表。回归测试
`test_preparation_covers_optional_extras_the_artifact_installer_will_use` 在修复前因缺少 MCP 失败，
修复后通过，并覆盖未来新增的 extra。构建脚本、公开 API、清单、导入隔离和静态 Python 下界的聚焦门禁
**37 passed**。

实际重跑准备命令时又复现相对目录问题：子进程在缓存目录内启动，同时把同一相对目录传给 pip，
导致下载落到嵌套目录、随后缺项检查仍失败。`ci_check.py::prepare_wheelhouse` 现在先解析为绝对路径，
再设置子进程 cwd 与目标；新增回归在修复前失败，并断言文件落到请求的缓存根目录且不创建同名嵌套目录。

第二轮完整门禁的源码套件 **1307 passed / 13 skipped**；树外 MCP 测试收集因共享辅助模块不可导入而失败。
原因是验证器删除了复制后的根 `conftest.py`。现保留该文件以设置测试辅助目录，包位置仍由复制后的
`package_paths` 从安装环境解析，并清空 pytest 子进程继承的 `PYTHONPATH`。新增独立子进程回归在
修复前复现导入失败，修复后通过；最终聚焦门禁 **39 passed**。运行时代码与持久化格式未改动。

最终重新执行：

| 命令或检查 | 结果 |
| --- | --- |
| `python scripts/ci_check.py --prepare-wheelhouse build-wheelhouse` | exit 0，41 个 wheel，MCP 及其依赖已包含 |
| `python scripts/ci_check.py --offline --wheelhouse build-wheelhouse --keep-artifacts` | exit 0，**6/6**：安装、源码测试、冷导入、wheel/sdist 树外测试与十二个示例、文档链接、来源许可全部通过 |
| 最终源码套件（由上述完整门禁运行） | **1308 passed / 13 skipped**；未安装可选 SDK 的基础环境不作为 MCP 专项通过证明 |
| 当前文件 Gitleaks 复扫 | 同一测试哨兵 1 条命中，无新增疑似密钥 |

保留的失败与成功日志位于本机忽略的 `build/public-audit/`，未发布原始日志。wheel 与 sdist
均从新建环境中的安装产物运行测试、十二个离线示例、评测与只读诊断；本次不是远程 CI 结果。

已有 `0.3.0a1` 发行产物的验证数字和摘要保持原样，见[发行记录](release-0.3.0a1.zh-CN.md)。
本次构建仅验证新的源码与元数据，不替换宿主已经固定的发行 wheel；不重新声明真实模型质量或未运行平台已经通过。

## 5. 公开后的首次远程 CI

提交 `615ecf2838aa5affa863c32ff09a3472d6e4ec27` 推送并创建 [PR #1](https://github.com/SZWzz/yanxu-harness/pull/1)
后，GitHub 托管 runner 已实际启动。[PR 运行 39358039181](https://github.com/SZWzz/yanxu-harness/actions/runs/39358039181)
的文档与许可 job 通过，五个环境的依赖准备均通过；Linux Python 3.10/3.11/3.12 的首次源码与树外测试
均在 `test_the_namespace_cap_counts_tasks_not_harness_objects` 失败，其余基础测试为 1307 passed / 13 skipped。
这些失败保留为首次执行证据，不改记为通过。

核对发现测试用固定 50 次 `asyncio.sleep(0)` 假定 SQLite 领取已完成，未实际等到 Harness 启动，
因此把空的 inflight 记录当作并发限制失败。本轮改为等待阻塞 Harness 的 `started` 事件（有界超时），
仍断言 tenant-a 正在运行、同 namespace 的第二任务不被选中、tenant-b 获得服务；`finally` 释放驱动。
只修改测试同步，不修改调度器或降低并发断言。跨平台状态仍以指定提交的实际运行结果为准。
