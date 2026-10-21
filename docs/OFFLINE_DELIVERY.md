# 离线交付与运行手册（2026-10-20）

本手册描述当前源码、隔离 CUDA 环境、本机自包含 MARVEL GPU 评估包、旧 fresh
no-capture 评估包和源码恢复快照的可重复操作。它是 **local experimental/evaluation-only**
交付，不是发布，`production_ready=false`、`strength_claim=false`、
`REDISTRIBUTION_NOT_AUTHORIZED`。

Phase 7 为 **PAUSED / NOT STARTED / NOT AUTHORIZED**。本手册不包含登录游戏、
真实采集、证书安装、代理修改、自动点击/出牌、流量重放或训练。

## 1. 先确认交付状态

| 状态 | 值 |
|---|---|
| DOCS_READY | 见 [PROJECT_STATUS.md](PROJECT_STATUS.md) |
| GIT_HANDOFF_READY | 2026-10-20 最终本地 commit（仅本地，未 push） |
| SOURCE_GPU_OFFLINE_READY | `build/gpu-env-20261020` + 固定离线输入 |
| FROZEN_MARVEL_GPU_LOCAL_READY | 本轮最终 commit 构建的本机自包含 GPU onedir（待独立核验） |
| FROZEN_OFFLINE_EVALUATION_READY | 旧 fresh no-capture onedir（历史，已被 GPU 包取代） |
| CAPTURE_DISABLED_IN_EVALUATION_PACKAGE | 本轮 GPU 包与旧评估包均无 capture helper |
| PHASE7_PAUSED_NOT_AUTHORIZED | 未执行 |
| REDISTRIBUTION_NOT_AUTHORIZED | 本地评估专用 |

## 2. 环境

- 已验收 CPU 环境：Python 3.12 x64，`torch 2.13.0+cpu`，`numpy 2.3.5`，
  `PySide6 6.11.1`，`PyInstaller 6.21.0`。
- 隔离 GPU 环境：`build/gpu-env-20261020`，官方 `torch 2.13.0+cu126`。
- 不修改全局 Python、显卡驱动、系统 CUDA Toolkit 或全局 `PATH`。

确认解释器：

```powershell
py -3.12 -B -c "import sys,struct; print(sys.version); print(struct.calcsize('P')*8)"
D:\project\guance\build\gpu-env-20261020\Scripts\python.exe -B -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"
```

## 3. 源码启动（GPU 环境，默认 auto）

在子进程或当前终端显式设置变量；不会改写持久偏好：

```powershell
$env:GUANCE_DATA_DIR = "$env:TEMP\guance-offline-eval"
$env:GUANCE_MODEL_DEVICE = 'auto'
$env:GUANCE_MARVEL_V2_SOURCE = 'D:\project\guance\build\acceptance\guance-model-platform-20261016\phase2\runtime\marvel-e5c82e7c-d7acbc8d\source'
$env:GUANCE_MARVEL_V2_CHECKPOINT = 'D:\project\guandan\runs\comparison_delivery\dataset\m09_candidate_iter002000.pt'
$env:PYTHONPATH = 'D:\project\guance\src'
D:\project\guance\build\gpu-env-20261020\Scripts\python.exe -m guance
```

实际设备与原因在设置页模型卡片显示：`当前使用 GPU 推理（cuda:0）` 或
`GPU 不可用，当前使用 CPU 推理：原因…`。设备回退不计入模型 fallback。

强制 CPU（同一 cu126 解释器）：`$env:GUANCE_MODEL_DEVICE = 'cpu'`。
强制 CUDA（无设备时明确失败）：`$env:GUANCE_MODEL_DEVICE = 'cuda'`。

## 4. 源码离线检查（推荐入口）

`scripts/run_local_inference_check.py` 显式接收可信解释器，只给子进程设置隔离
`GUANCE_DATA_DIR`，不改全局 `PATH`，也不允许 bundle 指定可执行程序。每个模式都
走真实生产 client/worker 与固定离线 confirmed decision。

