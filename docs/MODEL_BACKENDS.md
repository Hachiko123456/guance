# Guance model backends

> 当前阶段状态：Phase 1–6 与 GPU Repair-1 已独立通过；Phase 7 为
> **PAUSED / NOT STARTED / NOT AUTHORIZED**。`production_ready=false`、
> `strength_claim=false`，推荐分数不是胜率。权威能力矩阵见
> [PROJECT_STATUS.md](PROJECT_STATUS.md)。

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
  manifest.json    # schema 2（catalog）或 schema 1（legacy 直接选择）
  artifact         # 精确等于 manifest.artifact 的相对路径
  SHA256SUMS       # catalog 必须存在且与 manifest 一致
```

manifest schema 1 要求：`schema_version`、`backend`、`provider`、`artifact`、`artifact_format`、`input_contract`、`output_contract`、`model_version`、`training_version`、`rules_version`、`action_version`、`environment_version`、`encoding_version`、`algorithm`、`architecture`、`runtime`、`artifact_sha256`、`capabilities`。协议版本、路径、hash、provider/backend/runtime 和能力组合必须校验。路径不能逃出 bundle；缺字段、未知 backend/provider/runtime、hash 错、protocol 不兼容、artifact 缺失都拒绝加载，不静默回退。

manifest schema 2（模型目录必需）在 schema 1 之上新增：`model_id`（catalog 内唯一）、`artifact_bytes`、`runtime_contract`、`classification`（`production` / `production_candidate` / `experimental` / `evaluation_only` / `legacy`）、`readiness`（恰好 `experimental` / `production_ready` / `strength_claim` 三个布尔字段）。schema 2 会校验 artifact 字节数；未知键、重复 JSON member、非有限数、绝对路径或 `..` 逃逸、保留/ADS 名字都 fail closed。示例见 `docs/examples/model-bundle-fabledan-v2.json` 与 `docs/examples/model-bundle-marvel-v2.json`。

示例语义（不能混淆）：

- `docs/examples/model-bundle-fabledan-v2.json` 是**完整 bundle 配方**：把 `data/profiles/tencent_daguandan/models/best.npz` 复制为同目录 `artifact.npz` 即构成可验证 bundle（bytes/hash 与交付权重一致，`SHA256SUMS` 按 SHA-256SUMS 文本格式自行生成）。
- `docs/examples/model-bundle-marvel-v2.json` 是**模板**，不是可直接激活的合法 bundle：它固定的是 Phase 6 证据中 iter000100 `deployment.json` 的 bytes/hash（`1fb26d96…`/16479），仓库内不包含该 artifact 文件；打包新的 MARVEL 部署时必须由打包步骤重算 `artifact_bytes` 与 `artifact_sha256`。Phase 2 起，真实 bundle 由 `python scripts/build_marvel_materials.py build-bundle` 从冻结材料生成（示例见 phase2 证据目录），`artifact_bytes`/`artifact_sha256` 均由构建步骤计算。

**声明（claims）认证**：provider 对 `classification`/`readiness` 有严格 discovery 契约，manifest 不能自报升级。`guandan_marvel_v2` 只允许 `classification=evaluation_only` 且 `experimental=true`、`production_ready=false`、`strength_claim=false`；FableDan 只允许 `legacy`（`experimental=false`）或 `experimental`/`evaluation_only`（`experimental=true`），且 `production_ready`/`strength_claim` 必须为 false；Guandan scaffold 只允许 `experimental`/`evaluation_only`。任何伪造 production/strength 声明都会在 discovery 阶段以 `CLAIM_UNSUPPORTED` 拒绝，未经 deployment/evaluation 身份验证不能提升；iter2000 之类 evaluation-only 权重不得被宣称为 production champion。

legacy `best.npz` 仍支持，但只通过显式 legacy FableDan bundle wrapper。只替换兼容模型包时，Controller、ModelService 主流程和 worker 不变；新增不同模型架构时，新增 provider/adapter/runtime/strategy 和 manifest，不修改主流程。

## 模型目录（catalog）与 active selection

Phase 1 起，模型来源由数据驱动的 catalog 决定，代码不再固定 checkpoint 身份：

```text
<app-data>/Guance/models/
  catalog/<bundle-directory>/     # 直接子目录，每个是一个完整 bundle
    manifest.json
    artifact                      # FableDan: artifact.npz；MARVEL: deployment.json
    SHA256SUMS
  active_model.json               # 显式选择记录（schema 1）
