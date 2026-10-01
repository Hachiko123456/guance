# Windows 构建指南

## 构建单元

发布物是完整的 `release/Guance/` onedir 目录，不是单独的 `Guance.exe`。目录依赖 `_internal/` 中的 Qt/QML、Python runtime、模型和采集 helper。

## 环境

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
```

确认 64 位：

```powershell
.\.venv\Scripts\python.exe -c "import sys, struct; assert sys.version_info[:2] == (3, 12); assert struct.calcsize('P') * 8 == 64"
```

## 本地资产

- `app.ico`；
- 用户提供的 `best.npz`；
- 本地准备好的采集 runtime：`runtime/mitmproxy/mitmdump.exe`、`addon.py` 和完整 `_internal/`；
- QML 源目录 `src/guance/presentation/qml/`。

运行时模型可通过 `GUANCE_MODEL_PATH` 指定；`Guance.spec` 的构建输入固定使用已有 profile 下的模型位置，二者不能混淆。不要为 `<profile>` 占位符新建错误目录。可在本地解析实际目标：

```powershell
$modelTarget = .\.venv\Scripts\python.exe -c "from pathlib import Path; paths=list(Path('data/profiles').glob('*/models')); assert len(paths)==1, 'Select the profile explicitly'; print((paths[0]/'best.npz').resolve())"
# 仅在确认要复制自己的模型时执行：
Copy-Item -LiteralPath 'D:\models\best.npz' -Destination $modelTarget
```

`app.ico` 也是本地构建输入，本次不额外提交该文件。模型、图标和 frozen runtime 缺少任意一项时，源码克隆后不能直接打出完整发行包。

## Runtime 准备

```powershell
.\.venv\Scripts\python.exe scripts/prepare_runtime.py
.\.venv\Scripts\python.exe scripts/prepare_runtime.py --source 'C:\path\to\runtime'
```

`--source` 必须指向含 `mitmdump.exe` 和相邻 `_internal/` 的完整目录。基线准备脚本会替换目标 runtime 的 `_internal/`，先关闭相关进程，保留原安装目录，不要传入存放个人文件的自定义目标。`--force` 和增量复制只存在于本地待合并新版，不应给基线传入该参数。

## 使用 Guance.spec 构建

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --distpath release --workpath build/pyinstaller Guance.spec
```

`Guance.spec` 是本次整理后应该跟踪的构建入口。它显式收集应用 QML、使用到的 Qt/QML 子集、模型、图标和完整 runtime，并排除不需要的 Qt 大模块。

本次文档提交不包含工作区中其他构建脚本改动。基线 `scripts/build_release.py` 仍直接调用旧的 PyInstaller CLI，并可能重新生成/覆盖 `Guance.spec`。使用维护规格时不要运行该旧脚本；在后续独立提交中再统一入口。

## 资源检查

构建后检查以下相对路径：`Guance.exe`、`_internal/app.ico`、`_internal/guance/presentation/qml/Main.qml`、`_internal/runtime/mitmproxy/mitmdump.exe`、`_internal/runtime/mitmproxy/addon.py`、完整 helper `_internal/`，以及 `_internal/data/profiles/<profile>/models/best.npz`。

比较打包前后模型哈希、QML 文件清单和完整 helper runtime。spec 显式保留 Qt Quick Controls Basic、Layouts、Window、Templates、platform 插件；不要以“能 import PySide6”代替 QML 插件加载验证。构建成功后也不要随意删 DLL 来减小体积。

## 验证

```powershell
.\.venv\Scripts\python.exe -m compileall -q src scripts
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --distpath release --workpath build/pyinstaller Guance.spec
& .\release\Guance\Guance.exe --version
```

`--version` 只验证 bootloader 和最小导入，不代表 GUI、证书、代理、真实采集或模型推荐通过。完整目录检查见 [RELEASE.md](RELEASE.md)。