下文用 `$EV` 表示本任务证据根
`D:\project\guance\build\acceptance\guance-offline-delivery-20261020`，
`$GPU_PY` 表示 `D:\project\guance\build\gpu-env-20261020\Scripts\python.exe`，
`$CPU_PY` 表示已验收的 Python 3.12 解释器（绝对路径）。

```powershell
$EV = 'D:\project\guance\build\acceptance\guance-offline-delivery-20261020'
$GPU_PY = 'D:\project\guance\build\gpu-env-20261020\Scripts\python.exe'
$CPU_PY = 'C:\Users\<user>\AppData\Local\Programs\Python\Python312\python.exe'

# auto -> cuda:0 + 合法 MARVEL 推荐
& $GPU_PY -B scripts\run_local_inference_check.py --python $GPU_PY `
  --mode gpu-auto --output "$EV\source-check\gpu-auto.json"

# cu126 强制 CPU
& $GPU_PY -B scripts\run_local_inference_check.py --python $GPU_PY `
  --mode forced-cpu --output "$EV\source-check\forced-cpu.json"

# 进程隐藏 CUDA 设备（诊断；当前分类为 MODEL_INFERENCE_FAILED，见已知限制）
& $GPU_PY -B scripts\run_local_inference_check.py --python $GPU_PY `
  --mode no-cuda-hidden --output "$EV\source-check\no-cuda-hidden.json"

# CPU-only Torch：auto->cpu + 强制 cuda 明确失败
& $CPU_PY -B scripts\run_local_inference_check.py --python $CPU_PY `
  --mode cpu-only-fail-closed --output "$EV\source-check\cpu-only-fail-closed.json"

# CPU-only Torch：auto->cpu + 合法推荐
& $CPU_PY -B scripts\run_local_inference_check.py --python $CPU_PY `
  --mode cpu-only-auto --output "$EV\source-check\cpu-only-auto.json"
```

返回码 0 表示该模式符合预期；2 表示失败。JSON 记录解释器、torch 构建、实际设备、
原因码、推荐状态和隔离数据目录；runner artifact 写到同目录的 `*-artifact.json`。

## 5. 构建 fresh no-capture 评估包

输出根必须在本任务新目录；`dist` 与 `work` 不重叠，且不清理旧目录：

```powershell
py -3.12 -B scripts\build_release.py --exclude-capture-runtime `
  --dist-dir D:\project\guance\build\acceptance\guance-offline-delivery-20261020\package\dist `
  --work-dir D:\project\guance\build\acceptance\guance-offline-delivery-20261020\package\work