```

- catalog 只扫描该根目录的直接子目录，不扫全盘；文件、隐藏目录（`.` 开头）和嵌套目录被忽略。每个候选目录独立校验 manifest、artifact、SHA256SUMS、路径安全（symlink/junction/reparse/ADS/reserved 拒绝）和 provider family contract；单个坏包只记录问题（`catalogProblems`），不影响其他条目的发现。
- **catalog root 绑定与 reparse 边界**：`ModelCatalog` 把 catalog root 绑定到显式的 trusted models root（active 文档所在目录，且由组合根以 `root=` 明确传入）；catalog root 与 active 文档都必须位于该 root 内。每次 scan/resolve 都会重新检查 catalog root **及其全部现有父目录链**上的 symlink/junction/reparse（在任何目录内容被读取之前），scan 之后 root 被替换为 junction 也会在下次 resolve 时 fail closed；目标 bundle 的目录/manifest/artifact/SHA256SUMS 检查保持不变，不放宽。
- `discovered` 不等于 `active`。发现结果按 `model_id` 排序展示，但不会按 iteration、mtime 或目录名自动选择；没有任何自动升级。
- `model_id` 在 catalog 内必须唯一；重复时两个条目都被排除并在问题列表标记 `DUPLICATE_MODEL_ID`。**active resolve 以完整的去重后扫描为准**：只有确切的 `(model_id, bundle 目录)` 对在验证后的条目中恰好出现一次才允许激活；重复 id、problem 目录、被替换/篡改的目标目录一律 `ACTIVE_SELECTION_INVALID`，没有任何目录名/iteration/mtime 启发式。
- 显式选择后写入 `active_model.json`（连同提交标记 `active_model.commit.json`，内容为文档字节的 SHA-256）：
  - catalog 选择：`{"schema_version":1,"kind":"catalog","model_id":"...","bundle":"<相对目录名>","selected_at":"..."}`；
  - 直接导入（文件对话框 legacy npz / manifest / bundle 目录）：`{"kind":"legacy","path":"..."}`。
- **选择持久化是带提交标记的事务**：先原子 stage 文档，再写 preferences 镜像，最后写 commit 标记；提交点之前的文档不会被启动激活。preferences 或标记任一步失败都会把 active 文件恢复为先前精确字节；即使恢复也失败，文档哈希与标记不再匹配，重启仍然 fail closed（显式错误并要求重新选择），不会激活失败选择。
- 启动解析顺序：环境覆盖（`GUANCE_MODEL_BUNDLE`/`GUANCE_MODEL_PATH`，不持久化）→ `active_model.json`（必须与 commit 标记匹配）→ 旧 `preferences.model_path` 显式迁移 → 未选择。
- **旧配置迁移是严格一次性的**：只有迁移成功提交 active 文档后才采用旧 legacy 选择；迁移失败（目录/写入/replace/磁盘失败）时启动是明确的 `unavailable`，不会无 active 文档继续加载旧路径。
- 未选择时状态是明确的 `unavailable`（`MODEL_NOT_SELECTED`），不会隐式加载任何默认权重；`active_model.json` 损坏、未提交（标记缺失/不匹配）、指向缺失/损坏/逃逸/重复的 bundle 或 junction 污染的 catalog 时进入 error latch，并**持久化失败闩锁**：即使外部原因（篡改、重复包、junction）被移除，也必须由用户显式重新选择后才能恢复，不会静默自动激活。
- **reparse 边界**：catalog 根、bundle 目录、manifest、artifact、`SHA256SUMS`，以及 `active_model.json`/commit 标记与它们的父目录链，遇到 symlink/junction/reparse 一律拒绝；active 事务的临时文件与目标必须位于声明的 models root 内。
- 选择事务的加载语义与既有 import 流程一致：selection generation 提升后旧 client 先失去资格，加载失败保持 fail-closed 并要求显式重试。
- 设置页提供模型目录列表（刷新、显式选择、当前选择、问题条目）。QML 只读取 ViewModel 属性，不接触 manifest、provider 或文件系统。

MARVEL 只保证可发现（iter100/iter2000 同时列出）。加载仍走 provider 的完整校验；Phase 2 起，iter100（ddp merged）与 iter2000/iter500（flat）的真实 deployment bundle 均可加载并按精确 payload 变体校验，未知变体或任何身份不匹配 fail closed，不会 fallback。

## FableDan 兼容

- 启动来源只由显式选择决定：`active_model.json`、环境覆盖或旧 `preferences.model_path` 的显式迁移；没有隐式默认权重加载；
- FableDan 启动路径只导入 NumPy vendor，不要求 torch；catalog 扫描 FableDan bundle 也不会导入 torch/native；
- 旧 `FableDanAdvisor` 与旧 AdviceResult 接口保留；
- FableDan provider 加载失败只返回 error/unavailable，不切换 Guandan 或规则推荐；
- QML 只显示通用模型摘要和导入入口，不再硬编码 FableDan 文案或仅允许 `.npz`。

## Guandan MARVEL V2 experimental 状态

Phase 6 已把固定 evaluation-only checkpoint 接到 typed provider/worker/config/UI 链路；Phase 2 将 checkpoint 身份改由每个 bundle 的 deployment descriptor 声明（代码中不再有全局 checkpoint/iteration/source 常量），但**没有**把它升级为 live-ready 或 production-ready：

- provider=`guandan_marvel_v2`，backend=`guandan`，runtime=`torch-cuda` 或 `torch-cpu`
  （随本次实际执行设备；`torch.version.cuda` 只表示构建信息，不表示执行设备）；
- Phase 2 真实 bundle（descriptor 均为小型 JSON，不是 checkpoint 副本）：
  - `marvel-v2-iter000100`：champion `b00a2c17…`/8,316,529，payload variant `ddp_merged_frozen_v1`；
  - `marvel-v2-iter002000`：candidate `af0df1ea…`/8,588,181，payload variant `flat_not_smoothed_v1`；
  - `marvel-v2-iter000500`：same-contract future fixture `f4d1d587…`/8,366,101，payload variant `flat_not_smoothed_v1`；
- 运行时来源：由 `scripts/build_marvel_materials.py` 从冻结材料材料化到 Guance 证据目录（runtime_id `marvel-e5c82e7c27fb-d7acbc8d76c9`，native `d7acbc8d…`/3,685,033，source fingerprint `246775cf…`）；四条件门禁全部通过（descriptor fingerprint / native bytes+hash / raw native source manifest == pyd `embedded_source_sha256` / 隔离进程实测 `source_matches_loaded_binary=true`）。installer 在任何写入前严格校验 source manifest 与 zip 成员（拒绝逃逸/绝对/反斜杠/ADS/reserved/重复/内嵌不一致），并在同级 dot-prefixed staging 中完成组装与全量 hash 复验后才原子 rename；失败只删除本次 staging，target 与其外不留下任何文件。build-bundle 同样原子化：已存在的输出显式失败、不覆盖不合并，半包不可被 catalog 发现。install-runtime 还要求调用方显式提供全部七个 trusted pins（`--expected-source-zip-sha256`/`--expected-source-manifest-sha256`/`--expected-source-content-manifest-hash`/`--expected-source-commit`/`--expected-native-sha256`/`--expected-native-bytes`/`--expected-overlay-manifest-hash`，来源为受信任的 runtime-materials 记录，绝不从待安装材料自身推导）；pin 全部通过前不创建 staging、不解压、不执行 archive Python、不加载 pyd、不运行 probe。probe 以 `python -I -B` 隔离运行并从 `sys.path` 移除 source root，只用 deny-by-default finder 加载经字节校验的 `guandan_marvel/`+`nanopokerlib/` 模块与 native；zip 读取先做元数据门禁（declared size==manifest、单成员 16 MiB、总量 64 MiB、成员数、compress_size、压缩比 128），再以最多 expected+1 字节的 bounded streaming read 读取；
- `experimental=true`、`classification=evaluation_only`、`production_ready=false`、`strength_claim=false`；
- formal evaluation=`not_run`，champion=`not_established`，live eligible set=`[]`；
- 当前 `CanonicalPublicGameView` 不能证明完整 phase/declaration/previous-result/exchange、精确四 observer history/counter/private prefix，因此 provider 加载真实 runtime 后仍返回 `unavailable` / `GUANDAN_STATE_UNAVAILABLE`，并清除旧建议。

deployment bundle 的外层 artifact 是小型 JSON 描述（<64KB），不是 checkpoint 副本。外层 manifest 先固定描述文件 hash；描述文件再固定 checkpoint schema/hash/bytes、algorithm、architecture、iteration、protocol、config/source/training backend、payload variant（精确枚举：`ddp_merged_frozen_v1` / `flat_not_smoothed_v1`）、Windows CPython/Torch/native identity、source fingerprint、evaluation 和 readiness。任何缺失或不匹配都 fail closed；`weights_only=True` 之前始终先校验 bytes/hash，payload 内容不对外输出。checkpoint 仍在原路径原地读取（不复制）；运行 source 必须指向按上述冻结身份材料化并通过四条件门禁的 runtime 目录——Phase 2 起 Guandan worktree 不再是可信 runtime root（其 native 已被外部重建），用 worktree 作为 source 会 fail closed。

本地 worker 不信任这些路径作为 Python 启动路径。source 模式使用 `-I -X utf8`，清除继承的 `PYTHONPATH`/`PYTHONHOME`，只引导可信 Guance 包。进入 provider 后先校验完整 source fingerprint，并拒绝额外的 startup/customizer 与高风险顶层 shadow 文件；再通过 deny-by-default finder 暴露精确验证过的 `guandan_marvel` / `nanopokerlib` 模块。source root 从不加入全局搜索路径，Python 模块在执行前再次校验字节。Native extension 使用 Windows read-share-only handle 锁定：在同一锁内验证 final path/hash/bytes、完成 loader import 和 build identity，期间写入、删除或替换均被拒绝。外部路径和 fingerprint 路径还拒绝 ADS、reserved/control/traversal 与 symlink/junction/reparse。因此 source 内的 `sitecustomize.py`、`usercustomize.py` 和 shadow 模块不能在验证前执行，native 也不能在 hash 与 loader 执行之间被交换。

本地实验使用三个显式配置；缺任何一个都 unavailable，不扫描磁盘：

```powershell
$env:GUANCE_MODEL_BUNDLE = 'D:\project\guance\build\acceptance\guance-model-platform-20261016\phase2\bundles\marvel-v2-iter000100\manifest.json'
$env:GUANCE_MARVEL_V2_SOURCE = 'D:\project\guance\build\acceptance\guance-model-platform-20261016\phase2\runtime\marvel-e5c82e7c-d7acbc8d\source'
$env:GUANCE_MARVEL_V2_CHECKPOINT = 'D:\project\guandan\runs\comparison_delivery\dataset\m09_champion_iter000100.pt'
```

也可在设置页显式导入该 `manifest.json`；环境中的 source/checkpoint 路径不会进入 ModelMetadata、QML、日志或诊断导出。默认 source 搜索不包含 MARVEL V2，默认 FableDan-only import/start 不导入 Torch、`guandan_marvel` 或 `nanopokerlib`。

UI 显示 provider/backend/runtime/version、checkpoint/deployment fingerprint、capabilities、experimental/evaluation/readiness。即使 runtime 已成功加载，当前状态也显示 unavailable 而非 ready；未来若产生候选，`marvel_v2_policy_path_score` 只能标为“策略/路径分数（非胜率）”。

recommendation 口径：Phase 3 起的 offline confirmed-decision 路径已在固定只读输入上
通过真实 iter2000 + Phase 2 frozen runtime 产生合法推荐；Phase 5/6 与 GPU Repair-1
继续验证了固定输入、设备策略和重启/切换。仍未验证的是 **live public+self 状态、
真实 UI 推荐显示和 live input**；这些保持 UNVERIFIED/false。Phase 7 为
PAUSED / NOT STARTED / NOT AUTHORIZED。

## Phase 3 offline confirmed-decision recommendation

Phase 3 把完整 confirmed decision 接入真实 iter2000（Phase 2 冻结 runtime），仅离线、只展示：

- wire protocol 升级到 v3：`recommend` 显式携带 `request_kind`（`public` / `confirmed_decision`）；每种 kind 有精确字段集，未知/混杂字段、未知 kind 一律 `PROTOCOL_INVALID`；旧四字段 envelope 不再接受。
- confirmed 请求载荷为 `GuandanMARVELV2.ConfirmedDecision.v1` 有界纯数据（13 个顶层键、≤512 history steps、无 tensor/路径/provider 对象）；client 与 worker 两侧用同一 strict parser 校验，并重算 history/cache/model identity 与 decision identity sha256。
- provider 在 worker 内从冻结 GD-RULES 重新生成 exact legal rows（候选集 sha 必须与 decision 一致），运行真实 autoregressive/cache 路径直到完整 COMMIT，再做 own-hand/legal-set/declaration/wildcard/beat（rules oracle）/canonical advice 全量校验；`score_kind=marvel_v2_policy_path_score` 是**完整动作路径分数**：所有已选 token（含最后一个 COMMIT token）的自然对数概率之和（≤0，越大越好；未归一化 log-likelihood；**非胜率、非 Q 值、不得作强度结论**）。
- phase/current 拓扑一致性由 parser、provider、recommendation 三层强制：`LEAD_PLAY` 必须无 current 且 `leader_seat==active_seat`、`consecutive_passes==0`；`FOLLOW_PLAY` 必须有 declaration、有效且 `!=active_seat` 的 winner，MarvelState 的 family/comparison_rank/winning_size/winning_cards 与 declaration 精确一致，且 winner/leader 必须与 exact history 的最近有效 play/最近 lead actor 及 trailing passes 一致；oracle 拒绝 `FOLLOW_PLAY + current=None` 与 `LEAD_PLAY + current`；`decision_identity_sha256` 绑定完整 canonical state 与 declaration。
- controller 每个 turn 只走一条路径：MARVEL 仅在 ready + 匹配当前观察的完整 confirmed decision 时提交；selection/reload 期间 pending 绑定 generation，新选择为 FableDan 才 replay public observation，为 MARVEL 时只等待新 generation 且 identity 匹配的 decision；stop/close/model error/unavailable/timeout/selection-load failure/runtime-capture failure/state advance/terminal/pause 统一清除 `_pending_confirmed_key`/`_pending_model_observation`/`_pending_model_generation`/`_active_advice_key`。debug capture disabled 时不写 raw frame、不创建 converter，但真实 provider 通过独立 `runtime-session` identity 继续产生 confirmed decisions。
- MARVEL public-view 请求仍 `GUANDAN_STATE_UNAVAILABLE`；仅声明 `complete_action`+`confirmed_decision` 的 bundle 装载后为 ready；FableDan 继续只接受 public request，confirmed 请求返回 `REQUEST_KIND_UNSUPPORTED`；无任何回退。
- Phase 3 bundle 位于 phase3 evidence 下（同一 iter2000 / Phase 2 runtime identity，evaluation-only），Phase 2 evidence 未改动。

## GPU-first inference device policy (2026-10-20)

MARVEL V2 now has a centralized device policy (`auto` / `cpu` / `cuda`) with a
real CUDA execution probe (allocation + matmul + synchronize). `auto` uses CUDA
when it is genuinely available and otherwise falls back to CPU with a stable,
observable reason; `cuda` fails closed without CPU fallback; GPU execution
errors after load (OOM/assert/unknown) fail closed and are never classified as
"no GPU". The legacy `marvel_v2.cpu.v1` contract keeps its original CPU-only
meaning; the new `marvel_v2.device.v1` contract is device-flexible while keeping
the same checkpoint SHA-256, iteration and model identity. FableDan is
unchanged and still never imports Torch/MARVEL.

The isolated environment, launch commands, metadata fields, acceptance runner
and rollback instructions are documented in `docs/GPU_INFERENCE.md`. GPU-first
inference and its Repair-1 were independently verified; Phase 6 was completed
and independently verified afterwards. Phase 7 remains **PAUSED / NOT STARTED /
NOT AUTHORIZED**. This task did not change training, capture, proxy, certificate
or automatic game-action behavior.

## fresh no-capture 评估包（2026-10-20）

由当前工作树源码构建的 fresh no-capture onedir 只包含 FableDan（NumPy）与 Qt/QML，
不包含 Torch、MARVEL runtime/source、checkpoint 或 capture helper。frozen 模式下：

- FableDan 可完成真实 load/recommend/close/stop；
- MARVEL descriptor 加载明确 fail-closed（`MODEL_UNAVAILABLE`/`MODEL_LOAD_FAILED`），
  不会自动加载 FableDan 或替换 checkpoint；
- 缺失模型明确失败，不返回 ready；
- 设备能力与包能力矩阵见 [项目状态](PROJECT_STATUS.md) 与
  [离线交付](OFFLINE_DELIVERY.md)。自包含 frozen CUDA 包为 NOT_IMPLEMENTED。

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

1. Phase 7 仍 **PAUSED / NOT AUTHORIZED**：只有用户明确授权后才能使用固定 hash 的
   只读 capture，逐 decision 证明完整 public+self 状态；缺事实继续 unavailable。
2. 完成 live action row / history / cache / recommendation acceptance，不把
   policy/path score 宣称为胜率。
3. 独立验证 fresh no-capture 包、许可、依赖与 Windows smoke；默认包不得强依赖
   Torch/native。自包含 frozen CUDA 与跨机交付仍需单独工程与授权。
4. 正式 evaluation/champion 建立之前始终 `experimental=true`、`production_ready=false`，
   不得做强度或默认推荐声明。
5. 任一失败都不得 fallback、自动点击/出牌、请求重放或流量修改。

## 模型选择事务与失败语义

- 文件对话框取消不建立新选择，当前模型不受影响。
- 用户确认新路径后，Controller 先提升 selection generation、清除建议并撤销旧 ModelService；路径解析、preferences 写入、factory、load/start 都发生在旧模型已无推荐资格之后。
- preferences 使用当前目录内独占创建的临时文件和原子 replace。mkdir、序列化、写入或 replace 失败均保留旧文件字节，并使当前会话保持 fail-closed。
- requested source、persisted source、active source 分开记录；active source 只有 client 明确 ready 时才存在。
- ModelService 的 epoch 阻断旧 transport 回调，ViewModel 的 selection generation 阻断已进入 Qt 队列的旧 status/result。
- 旧 client invalidate/close 失败不会恢复其资格。用户必须再次明确选择并成功加载才能恢复推荐。
- 若存储完全不可写，只保证同一进程内的 failed-selection latch；不会虚构跨进程持久化保证。
