# 项目状态与能力矩阵（2026-10-20）

本文件是当前公开状态的唯一入口。它是离线交付准备记录，不是新的阶段验收，
也不改变任何 production/实战边界。内部 agent 记录不属于公开交付的一部分。

## 1. 阶段状态

| 阶段 | 状态 | 说明 |
|---|---|---|
| Phase 1–5 | PASSED（历史） | 固定输入、catalog/selection、真实 MARVEL 推荐、provider 切换、离线验收均已独立核验。 |
| Phase 5 Repair-1 | PASSED（历史） | 34 provider-eligible / 32 strict-eligible / 2 strict-ineligible，errors=0，fallback=0。 |
| GPU-FIRST INFERENCE | PASSED（历史） | `auto`/`cpu`/`cuda` 设备策略；真实 GPU 与 CPU-only 路径均已验收。 |
| GPU-FIRST INFERENCE Repair-1 | PASSED（历史） | 数值比较门禁与错误分类修复，独立 verifier 已通过。 |
| Phase 6 | PASSED（历史） | 20 个唯一 PID：**9 ready / 11 fail_closed**（纠正早期 8/12 汇总）。 |
| Phase 7 | **PAUSED / NOT STARTED / NOT AUTHORIZED** | 只有用户可单独授权；本轮未登录游戏、未采集、未训练。 |

Phase 6 统计口径：`N10-partial-beside-active` 是正向 partial-beside 检查，不是
fail_closed；`R8-post-ready-baseline` 是正向记录。原始 `restart-log.json` 未被改写；
纠正记录保存在本地内部证据中，不随公开源码包分发。

## 2. 能力矩阵

状态只取以下值：`PASSED`、`UNVERIFIED`、`UNSUPPORTED`、`NOT_IMPLEMENTED`、
`NOT AUTHORIZED`。机器可读版本见 `capability-matrix.json`（本任务证据）。

| 能力 | 状态 | 依据 |
|---|---|---|
| source CPU | PASSED | 已验收 Python 3.12 x64 + `torch 2.13.0+cpu`；`auto` → `cpu`，原因 `CPU_ONLY_TORCH`，同一权重产生合法推荐。 |
| source GPU auto | PASSED | 隔离环境 `build/gpu-env-20261020`（官方 `torch 2.13.0+cu126`）；`auto` → `cuda:0`，`CUDA_PROBE_OK`，合法推荐。 |
| source forced cpu | PASSED | cu126 构建强制 CPU 时 `actual_device=cpu`、`torch_cuda_build=12.6`，不伪造 CPU-only 构建。 |
| source cuda fail-closed | PASSED | 无可见设备时 `auto` → CPU 并给出分类原因；强制 `cuda` 以 `MODEL_UNAVAILABLE` 失败，不回退 CPU 或模型。 |
| fresh frozen no-capture | PASSED（历史，2026-10-20 前一轮） | 由当时工作树源码构建的 onedir；真实无网络 GUI smoke 与 frozen FableDan load/recommend/close/stop 通过。 |
| frozen MARVEL GPU（本机自包含 onedir） | UNVERIFIED（本轮实现完成，待独立核验） | 2026-10-20 从最终本地 commit 构建：包内收集 `torch 2.13.0+cu126`/CUDA DLL、冻结 MARVEL source/native、真实 iter2000 checkpoint 与 descriptor；实现者已对最终 ZIP 解压包运行 GUI、FableDan、GPU/CPU 推荐与负向测试，独立 verifier 未运行前不宣称 PASSED。 |
| 自包含 frozen GPU 跨机发行包 | NOT_IMPLEMENTED / UNVERIFIED | 本轮只验证本机（RTX 3050 Ti Laptop + 已验收驱动）；未在第二台无 GPU 或不同驱动机器测试，不声称全机型保证。 |
| capture | UNSUPPORTED / DISABLED | 本轮 GPU 评估包明确 `capture=false`；排除 Python 3.14 capture helper；frozen 模式不发现系统 mitmproxy。 |
| live UI / recommendation displayed | UNVERIFIED | no-network smoke 只证明窗口/QML 启动，不证明真实模型推荐显示或 live 输入。 |
| cross-machine | UNVERIFIED | 仅本物理主机；跨机 catalog、DPI、OS 差异未测试。 |
| redistribution | NOT AUTHORIZED | 本地 experimental/evaluation-only；FableDan 权重、Qt/CRT 与第三方再分发权利未确认。 |

## 3. 四类交付必须分开

1. **当前源码 + 隔离 CUDA 环境**：可以在固定离线输入上 `auto→GPU`，无 CUDA 时
   同一模型 `→CPU`。这不代表已经生成自包含 GPU 可执行包。
2. **旧 fresh no-capture onedir（2026-10-20 前一轮）**：Windows x64、CPython 3.12 闭包、
   不含 Torch/MARVEL/capture；FableDan（NumPy）可用，MARVEL 明确 unavailable。
   该包已被本轮 GPU 包取代，仅作历史证据保留。
3. **本轮本机自包含 GPU onedir（2026-10-20 final local GPU delivery）**：
   包内包含 `torch 2.13.0+cu126`、CUDA DLL、冻结 MARVEL source/native、
   真实 iter2000 checkpoint 和 descriptor；默认 `auto` 在本机使用 `cuda:0`，
   无可用 CUDA 时同一权重回退 CPU，强制 `cuda` 失败不回退。仅限本机
   local evaluation/no-redistribution；跨机与不同驱动未验证。
4. **实时采集包 / Phase 7 实战**：**NOT IMPLEMENTED / NOT AUTHORIZED**。
   不能把第 2 或第 3 类交付描述成实时采集已通过。

## 4. 不变的产品边界

- `production_ready=false`
- `strength_claim=false`
- `recommendation displayed=false`（UNVERIFIED）
- `live input used=false`（UNVERIFIED）
- `publish=false`
- 不自动点击、不替用户出牌、不重放请求、不修改流量
- 不记录/导出 cookie、Authorization、CA 私钥或隐藏对手手牌
- FableDan 评分与 MARVEL path score 都不是胜率，不构成强度或冠军声明

## 5. 相关公开文档

- [离线交付与运行手册](OFFLINE_DELIVERY.md)
- [GPU 推理与设备策略](GPU_INFERENCE.md)
- [模型后端](MODEL_BACKENDS.md)
- [构建](BUILD.md) / [发布](RELEASE.md)
- [第三方依赖与再分发](THIRD_PARTY_AND_REDISTRIBUTION.md)
