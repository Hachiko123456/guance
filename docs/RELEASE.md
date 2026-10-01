# 发布说明

## 交付内容

发布时传输整个目录：

```text
release/Guance/
├─ Guance.exe
└─ _internal/
   ├─ PySide6/                  # 使用到的 Qt/QML 子集
   ├─ guance/presentation/qml/ # 应用 QML
   ├─ runtime/                 # 采集 frozen runtime
   └─ data/.../best.npz        # 用户明确选择随包提供的模型
```

不能只发送 exe。缺少 `_internal` 会导致 Qt、QML、模型或 helper 加载失败。

## 发布前

```powershell
.\.venv\Scripts\python.exe -m compileall -q src scripts
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --distpath release --workpath build/pyinstaller Guance.spec
& .\release\Guance\Guance.exe --version
```

记录：源码 commit、发布 ZIP 的 SHA-256（或逐文件 SHA-256 清单）、Python/PyInstaller 版本、模型 SHA-256、构建日志和未验证项。目录没有单个天然的文件哈希，不能只写“目录 SHA-256”而不给计算方式。

本次是文档和仓库规则提交，不创建 Release、上传二进制或宣布实机验收完成。源码基线与本地工作区差异见 README；不能把包含未提交修改的本地 EXE 标为此文档 commit 的可复现发行物。

## 目标机限制

- Windows 10/11 x64；
- 不需要安装 Python、NumPy 或单独的采集 runtime，但必须保留完整 onedir；
- `%LOCALAPPDATA%\Guance` 中的用户配置、日志、导出、证书和代理状态不随发布目录安全迁移；
- 每台机器、每个用户都需要重新确认证书和代理授权；
- 自动化 smoke 不能替代 DPI、多屏、杀毒软件、防火墙、direct/upstream 和真实连接实机验证。

## 发布安全

发布包不应包含开发机证书、个人配置、抓包、日志、凭据或未授权模型权重。发布前检查 `.gitignore`、包清单和诊断导出；任何代理或 helper 残留都阻止交付。

## 回滚

保留上一个已验证的完整 onedir 目录和对应 commit。回滚时替换整个目录，不只替换 exe；用户目录中的配置和证书状态应由应用自己的恢复流程处理。
