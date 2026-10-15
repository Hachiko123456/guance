# Guance model backends

## 目的与边界

Guance 的模型接入现在通过一个模型无关的应用层 facade 运行：

```text
Capture / SessionEngine
  -> GameObservation
  -> CanonicalPublicGameView
  -> ModelService
  -> InferenceClient (latest-only JSON Lines transport)
  -> generic worker
  -> manifest + ProviderRegistry + AbstractFactory
  -> provider Adapter/ACL + Runtime + Strategy
  -> CanonicalAdviceResult
  -> Controller / ViewModel / QML
```

Controller、SessionEngine 和 UI 不依赖 FableDan、Guandan、NumPy、PyTorch、ONNX、`.npz`、`.pt` 或某个 provider 的 claim 语义。推荐只是一项建议；没有自动点击、自动出牌、请求重放或流量修改。

## CanonicalPublicGameView

`CanonicalPublicGameView` 是公开信息边界。它只包含 schema/ruleset、session/game/state identity、座位、级牌、play phase、trick、本人手牌、公开剩余张数、公开动作、领出动作、`visibility="public_plus_self"`、`completeness` 和 `ambiguities`。

它不包含对手隐藏手牌、post-state、结算、奖励、未来动作、payload、cookie、token 或 authorization。构造时采用白名单校验；状态版本、动作序号、实体牌和座位不合法时 fail closed。

Guandan 训练项目当前的 `StepwiseActionState/RoundState` 编码器虽然最终 tensor 不公开对手手牌，但它的合法 token 和状态投影仍依赖完整状态、声明、exchange/previous-result 等字段。Guance 不能把现有公开 `GameObservation` 补造成完整状态。缺字段时 Guandan provider 必须报告 unavailable/unsupported。

## CanonicalAdviceResult

`CanonicalAdviceResult` 保留旧 AdviceResult 的 session/game/state/request identity、候选、耗时、错误和 model fingerprint，同时为候选和结果增加：

- `score_kind`：例如 `fabledan_q` 或 `marvel_v2_policy_path_score`；后者只表示策略/路径排序分数，**不是胜率**；
- `metadata`：受限的 provider/model/runtime provenance；
- provider/model version。

FableDan 的 Q value、Guandan logits/token score、概率不是同一量纲。UI 不比较不同 provider 的 raw score；`claim` 只作为兼容字段，provider 不支持时可以为空。

## ModelService 与 Generic Worker

`ModelService` 是 Controller 的唯一模型 facade，负责 source selection、submit、invalidate、import、状态、生命周期和 callback generation。它拒绝旧 generation 的结果，不因加载失败自动恢复旧 provider，也不切换到 fake/rule provider。

`InferenceClient` 只负责进程、JSON Lines、request_id、latest-only queue、timeout、restart、close、response identity 和 stale rejection。worker 支持 `load`、`reload`、`recommend`、`close`、`stop` 以及 `loaded/status/result/error` envelope。selected/loading/loaded 使用同一个显式 source 和 `bundle_fingerprint`；加载失败会释放旧 provider，不恢复旧 source。畸形 JSON、过长消息、错误 protocol、request/state identity 不匹配均 fail closed。

## Provider Registry / Abstract Factory / Strategy / Adapter

Provider 通过显式注册表解析，当前注册：

- `fabledan`：生产兼容 provider，包裹现有 NumPy `FableDanAdvisor`；不改变旧编码、牌型解析或 `.npz` 推理算法；
- `guandan` / `builtin.guandan`：0.1 接口 scaffold。真实 runtime 缺失时不可用，不报告 ready；
- `guandan_marvel_v2`：独立的 0.2-MARVEL experimental/evaluation-only provider；只接受显式 deployment bundle，不借用 `guandan` 或 FableDan compatibility policy；
- fake runtime：只通过测试依赖注入使用，不注册到默认生产 registry。

Factory 根据 manifest 创建 Adapter、Runtime 和 Strategy。Adapter 是 Anti-Corruption Layer：把 canonical public view 转成 provider-specific input；provider tensor/token 不进入公共 contracts。Strategy 为 top-N、beam/search 或未来其他搜索替换点；它不改变 canonical state。

## Model Bundle / Manifest

模型包的典型结构：

