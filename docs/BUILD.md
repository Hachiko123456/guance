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

## 使用 Guance.spec / build_release 构建

`Guance.spec` 是维护中的构建入口。受支持的安全构建路径是
`scripts/build_release.py`：它验证输出路径必须位于仓库内，自定义输出必须位于
`build/acceptance/` 且 `dist`/`work` 不重叠，并支持两种包身份：

```powershell
# 完整采集包（需要先准备 runtime/mitmproxy）
.\.venv\Scripts\python.exe -B scripts\build_release.py `
  --dist-dir build\acceptance\<task>\package\dist `
  --work-dir build\acceptance\<task>\package\work

# 评估用 no-capture 包（不要求 capture helper，使用独立 work 缓存）
.\.venv\Scripts\python.exe -B scripts\build_release.py --exclude-capture-runtime `
  --dist-dir build\acceptance\<task>\package\dist `
  --work-dir build\acceptance\<task>\package\work

# 本机 local-only MARVEL GPU 包（capture=false；显式注入已验收本地资产）
py -3.12 -B scripts\build_release.py --exclude-capture-runtime --profile marvel-gpu `
  --dist-dir build\acceptance\<task>\package\dist `
  --work-dir build\acceptance\<task>\package\work
```

`--profile marvel-gpu` 必须显式提供四个本地资产环境变量，构建脚本会先验证再复制：

| 变量 | 用途 |
|---|---|
| `GUANCE_MARVEL_SITE_PACKAGES` | 已验收 `torch 2.13.0+cu126` 所在 site-packages（只读复用，不重装） |
| `GUANCE_MARVEL_SOURCE` | Phase 2 冻结 MARVEL source/native 目录（只读，hash 不变） |
| `GUANCE_MARVEL_CHECKPOINT` | 真实 iter2000 checkpoint（sha256 `af0df1ea…`，8,588,181 bytes） |
| `GUANCE_MARVEL_BUNDLE` | 只含 `manifest.json`/`deployment.json`/`SHA256SUMS` 的 descriptor 目录 |

GPU profile 在 PyInstaller onedir 完成后，把 Torch/CUDA DLL、冻结 source、native 扩展、
checkpoint 和 descriptor 复制到 `_internal/marvel_runtime/`（`site-packages/`、`source/`、
`checkpoint/`、`bundles/`），并验证 descriptor 逐文件 source fingerprint、native
bytes/hash、checkpoint hash 后才成功。它不修改模型数学、identity 或既有严格资源校验；
默认/no-capture profile 仍拒绝任何 `_internal/marvel_runtime`。

不要对旧的 `release/` 或 `build/pyinstaller` 使用 `--clean`；需要清理时只删除本任务
新建根内、已解析绝对路径并确认无重解析点越界的目录。

