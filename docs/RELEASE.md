# 发布说明（当前为离线评估交付，不是发布）

## 交付内容

发布单元始终是完整的 `Guance/` onedir 目录，不是单独 `Guance.exe`。
当前存在四类必须分开描述的交付：

| 交付 | 内容 | 状态 |
|---|---|---|
| 当前源码 + 隔离 CUDA 环境 | `src/` + `build/gpu-env-20261020`（官方 `torch 2.13.0+cu126`） | SOURCE_GPU_OFFLINE_READY |
| 旧 fresh no-capture onedir（2026-10-20 前一轮） | 当时源码构建，无 Torch/MARVEL/capture | FROZEN_OFFLINE_EVALUATION_READY（已被本轮 GPU 包取代） |
| 本轮本机自包含 MARVEL GPU onedir（2026-10-20） | 最终本地 commit 构建；包含 Torch/CUDA、冻结 source/native、iter2000 checkpoint、descriptor；capture=false | 实现完成，待独立核验；仅本机 local evaluation |
| 实时采集包 / 跨机 frozen GPU 发行包 | 未开发 | NOT_IMPLEMENTED / NOT_AUTHORIZED |

目录结构（本轮 GPU 包）：

```text
Guance/
├─ Guance.exe
├─ START_HERE.md
├─ NOTICE.md
├─ README.md
├─ capability-manifest.json
├─ SHA256SUMS
└─ _internal/
   ├─ PySide6/                  # 使用到的 Qt/QML 子集
   ├─ guance/presentation/qml/ # 应用 QML
   ├─ data/.../best.npz        # 仅本地评估授权的 FableDan 权重
   └─ marvel_runtime/
      ├─ site-packages/torch/  # torch 2.13.0+cu126 + CUDA DLL
      ├─ source/               # Phase 2 冻结 MARVEL source/native
      ├─ checkpoint/           # 真实 iter2000 checkpoint
      └─ bundles/              # 只读 descriptor
```

旧 no-capture 评估包**没有** `_internal/runtime/`、`mitmdump.exe`、`python314.dll`、
Torch、MARVEL runtime/source 或 checkpoint。本轮 GPU 包包含 `marvel_runtime`，但仍
**没有** capture helper、`python314.dll`、抓包、证书、日志或用户配置。缺少 `_internal`
会导致 Qt、QML 或模型加载失败。

## 发布前（本轮为本地验收，不创建 Release）

```powershell
.\.venv\Scripts\python.exe -m compileall -q src scripts
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -B scripts\build_release.py --exclude-capture-runtime `
  --dist-dir build\acceptance\<task>\package\dist `
  --work-dir build\acceptance\<task>\package\work
& .\build\acceptance\<task>\package\dist\Guance\Guance.exe --version
```

记录：源码 HEAD、branch、dirty 状态、tracked diff hash、untracked allowlist、
逐文件 SHA-256、onedir ZIP 的 SHA-256/bytes/时间、Python/PyInstaller/Qt/NumPy
精确版本、构建日志、smoke 报告、能力清单、局限和回滚步骤。
目录没有单个天然文件哈希，不能只写“目录 SHA-256”而不给计算方式。

**本轮不创建 GitHub Release、不上传源码/ZIP/二进制、不发布或销售。**
`REDISTRIBUTION_NOT_AUTHORIZED`；第三方与权重权利见
[第三方依赖与再分发](THIRD_PARTY_AND_REDISTRIBUTION.md)。

## 目标机限制

- Windows 10/11 x64；CPython 3.12 闭包；不需要单独安装 Python 或 NumPy。
- 评估包不提供真实采集；capture 状态为 unavailable，不会发现或启动系统 mitmproxy。
- MARVEL 需要外部 runtime/checkpoint 与 Torch；评估包内明确 unavailable/fail-closed。
- `%LOCALAPPDATA%\Guance`（或 `GUANCE_DATA_DIR`）中的用户配置、日志、导出、证书和
  代理状态不随发布目录迁移；每台机器、每个用户都需要重新确认证书和代理授权。
- 自动化 smoke 不能替代 DPI、多屏、杀毒软件、防火墙、direct/upstream 和真实连接
  实机验证；本轮也未证明跨机可移植性。

## 发布安全

包与源码快照不得包含开发机证书、个人配置、抓包、日志、凭据、代理租约、
隐藏对手手牌、模型训练数据或未授权权重。任何代理或 helper 残留都阻止交付。

## 回滚

保留上一个已验证的完整 onedir 目录和对应源码快照。回滚时替换整个目录，不只替换
exe；用户目录中的配置和证书状态由应用自身的恢复流程处理。回到原 CPU 环境：
取消 `GUANCE_MODEL_DEVICE`，使用原 `py -3.12 -m guance`，不要删除用户数据或旧证据。