```text
model_bundle/
  manifest.json
  artifact
  SHA256SUMS       # 可选但必须与 manifest 一致
```

manifest schema 1 要求：`schema_version`、`backend`、`provider`、`artifact`、`artifact_format`、`input_contract`、`output_contract`、`model_version`、`training_version`、`rules_version`、`action_version`、`environment_version`、`encoding_version`、`algorithm`、`architecture`、`runtime`、`artifact_sha256`、`capabilities`。协议版本、路径、hash、provider/backend/runtime 和能力组合必须校验。路径不能逃出 bundle；缺字段、未知 backend/provider/runtime、hash 错、protocol 不兼容、artifact 缺失都拒绝加载，不静默回退。

legacy `best.npz` 仍支持，但只通过显式 legacy FableDan bundle wrapper。只替换兼容模型包时，Controller、ModelService 主流程和 worker 不变；新增不同模型架构时，新增 provider/adapter/runtime/strategy 和 manifest，不修改主流程。

## FableDan 兼容

- 默认 source 仍优先选择用户明确配置的 source，其次是本地默认 bundle，再兼容 legacy `.npz`；
- FableDan 启动路径只导入 NumPy vendor，不要求 torch；
- 旧 `FableDanAdvisor` 与旧 AdviceResult 接口保留；
- FableDan provider 加载失败只返回 error/unavailable，不切换 Guandan 或规则推荐；
- QML 只显示通用模型摘要和导入入口，不再硬编码 FableDan 文案或仅允许 `.npz`。

## Guandan MARVEL V2 experimental 状态

Phase 6 已把固定 evaluation-only checkpoint 接到 typed provider/worker/config/UI 链路，但**没有**把它升级为 live-ready 或 production-ready：

- provider=`guandan_marvel_v2`，backend=`guandan`，runtime=`torch-cpu 2.13.0+cpu`；
- checkpoint SHA-256=`44b8a41371965279c7ce8f3e592162689e9a1b32cb4a07147f2a8ea58ff89696`；
- deployment fingerprint=`363723662970edc84dcdc63c143dbe3d8bb54816e0b5c4822cd32d184740f6ca`；
- `experimental=true`、`classification=evaluation_only`、`production_ready=false`、`strength_claim=false`；
- formal evaluation=`not_run`，champion=`not_established`，live eligible set=`[]`；
- 当前 `CanonicalPublicGameView` 不能证明完整 phase/declaration/previous-result/exchange、精确四 observer history/counter/private prefix，因此 provider 加载真实 runtime 后仍返回 `unavailable` / `GUANDAN_STATE_UNAVAILABLE`，并清除旧建议。

Phase 6 deployment bundle 的外层 artifact 是小型 JSON 描述，不是 checkpoint 副本。外层 manifest 先固定描述文件 hash；描述文件再固定 checkpoint schema/hash/bytes、algorithm、architecture、iteration、protocol、config/source/training backend、Windows CPython/Torch/native identity、evaluation 和 readiness。任何缺失或不匹配都 fail closed。固定 checkpoint 和 Guandan source 仍在原路径原地读取，不复制到 Guance。

本地 worker 不信任这些路径作为 Python 启动路径。source 模式使用 `-I -X utf8`，清除继承的 `PYTHONPATH`/`PYTHONHOME`，只引导可信 Guance 包。进入 provider 后先校验完整 source fingerprint，并拒绝额外的 startup/customizer 与高风险顶层 shadow 文件；再通过 deny-by-default finder 暴露精确验证过的 `guandan_marvel` / `nanopokerlib` 模块。source root 从不加入全局搜索路径，Python 模块在执行前再次校验字节。Native extension 使用 Windows read-share-only handle 锁定：在同一锁内验证 final path/hash/bytes、完成 loader import 和 build identity，期间写入、删除或替换均被拒绝。外部路径和 fingerprint 路径还拒绝 ADS、reserved/control/traversal 与 symlink/junction/reparse。因此 source 内的 `sitecustomize.py`、`usercustomize.py` 和 shadow 模块不能在验证前执行，native 也不能在 hash 与 loader 执行之间被交换。

本地实验使用三个显式配置；缺任何一个都 unavailable，不扫描磁盘：

