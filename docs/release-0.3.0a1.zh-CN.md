# 0.3.0a1 本地发行与 Yanxu 采用记录

日期：2026-10-20（Asia/Shanghai）。本轮由作者授权完成修复提交、升版、正式产物验证、
合并 main 和 Yanxu 固定 wheel 验证。分发名为 `yanxu-harness`，导入名为 `yanxu_harness`。
这是本地 Alpha 发行；不上传包索引，不推送发行 tag，不调用付费模型。

## 来源与范围

R5 修复与独立复核记录提交：`a636523`。版本从 `0.2.0a1` 升为 `0.3.0a1`。
新增可选 MCP Tools 客户端（stdio、Streamable HTTP）、显式工具效果结果和可选派发守卫。
基础安装保持标准库依赖；公共 API 与包元数据同步升版，已有持久化格式版本保持独立。

历史审查、失败复现和修复前后测试证据保留原版本号，见
[独立复核](mcp-client-independent-review.zh-CN.md) 与 [交付报告](mcp-client-delivery-report.zh-CN.md)。

## 构建与门禁

状态：待运行。以干净的升版提交构建 wheel/sdist，产物和校验和写入
`build/release-0.3.0a1/`。正式发行取同一次构建的产物，安装门禁从该产物树外安装验证。
实际构建提交、SHA256 和各项结果在运行后补充，不沿用旧版本产物的通过结论。

## Yanxu 采用

状态：待运行。固定 `vendor/harness/` 的新 wheel，并同步宿主版本、digest、源码提交和
依赖声明。宿主已有工作区改动保留；验证采用临时测试数据，不改真实 vault。
本轮固定基础 wheel 并验证宿主现有合同和恢复行为；宿主开启 MCP 服务的产品配置另行实施。

## 验证边界

本机 Python 3.12 / macOS arm64。Python 3.10 实跑、远程 CI、其他平台和真实付费模型
仍没有本轮证据；历史 Yanxu 真实模型测试不改记成本版本的测试结果。