也可以直接调用维护规格（此时必须自行确保输出目录安全）：

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --distpath release --workpath build/pyinstaller Guance.spec
```

`Guance.spec` 显式收集应用 QML、使用到的 Qt/QML 子集、FableDan 模型、图标和完整
runtime，并排除不需要的 Qt 大模块。`--exclude-capture-runtime` 通过
`GUANCE_EXPERIMENTAL_PACKAGE_NO_CAPTURE_RUNTIME=1` 同时约束 spec 和构建后验证。

## 资源检查

构建后检查以下相对路径：`Guance.exe`、`_internal/app.ico`、`_internal/guance/presentation/qml/Main.qml`、完整 Qt/QML 插件子集，以及 `_internal/data/profiles/<profile>/models/best.npz`。完整采集包还应包含 `_internal/runtime/mitmproxy/mitmdump.exe`、`addon.py` 和完整 helper `_internal/`；no-capture 评估包必须**不包含**它们，也不包含 `python314.dll`、Torch、`.pt/.pth/.ckpt` checkpoint。`marvel-gpu` profile 例外地包含 `_internal/marvel_runtime/`（Torch/CUDA、冻结 source/native、iter2000 checkpoint、descriptor），但仍必须**不包含** `_internal/runtime/mitmproxy`、`python314.dll`、抓包、证书或未列入 allowlist 的模型；构建脚本会拒绝 `torch/lib/*.lib` 静态库和任何额外 `_internal/marvel_runtime` 资产。

比较打包前后模型哈希、QML 文件清单和完整 helper runtime。spec 显式保留 Qt Quick Controls Basic、Layouts、Window、Templates、platform 插件；不要以“能 import PySide6”代替 QML 插件加载验证。构建成功后也不要随意删 DLL 来减小体积。

`scripts/run_offline_delivery_acceptance.py` 提供可重复的包验收：

```powershell
py -3.12 -B scripts\run_offline_delivery_acceptance.py audit      --package <dist>\Guance
py -3.12 -B scripts\run_offline_delivery_acceptance.py smoke      --package <dist>\Guance
py -3.12 -B scripts\run_offline_delivery_acceptance.py worker     --package <dist>\Guance
py -3.12 -B scripts\run_offline_delivery_acceptance.py capability --package <dist>\Guance
py -3.12 -B scripts\run_offline_delivery_acceptance.py finalize   --package <dist>\Guance
```

它检查 CPython 3.12 x64 闭包、`Guance.exe --version`、真实无网络 GUI smoke、frozen FableDan load/recommend/close/stop、MARVEL/capture fail-closed、逐文件 SHA-256 和 onedir ZIP 哈希。GUI 打开或截图成功不等于 live/model recommendation display 通过。

`marvel-gpu` 最终包使用独立验收器（只对最终 ZIP 解压目录运行，不碰构建树）：

```powershell
py -3.12 -B scripts\run_final_local_gpu_acceptance.py finalize --package <delivery>\Guance --zip <delivery>\Guance-final-gpu.zip --out <evidence> --git-commit <sha>
py -3.12 -B scripts\run_final_local_gpu_acceptance.py extract --zip <delivery>\Guance-final-gpu.zip --out <evidence>
py -3.12 -B scripts\run_final_local_gpu_acceptance.py audit --package <extracted>\Guance --out <evidence>
py -3.12 -B scripts\run_final_local_gpu_acceptance.py verify-manifest --package <extracted>\Guance --out <evidence>
py -3.12 -B scripts\run_final_local_gpu_acceptance.py smoke --package <extracted>\Guance --out <evidence>
py -3.12 -B scripts\run_final_local_gpu_acceptance.py gui --package <extracted>\Guance --out <evidence>
py -3.12 -B scripts\run_final_local_gpu_acceptance.py fabledan --package <extracted>\Guance --out <evidence>
py -3.12 -B scripts\run_final_local_gpu_acceptance.py marvel --package <extracted>\Guance --out <evidence>
py -3.12 -B scripts\run_final_local_gpu_acceptance.py fixed --package <extracted>\Guance --out <evidence>
py -3.12 -B scripts\run_final_local_gpu_acceptance.py negatives --package <extracted>\Guance --out <evidence>
py -3.12 -B scripts\run_final_local_gpu_acceptance.py cleanup --package <extracted>\Guance --out <evidence>
```

`fixed` 通过包内 `Guance.exe --model-worker` 复用固定离线输入，分别验证 `auto→cuda:0`、
强制 `cpu`、以及 `CUDA_VISIBLE_DEVICES=-1` 时 `auto→cpu`；`negatives` 验证缺失/篡改模型、
错误 wire 契约和切换后旧请求 fail-closed。

## 验证

```powershell
.\.venv\Scripts\python.exe -m compileall -q src scripts
.\.venv\Scripts\python.exe -m pytest -q
& .\release\Guance\Guance.exe --version
```

`--version` 只验证 bootloader 和最小导入，不代表 GUI、证书、代理、真实采集或模型推荐通过。完整目录检查见 [RELEASE.md](RELEASE.md) 与 [OFFLINE_DELIVERY.md](OFFLINE_DELIVERY.md)。