```powershell
$env:GUANCE_MODEL_BUNDLE = 'D:\project\guance\build\acceptance\guandan-marvel-v2-phase6-20261013\experimental-bundle\manifest.json'
$env:GUANCE_MARVEL_V2_SOURCE = 'D:\project\guandan'
$env:GUANCE_MARVEL_V2_CHECKPOINT = 'D:\project\guandan\runs\M09_full_training\remote\ippo_pilot_failed_20261011\extracted-output\evaluation_checkpoints\iter000100.pt'
```

也可在设置页显式导入该 `manifest.json`；环境中的 source/checkpoint 路径不会进入 ModelMetadata、QML、日志或诊断导出。默认 source 搜索不包含 MARVEL V2，默认 FableDan-only import/start 不导入 Torch、`guandan_marvel` 或 `nanopokerlib`。

UI 显示 provider/backend/runtime/version、checkpoint/deployment fingerprint、capabilities、experimental/evaluation/readiness。即使 runtime 已成功加载，当前状态也显示 unavailable 而非 ready；未来若产生候选，`marvel_v2_policy_path_score` 只能标为“策略/路径分数（非胜率）”。

仍待 Phase 7 的内容：固定只读 capture 上逐 decision 的完整 public+self 状态、action rows、真实 recommendation acceptance。Phase 8 之前也不做正式 package/release 判断。

## 本地 capture 回归

`tests/local_capture_manifest.json` 只保存 capture ID、相对路径、source SHA-256 和安全质量元数据，不保存 frame、payload、完整对局、隐藏手牌或凭据。五个固定输入的 hash/decision metadata 已记录在任务验收文档。

默认 pytest 不运行 `local_capture`。显式运行：

```powershell
py -3.12 -m pytest -m local_capture -q
py -3.12 scripts/run_local_capture_acceptance.py
```

缺失、hash 不匹配、source 读取前后变化、converter error、partial/quarantine 或 decision count 不一致都必须显式失败；不得静默 skip。报告只输出状态、hash、局数和决策计数到 stdout/临时日志，不写 tracked path。

本仓库的 `debug/captures/` 继续保持 gitignored；不得 `git add -f`。本任务没有提交原始 `frames.jsonl`、payload、完整转换对局或模型权重。

## 强度评估与接口验收的区别

接口测试只能证明输入合法、隐私边界正确、输出候选属于本人手牌、identity/state version 正确、stale 被丢弃和错误不 fallback。它不能证明模型强度、胜率或优于另一个 provider。

只有在真实 checkpoint、固定 fingerprint/version/provider/runtime、独立评估数据和明确指标存在时，才可以做模型强度比较；训练 loss 不能直接证明线上推荐强度。

## 后续接入门禁

1. Phase 7 只使用明确授权、固定 hash 的只读 capture，逐 decision 证明完整 public+self 状态；缺事实继续 unavailable。
2. 完成 action row / history / cache / recommendation acceptance，不把 policy/path score 宣称为胜率。
3. Phase 8 再验证 clean package、许可、依赖、Windows smoke；默认包不得强依赖 Torch/native。
4. 正式 evaluation/champion 建立之前始终 `experimental=true`、`production_ready=false`，不得做强度或默认推荐声明。
5. 任一失败都不得 fallback、自动点击/出牌、请求重放或流量修改。

## 模型选择事务与失败语义

- 文件对话框取消不建立新选择，当前模型不受影响。
- 用户确认新路径后，Controller 先提升 selection generation、清除建议并撤销旧 ModelService；路径解析、preferences 写入、factory、load/start 都发生在旧模型已无推荐资格之后。
- preferences 使用当前目录内独占创建的临时文件和原子 replace。mkdir、序列化、写入或 replace 失败均保留旧文件字节，并使当前会话保持 fail-closed。
- requested source、persisted source、active source 分开记录；active source 只有 client 明确 ready 时才存在。
- ModelService 的 epoch 阻断旧 transport 回调，ViewModel 的 selection generation 阻断已进入 Qt 队列的旧 status/result。
- 旧 client invalidate/close 失败不会恢复其资格。用户必须再次明确选择并成功加载才能恢复推荐。
- 若存储完全不可写，只保证同一进程内的 failed-selection latch；不会虚构跨进程持久化保证。
