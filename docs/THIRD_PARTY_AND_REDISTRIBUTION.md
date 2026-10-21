# 第三方依赖与再分发状态

本文件记录当前离线评估交付涉及的第三方组件与再分发判断。
**它不是法律意见，也不构成任何再分发授权。**

## 结论

- 项目自有代码**没有**根目录许可证文件；本任务不替项目选择 MIT/GPL/Apache 等许可证。
- `REDISTRIBUTION_NOT_AUTHORIZED`：不得上传、发布、销售或对外分发源码包、权重或二进制。
- 本地 experimental/evaluation-only 使用不等于取得第三方再分发权利。

## 1. 已确认的上游材料

| 组件 | 版本/身份 | 许可证或状态 | 处理 |
|---|---|---|---|
| FableDan vendored runtime | commit `7cc5e311b9860bc44f76c082d9c1b21fc8b2d3ec` | `src/guance/infrastructure/model/fabledan_vendor/FABLEDAN_LICENSE`：Apache-2.0 + 非商业附加限制 | 保留许可证与 revision，不改写；仅本地非商业评估 |
| Python | 3.12.0 x64 | Python Software Foundation License | 随 onedir 的 Python runtime 保留 PSF 许可 |
| NumPy | 2.3.5 | BSD-3-Clause | 随包；保留版权与许可文本 |
| PySide6 / shiboken6 | 6.11.1 | Qt for Python；LGPLv3/GPLv3/商业许可 | **待确认**：再分发需保留通知与可重新链接义务 |
| Qt | 6.11.1（PySide6 wheel） | LGPLv3/GPLv3/商业许可 | **待确认**：插件与动态库再分发条款 |
| PyInstaller | 6.21.0 | GPLv2-or-later + bootloader exception | 仅构建工具；产物再分发仍受其他组件约束 |
| pywin32 | 311 | PSF-style | 仅 Windows；随包时保留许可 |
| pytest | 9.1.1 | MIT | 开发依赖，不进入评估包 |
| Microsoft VC++ Runtime | 14.44.35211.0（PySide6 wheel 提供） | Microsoft 可再分发条款 | **待确认** |
| FableDan 权重 `best.npz` | 16,076,952 bytes / SHA-256 `86570527c988026f74e21e84bf2f50876ede1d38c962d196e444115cf3571310` | 权利**未确认** | 仅本地评估；不声明可再分发 |
| PyTorch | 2.13.0+cu126（官方 wheel，来自 `build/gpu-env-20261020`） | BSD-style；CUDA 构建含 NVIDIA 运行时组件 | 仅本轮本机 local evaluation 包内复制；再分发**未授权** |
| NVIDIA CUDA runtime DLL（cudart/cublas/cudnn/cusparse 等） | 随 `torch/lib` | NVIDIA EULA / 再分发条款**未确认** | 仅本机 local evaluation；不声明可再分发 |
| MARVEL iter2000 checkpoint `m09_candidate_iter002000.pt` | 8,588,181 bytes / SHA-256 `af0df1ea…` | 训练侧来源 `guandan` 工作树；权利**未确认** | 仅本轮本机 local evaluation 包内复制；不提交 Git、不对外分发 |
| Phase 2 冻结 MARVEL runtime/source + native pyd | source fingerprint `246775cf…`，native `d7acbc8d…` | 上游 `guandan_marvel`/`nanopokerlib` 许可与再分发权利**未确认** | 仅本轮本机 local evaluation 包内复制；不提交 Git、不对外分发 |

## 2. 未包含、未授权的材料

| 材料 | 状态 | 说明 |
|---|---|---|
| MARVEL 其他 checkpoint（iter100/500） | 不随包 | 只有 iter2000 按本轮 allowlist 注入本机 GPU 包；权利未确认 |
| Guandan 训练代码/数据 | 不随包 | 不复制、不打包；GPU 包只含推理所需冻结 source/native 子集 |
| mitmproxy capture helper（Python 3.14） | 不随包 | 本轮 GPU 包与旧评估包都明确排除；capture 不可用 |
| 证书、CA 私钥、cookie、Authorization、真实抓包 | 绝不包含 | 隐私与安全边界 |
| 项目根许可证 | 不存在 | 不擅自创建或选择 |

## 3. 依赖分层

- **基础 CPU 运行**：Python 3.12、NumPy 2.3.5、PySide6 6.11.1、pywin32 311。
- **独立 GPU 开发环境**：`build/gpu-env-20261020` 中官方 `torch 2.13.0+cu126`；
  源码 GPU 推理与本轮本机 GPU 包均复用该已验收 runtime，不重装、不覆盖。
- **本轮 GPU 包内 runtime**：复制到 `_internal/marvel_runtime/site-packages` 的
  Torch/CUDA 子集、冻结 MARVEL source/native、iter2000 checkpoint 与 descriptor；
  仅本机 local evaluation，再分发未授权。
- **PyInstaller 构建环境**：与基础 CPU 环境相同，`PyInstaller 6.21.0`。
- **capture helper**：独立 Python 3.14 runtime，本轮不构建、不打包。

不要把这五类混成一个 `pip freeze`。

## 4. 待用户/法律确认

1. Qt/PySide6/shiboken6 的 LGPL 通知、动态链接与可重新链接义务。
2. Microsoft VC++ Runtime 的再分发条款。
3. FableDan 权重是否允许随包或跨机使用。
4. MARVEL checkpoint/source/native 与 PyTorch/CUDA 运行时组件是否允许跨机或对外分发。
5. 是否为项目自有代码新增根许可证。

在上述事项确认前：`REDISTRIBUTION_NOT_AUTHORIZED`、`publish=false`。
