# 发布到 PyPI

发布工作流是 `.github/workflows/publish.yml`。它复用现有发布验证器，
先构建 wheel 和 sdist，分别在源码目录外安装、运行测试和示例，再检查文档、许可证和
PyPI 元数据。发布任务只上传这次验证通过的两个分发文件。

手动运行工作流只验证并保存产物；发布 GitHub Release 才会上传到正式 PyPI。

## 首次配置

1. 注册并登录 [PyPI](https://pypi.org/account/register/)，验证邮箱并完成账号要求的双重认证。
2. 在 GitHub 仓库的 Settings → Environments 创建名为 `pypi` 的环境。
   如需发布前人工审核，可在该环境配置 required reviewers。
3. 在 PyPI 的 [Publishing 页面](https://pypi.org/manage/account/publishing/)
   添加 GitHub pending publisher，填写：

   | 字段 | 值 |
   | --- | --- |
   | PyPI Project Name | `yanxu-harness` |
   | Owner | `SZWzz` |
   | Repository name | `yanxu-harness` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

   Workflow name 填文件名，不是工作流显示名。首次成功上传时，PyPI 创建项目。
   pending publisher 不会提前占用包名；如果名称已被其他项目使用，需要先处理名称归属。
   如果项目已经属于你的账号，在该项目的 Publishing 设置中添加同样的 trusted publisher。

这里使用 OIDC Trusted Publishing，不需要在 GitHub Secrets 中保存 PyPI Token。
参见 [PyPI 首次创建项目说明](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
和 [发布配置说明](https://docs.pypi.org/trusted-publishers/using-a-publisher/)。

## 发布前验证

将待发布修改提交并合入 `main`，确认 `harness` 工作流的所有任务通过。
在 Actions → publish to PyPI → Run workflow 选择 `main`，先运行一次只验证的流程。
成功后可以下载 `pypi-distributions` 和 `pypi-verification`，查看产物、SHA256 和测试报告。

也可以在本地复现：

```bash
python scripts/ci_check.py --prepare-wheelhouse build-wheelhouse
python scripts/verify_release.py --offline --wheelhouse build-wheelhouse --out build/pypi-release
python -m pip install twine
python -m twine check --strict build/pypi-release/*.whl build/pypi-release/*.tar.gz
```

本地报告如果标记为 dirty，只表示当前未提交工作区的验证结果；正式发布应来自已提交的版本。

## 发布 0.3.0a1

1. 确认 `pyproject.toml` 和 `yanxu_harness.__version__` 都是 `0.3.0a1`。
2. 在 GitHub Releases 创建 tag 为 `v0.3.0a1` 的 Release，指向上述通过验证的 `main` 提交，
   将其标记为 pre-release，并填写更新说明。
3. 发布该 Release。`publish to PyPI` 自动运行；如 `pypi` 环境设置了审批，审核通过后上传。
4. 检查 [PyPI 项目页面](https://pypi.org/project/yanxu-harness/)，并在新虚拟环境验证：

   ```bash
   python -m pip install --index-url https://pypi.org/simple/ 'yanxu-harness==0.3.0a1'
   python -c "import yanxu_harness; print(yanxu_harness.__version__)"
   ```

5. 发布成功后更新 README 和贡献说明中“尚未发布到包索引”的文字，补充 PyPI 安装方式。

`0.3.0a1` 是 alpha 预发布，安装时可像上面一样明确指定版本，或使用 `--pre`。
后续发布先更新版本号和更新说明，再创建对应的 `v<version>` Release。
PyPI 不能用相同文件名覆盖已上传的分发文件；修复代码后需要发布新版本。

若上传失败，先检查 Actions 日志和 PyPI 已上传文件。修复认证配置后可重跑失败的任务；
如果只上传了一部分文件，不要直接重建后覆盖，应先核对已有文件及校验和，再处理剩余文件。