```

`--exclude-capture-runtime` 设置独立 PyInstaller work 目录与显式构建环境，
不要求 `runtime/mitmproxy`，并在构建后验证包内不存在 capture runtime。
不要对旧 `release/` 或 `build/pyinstaller` 使用 `--clean`。

## 6. 验证评估包

```powershell
$DIST = 'D:\project\guance\build\acceptance\guance-offline-delivery-20261020\package\dist\Guance'
py -3.12 -B scripts\run_offline_delivery_acceptance.py audit      --package $DIST
py -3.12 -B scripts\run_offline_delivery_acceptance.py smoke      --package $DIST
py -3.12 -B scripts\run_offline_delivery_acceptance.py worker     --package $DIST
py -3.12 -B scripts\run_offline_delivery_acceptance.py capability --package $DIST
py -3.12 -B scripts\run_offline_delivery_acceptance.py finalize   --package $DIST
```

- `audit`：逐文件清单、静态包审计、`Guance.exe --version`、`python312.dll` 闭包、
  明确禁止 capture/Torch/checkpoint。
- `smoke`：真实无网络 `--frozen-smoke`，隔离 `GUANCE_DATA_DIR`，验证可见窗口、
  正常退出、未创建 controller、未操作代理/证书。
- `worker`：frozen FableDan `load/recommend/close/stop`；MARVEL descriptor 明确
  fail-closed；缺失模型明确失败。
- `capability`：生成能力矩阵与 package capability manifest。
- `finalize`：构建身份、逐文件 `SHA256SUMS.txt`、完整 onedir ZIP 及 SHA-256。

GUI 打开或截图成功不等于真实 live/model recommendation display 通过。

## 7. 解压运行与退出

### 7.1 本轮 MARVEL GPU 包（2026-10-20 最终本地交付）

1. 解压最终 GPU ZIP，保留完整 `Guance/` 目录（必须保留 `_internal`）。
2. 首次运行建议使用隔离数据目录，避免触碰真实用户配置：
   ```powershell
   $env:GUANCE_DATA_DIR = "$env:TEMP\guance-gpu-eval-data"
   .\Guance.exe
   ```
3. 在“设置”中导入 GPU 模型 descriptor：
   `_internal\marvel_runtime\bundles\marvel-v2-iter002000-device`。
   默认为 `auto`：本机有可用 CUDA 时显示 GPU 推理（`cuda:0`）；无可用 CUDA 时同一
   权重回退 CPU，并显示原因。强制 `cuda` 不可用时明确失败，不会回退 CPU 或换模型。
4. 包内已包含 Torch/CUDA、冻结 MARVEL source/native、真实 iter2000 checkpoint；
   运行时不读取 `D:\project\guance`、`D:\project\guandan`、开发 venv 或全局 `PYTHONPATH`。
5. 关闭窗口即退出；模型 worker 由 client 生命周期回收。需要回到 CPU 时把设备设为
   `cpu` 或 `auto`（无设备时即 CPU）。
6. capture 在本包中不可用；不会发现系统 mitmproxy，也不会修改代理或证书。

### 7.2 旧 no-capture 评估包（历史）

1. 解压 `Guance-offline-no-capture-onedir.zip`，保留完整 `Guance/` 目录。
2. 运行 `Guance.exe`；首次启动只查看授权说明，不要开始真实采集。
3. 该包中 capture 状态为 unavailable，MARVEL 明确 fail-closed；不会回退到系统 mitmproxy。
4. 关闭窗口即退出；模型 worker 由 client 生命周期回收。
5. 需要完全隔离时设置 `GUANCE_DATA_DIR` 到临时目录后再启动。

`--version` 只验证 bootloader/最小导入；`--exit-after` 只安排退出，
不能单独证明无副作用。

## 8. 回到原 CPU 环境

1. 取消 `GUANCE_MODEL_DEVICE`（或设为 `auto`）。
2. 用原解释器启动：`py -3.12 -m guance`。
3. 旧 `marvel_v2.cpu.v1` bundle 保持原始 CPU-only 语义。
4. 隔离 GPU 环境只位于 `build/gpu-env-20261020`，可单独删除；不要删除
   checkpoint、capture、用户数据或已验收 CPU Python。
5. 评估包与源码快照可单独删除；不要覆盖旧 `release/` 与历史证据。

## 9. 源码恢复快照

```powershell
py -3.12 -B scripts\make_source_snapshot.py --root D:\project\guance `
  --output <evidence>\source\guance-source-recovery.zip `
  --verify-python "C:\...\python.exe" --verify-report <evidence>\source\extraction-check.json
```

快照包含 tracked 文件、显式 allowlist 的 untracked 源码/测试/公开文档及
`tracked-uncommitted.patch`；排除 `.git`、虚拟环境、build/release、模型权重、
证书、日志、抓包、私密证据和 agent 配置。解压后在系统临时目录验证关键模块可导入。

## 10. 明确未完成

- 跨机可移植性、第二台无 GPU 机器、不同驱动/OS 组合：UNVERIFIED。本轮只验证本机
  （RTX 3050 Ti Laptop，驱动 576.80 / CUDA 12.9）。
- 实时采集与真实 live UI：本轮 CAPTURE_DISABLED_IN_EVALUATION_PACKAGE，
  live UI UNVERIFIED；Phase 7 未授权。
- 模型强度、冠军、正式评估：`strength_claim=false`，FableDan 评分与 MARVEL
  path score 都不是胜率。
- Git 远端备份：本轮只做本地 commit，未 push；remote freshness UNVERIFIED。
- 第三方/Qt/CRT/Torch/CUDA/权重的再分发许可：NOT_AUTHORIZED，需单独法律确认。
- 任何发布、上传、销售或再分发：未授权。
