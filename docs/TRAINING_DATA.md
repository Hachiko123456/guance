# 自动后台训练数据：存储协议、字段语义与离线使用

版本：1；实现日期：2026-10-02。适用代码为 `guance.infrastructure.training_data`。

> 本文是给维护者、训练脚本和大模型阅读的规范。它描述已实现的功能，不把前期 Demo 中的占位字段当作真实训练协议。
> 数据是**候选数据**，不证明动作正确或模型提升。不自动训练、不自动上传、不自动替换模型。

## 1. 系统边界与运行方式

- 真实采集仍经过原来的授权、协议解析和 `SessionEngine` 验证；本模块只是其安全输出的订阅者。
- 默认开启，不新增页面或采集按钮。成功启动原采集流程时创建记录线程；首次收到有效状态也可惰性创建。
- 保存目录为原用户数据目录下的 `training_data`。默认 Windows 路径为 `%LOCALAPPDATA%\Guance\training_data`；已有 `GUANCE_DATA_DIR` 覆盖对本模块同样有效。
- 故障排查可在启动进程前设 `GUANCE_TRAINING_DATA=0`（或 `false`、`off`）禁用。该开关没有新增 GUI。
- 记录器 producer 只做有上限的白名单字段复制和 `Queue.put_nowait`；JSON 编码、状态关联、校验、目录创建、flush/fsync、原子替换均在 `guance-training-data` 后台线程。**并不声称零 CPU/内存开销**。
- 现有调试报文录制是另一个系统，行为保持不变；本模块不复用原始抓包文件作为在线输入，也不改变它原来的错误处理策略。
- 队列默认最多 256 条状态快照，单快照最多 4096 条公开动作，单局最多 256 条本人决策。超限/缺帧不能悄悄恢复为完整数据。
- 新增数据写盘失败不调用 `model.invalidate()`、不改变 UI 当前错误、不停止 runtime；只在记录器统计和 manifest 中留下原因。原来真正的协议/状态错误仍照常 fail-closed。
- 停止时先执行原有 runtime/代理/model 清理，再通知记录器停止接收。Controller 使用 `close(timeout=0)`，不等待磁盘；后台尽可能排空。独立 API 默认 timeout 为 0.2 秒，最多 1 秒。
- daemon 线程在进程被杀时可能来不及排空。临时文件/`open` manifest/未刷完计数不是完整落盘证明。本版**不实现崩溃后自动补齐记录或自动重放缺失动作**。

## 2. 稳定语义层与派生层

```text
已验证的 EngineUpdate
  → 有界白名单快照 → 独立后台构建器
  → canonical decision JSONL + manifest
  → 显式离线适配（可重做）
  → 当前 FableDan tokens / chosen_action_features / 终局目标
```

语义层保留可验证的本人手牌、公开历史、确认动作、结果，不绑定 80 维向量或网络层数。派生层绑定明确的 encoder/reward 版本。网络加深但输入约定不变时，编码甚至可以直接复用；输入/词表改变时才重新编码。新模型需要从未采集的时间、隐藏牌或额外标签时，不能补造，只能拒绝或屏蔽对应任务。

本版的明确限制：

1. 不保存模型 Q 分数、Top-N、点击候选或人类“更好选择”。`observe_advice` 是有意忽略；缺失不是分数 0，也不是无合法动作。
2. 不在线保存全部合法动作。`legal_actions=null` 表示未记录；指定版本规则可离线重新生成。生成器可能选用代表性实体牌组合，不承诺枚举所有等价实体选择。
3. 不将“实际动作”当作“最优动作”。终局团队回报也不证明每一步正确。
4. 不采集对手隐藏手牌，即使结算事件中包含。真实数据的 belief/oracle loss 一律没有标签。
5. 不自动划分训练/验证/测试集。训练工程必须以 episode 为组，且先处理跨录制重复。
6. 合成数据与真实数据有不同 source；合成测试不能代替真实旧局验证，更不代表完成训练收益验收。

## 3. 文件布局、完整性和生命周期

```text
<用户数据目录>/training_data/
  episodes/<episode_id>.jsonl      # 一行一个本人 decision；同局更正原子替换
  manifests/<run_id>.json          # 在线运行清单
  manifests/legacy-<sha256>.json   # 同源旧文件转换清单，重复转换幂等
```

每个 episode 文件以临时文件写完、flush/fsync 后 `os.replace`。不把半行 JSON 当有效产物。合法 settlement 到达即发布，不等待下一局或退出；manifest 随发布更新。相同结算不重复生成。后来出现冲突、记录丢失时，会以同 episode ID 更正为 quarantine，而不是并存两份相互矛盾的“真相”。

状态链路：

- `active`：记录前置状态，只有紧接其后的**确认本人动作**才创建 decision。
- `ended`：仅说明行动结束，仍无终局标签；不得当作 settled。
- `settled`：仅接受 `EngineUpdate.kind="state"` 且 `message="settlement"` 的安全验证摘要。
- 新 Deal/正常停止：未取得结算的已确认部分可封为 partial；没有任何决定性动作时可只有零记录 episode 元信息。
- 错误/队列溢出：当前 episode 失效，输出 quarantine；后续数据必须从新的 Deal 边界建立新局。

训练前应读取 manifest 及质量状态，不能只扫描所有 JSONL 当作合格样本。`manifest.status` 为 `closed` 才证明写线程正常结束；`open`/`degraded` 必须视为未完成/需要检查。完整文件的存在不证明整个运行无丢失。严重磁盘故障可能连 degraded 状态也写不成功；旧的 open 标记仍不能放行。

## 4. Decision 顶层字段

所有列出的字段由 v1 写入器输出。没有额外任意字段透传。`validate_decision` 拒绝未知必需版本、未知顶层字段和非有限数。机器可读 JSON Schema Draft 2020-12 结构见 `TRAINING_DATA_SCHEMA.json`；同字段的完整合成 JSON 例子见 `examples/training-data-decision-v1.json`。示例仅是结构/动作关联测试，不是经过完整108牌真实对局复核的训练素材。双副牌实体码及状态前后关联等跨字段语义仍以 `validate_decision` 和现有状态机为准。

| 字段 | 类型 / 可空 | 含义 |
|---|---|---|
| `record_type` | 固定 `decision` | 本行记录类型，不是 episode 摘要 |
| `schema_version` | integer，固定 1 | 本存储协议版本，与 `GameObservation.schema_version` 是不同协议 |
| `action_schema_version` | integer，固定 1 | 观测动作身份算法版本 |
| `card_encoding` | 固定 `guance-physical-v1` | 实体牌编码；不可当作 FableDan 0..107 ID |
| `record_id` | SHA-256 hex | 由 episode ID、确认动作 index 和动作签名计算 |
| `provenance` | object，必需 | 来源、局身份、时间和规则版本，详见下表 |
| `pre_state` | object，必需 | 动作发生之前的可信本人视角，禁止追加后来的历史/结果 |
| `observed_action` | object，必需 | 后续已确认发生的本人动作，不来自推荐或点击 |
| `post_state` | object，必需 | **该动作之后**的状态，仅用于转移验证/离线任务；不能误作当前动作输入 |
| `status` | enum | `accepted_candidate` / `partial` / `quarantine` |
| `quality` | object | 与 status 一致的判定、原因和丢失标记 |
| `outcome` | object/null | 仅验证结算后有安全名次；与输入分离 |
| `reward` | 固定 null | 语义层不绑定某个奖励函数；派生层按版本计算 |
| `privacy_scope` | 固定 `own_hand_and_public_actions_only` | 包含本人私有手牌，不等于可公开分享 |
| `legal_actions` | 固定 null | 未在线记录，绝不解释为“没有合法动作” |
| `legal_actions_status` | 固定 `not_recorded_regenerate_from_rules` | 必须通过指定规则/编码器再生成 |

### 4.1 provenance

| 字段 | 类型 / 约束 | 语义 |
|---|---|---|
| `source` | `real` / `legacy` / `synthetic` | 在线真实输出 / 旧录制重建 / 人工测试合成；互不混用 |
| `source_id` | bounded string | 在线随机 run 身份；旧文件为 `sha256:<文件摘要>`，不是原路径 |
| `episode_id` | SHA-256 hex | hash(source, source_id, session_id, game_id) |
| `session_id` | bounded string | 引擎逻辑会话，不是平台账户或 Cookie |
| `game_id` | bounded string | 引擎生成的局号，仅在 source/run 内唯一 |
| `source_sequence` | integer/null | **动作后更新**的本地输入序号，不是服务端全局 action ID |
| `captured_at` | finite nonnegative number | 动作后快照的原捕获时间；单位为秒，保持输入时钟含义，不推断玩家思考时长 |
| `rule_profile` | string/null | 当前线上/旧局转换填 `tencent_daguandan`，是本项目兼容档案标识 |
| `rule_revision` | string/null | 当前填 `guance-observed-v1`；不是“自动支持未来所有规则”的承诺 |

identifier 只允许受限 ASCII，最长160字符，不接受任意自由文字。旧文件路径、连接 ID、账户标识、原始错误消息不输出。规则未知可以保留语义记录，但当前派生器会拒绝不支持/缺失的规则档案。

### 4.2 pre_state / post_state

两个对象具有相同结构，但可见时点不同。`pre_state` 是模型动作输入；`post_state` 已含该动作，训练当前动作时默认禁止送入编码器。

| 字段 | 类型 / 约束 | 语义 |
|---|---|---|
| `session_id`, `game_id` | string | 与 provenance 同局 |
| `state_version` | nonnegative integer | 引擎版本；确认动作后必须恰好 +1，不等于每个网络帧 +1 |
| `captured_at` | finite nonnegative number | 该快照自己的捕获时间，pre 与 post 分开 |
| `phase` | enum | `waiting_deal/waiting_start/active/ended/settled`；decision 的 pre 必须 active |
| `level` | integer 1..13 | 本局级牌：1=A，11=J，12=Q，13=K |
| `my_seat` | integer -1..3 | 观察者绝对座位，-1 未确认；未确认的 pre 不能作为本人样本 |
| `first_seat`, `current_seat` | integer -1..3 | 首家/当前行动者；-1未知或无行动者，不是座位0 |
| `hand` | unique integer[]，最多27 | 本人的实际实体牌，按整数排序；不包含对手手牌 |
| `remaining` | integer[4]，各0..27 | 按绝对座位0..3排列，不是“我、下家、对家、上家”的相对顺序 |
| `history` | PublicAction[]，最多4096 | 已确认公开动作，从 index=1连续；保留完整收集历史，不按当前模型512 token截断 |
| `free_lead` | boolean | 是否自由领出；不能仅凭“上一手是 pass”推断 |
| `trick` | nonnegative integer | 当前轮次；与 history.trick 共同确定当前领出 |
| `seat_confirmed` | boolean | 必须和 my_seat 是否0..3一致 |
| `seat_confirmation_source` | enum/null | `manual/inferred/3019` 或 null；未知自由文本不透传 |
| `finish_ranks_by_seat` | integer[4]，可不提供 | 当前已经公开的名次；-1未定，1..4名次。不是提前加入最终结果 |
| `lead_action` | PublicAction/null，可不提供 | 当前引擎 table 通常不提供。派生器用 free_lead、trick 和公开历史重建，不能凭空猜 |

当首次本人动作才确认座位时，其**动作前**座位尚未确认，不能借助后见信息制造前置样本。这类动作仍保留在后续公开历史中，但不生成该步本人 decision。

### 4.3 PublicAction 与 observed_action

PublicAction 固定字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `index` | integer 1..4096 | 局内确认动作索引，从1连续，不是数组当前排序位置 |
| `player` | integer 0..3 | 动作主体绝对座位 |
| `kind` | `play` / `pass` | 出牌或确认过牌；未知动作不能改写成 pass |
| `cards` | unique integer[] | 物理牌集合，排序规范化；pass 必须空，play 必须非空 |
| `combo_type` | integer/null | 已观察协议牌型码，不是 FableDan 的模型枚举编号；含义由规则档案决定 |
| `next_player` | integer0..3/null | 确认消息中的后续行动者；null不代表0 |
| `trick` | integer | 动作所属轮次 |

`observed_action` 额外包含 `signature` 和 `event_index`。`event_index == index`。

`signature = SHA256(canonical_json([action_schema_version, {kind,cards,combo_type}]))`。它不依赖候选数组下标、演员座位或轮次；但只标识**已观测实体动作**，不是“任意上下文中语义完全相同”的证明。实体相同但逢人配解释不唯一时，不会凭签名认定 claim；派生器必须唯一解析或拒绝。

`record_id = SHA256(canonical_json([episode_id, index, signature]))`，因此同动作跨不同决策不混淆。canonical JSON 为 UTF-8、按键排序、紧凑分隔符、禁止 NaN/Infinity，具体实现为 `schema.canonical_json/stable_id`。

## 5. 双副牌编码、逢人配与动作合法性

```text
code = (copy << 8) | (suit << 4) | rank
copy: 0 或 1
suit: 1=黑桃，2=红桃，3=梅花，4=方块，0=王
普通牌 rank: 1=A，2..10，11=J，12=Q，13=K
王 rank: 14=小王，15=大王；suit必须0
```

示例：红桃2为34，另一副红桃2为290；小王为14/270，大王为15/271。两个同花色同点数实体仍是不同牌。整数码大小不代表游戏牌力。

当前规则下，**红桃级牌才是逢人配，大小王不是通用逢人配**。canonical 保存真实实体码和协议牌型，不永久写死 `is_wildcard` 标志；使用时根据 level 与规则计算。当前派生器才写解析后的 `claim_ranks_zero_based`，不能把它和普通 rank 1..13混用。

`accepted_candidate` 只证明来源/转移/结算链路通过检查，不等于全牌型语义已经唯一解析。`validate_decision` 是结构与时序验证器，不是替代完整规则引擎。合法性和配牌语义还需通过指定 FableDan 适配器检查；可能被拒绝。

## 6. quality、outcome、缺失值

`quality = {status, reasons: string[], lost: boolean}`：

- `accepted_candidate`：有可信前置状态、确认动作、连贯更新及合法结算摘要；reasons空、lost=false。仍需训练适配和独立评测。
- `partial`：此前确认的输入/动作可保留，但没有可信结算；outcome/reward为null。原因通常 `missing_settlement` 或 `ended_without_settlement`。
- `quarantine`：状态冲突/掉数据/边界异常/写入链路失败；不自动送入训练。outcome清空，保留拒绝原因。

典型拒绝码包括 `queue_overflow`、`writer_error`、`invalid_update`、`state_error`、`capture_error`、`external_loss`、`missing_deal`、`state_version_gap`、`state_version_conflict`、`history_gap`、`history_conflict`、`ambiguous_action_link`、`identity_conflict`、`invalid_settlement`、`settlement_conflict`、`decision_buffer_limit`。代码中的枚举和转换错误码是权威，不输出底层异常原文。

`outcome` 非空时仅含：

```json
{"phase":"settled","finish_ranks_by_seat":[2,3,1,4]}
```

这是**合成格式示例**：数组索引是绝对座位，值是名次1..4，各出现一次。不要写入结算对手手牌。未取得真实结果时 null，不能填 `[0,0,0,0]` 或虚构 win。

null、字段缺失、空集合是不同含义：未记录合法动作是 null，确认过牌 cards是[]，没有相应时点的建议不是0分，未确认座位是-1，未知结果是null。训练 mask 根据具体任务和可用标签生成，不是看到 accepted 就所有 loss 都开启。

## 7. 逢人配、双副牌、动作身份与规则版本

项目实体牌码来自 `guance.domain.cards`，不是 FableDan 内部 0..107 编号。转换前后不得丢掉 copy。红桃（suit=2）级牌在当前规则中才可能是逢人配；大小王是实体牌，不自动当逢人配。规则/适配器必须根据本局 level 和 `rule_profile` 重新判定。

`action_signature` 只稳定标识已观察的物理动作和协议动作类型；它不代表动作牌型已经被 FableDan 唯一解析。`combo_type` 的数值属于观察协议，不能直接当成 FableDan 的 `Move.type`。如果实际牌型/逢人配分配存在歧义，canonical 仍可留在 quarantine 或 accepted_candidate，但 `derive_fabledan` 必须拒绝该样本，不能补零特征。

## 8. 当前 FableDan 派生视图

入口：

```powershell
python -m guance.infrastructure.training_data derive <decision.jsonl> --output <derived.jsonl>
```

派生器不加载 `best.npz`，也不启动 Qt/采集。它使用固定 revision 的纯编码/牌型适配：

- `encoder.revision`：当前 vendored FableDan revision；
- `vocab_size=48`；
- `feature_dim=80`；
- `max_tokens=512`；
- `inputs.tokens`：历史编码；
- `inputs.chosen_action_features`：实际动作的 80 维特征；
- `action.exact_generated_index`：如果当前动作生成器能唯一找到相同实体动作，则写索引，否则为 null 并加 warning；
- `targets.terminal_return`：只由 verified rank 结果按明确 reward revision 离线计算；canonical `reward` 永远为 null；
- `targets.masks.belief_oracle=false`：真实局没有对手隐藏手牌标签。

派生视图不是 canonical 真源。网络增层、词表变化、特征维度变化、policy/value head 增加，都应从 semantic decision 重新生成新的 adapter 输出。已有 Q 分数不是真值标签；实际动作也只是行为标签，不能自动称为最优动作。

当前固定 reward 只是一个显式派生示例，不是模型质量证明：首名及其队友按 pinned engine 的团队结果规则映射，再除以3；未知/partial/quarantine 保持 mask=false。若训练工程采用不同奖励，应使用新的 `reward_revision`，不得覆盖 canonical outcome。

## 9. 旧对局转换

入口（在仓库根目录执行；`<...>` 为需替换的占位符）：

```powershell
python -m guance.infrastructure.training_data convert debug/captures/<session>/frames.jsonl --output-root <另一个本地目录>
```

本版有意只接受项目自己的 `frames.jsonl` 元数据格式：

- `direction` 必须严格是 `server` 或 `client`；缺失/其他值拒绝，不猜方向；
- `client` 行忽略；非目标 host 忽略；
- `payload_b64` 必须严格 Base64，长度和 SHA-256 必须匹配；
- 保持输入文件顺序，不按混合连接的全局 sequence 重新排序；
- 使用现有 `SessionEngine` 和相同状态验证；不得调用 reset 绕过暂停；
- source identity 使用输入文件 SHA-256，重复转换得到相同 episode ID；
- 原文件只读，不修改、不重写；报告只包含哈希、计数、有限原因码。

`.mitm` 当前不进入训练数据转换入口。现有 `.mitm` reader 的方向/丢弃标志契约比训练导入所需的严格白名单宽松；宁可明确报告 `unsupported_input_format`，也不把不确定的 frame 当成可信 server 数据。未来若支持 `.mitm`，必须先增加严格适配器和独立测试。

截至 2026-10-02 的本机旧录制审计：

- 9 个顶层 `frames.jsonl` 会话；
- 1 个会话因缺少 direction 在第278行拒绝；
- 8 个方向明确的会话安全重建；
- 160 个本人决策前状态—确认动作配对；
- 8 个已验证 settlement；
- canonical 字段与独立 SessionEngine 重建 160/160 一致；
- 当前 FableDan 派生 156 条成功，4 条因既有公开动作牌型/配牌语义歧义拒绝；另有7条无唯一实体动作索引，保留 warning，不强行转成分类 index。

这些数量是本机验证证据，不代表其他历史文件都能转换，也不代表训练后模型会提升。

## 10. 失败、重试与旧数据更正

- 写入失败不重试当前 live producer；它只把当前 episode 标记为 `writer_error`，避免在实时路径中等待磁盘。
- 旧数据转换可以由用户显式重新运行。相同 source SHA 不产生第二个 episode；输出采用原子替换。
- 同一 episode 的 settlement 后失效会生成同一 episode ID 的 quarantine 修订，禁止旧 accepted 批次覆盖新隔离批次。
- 数据修正不是静默修改：manifest 的同一 episode 条目更新为最新状态，旧运行 manifest 仍能审计；训练工程以最新且完整的 manifest 为准。
- 原始输入损坏、方向不明、源文件在读取中变化、状态机暂停或消息缺口都必须报告原因；不能仅靠输出文件存在判断成功。

## 11. 给训练工程的最小规则

1. 只读取 `status=accepted_candidate` 且 `quality.lost=false` 的样本。
2. 以 `episode_id` 分组切分 train/validation/test，不能按 decision 行随机切分。
3. 用 `pre_state` 生成模型输入；禁止把 `post_state`、outcome、未来 history 或 settlement 名次回填到输入。
4. 用 `observed_action` 作为行为标签时标明 `label_source=confirmed_behavior`，不要标成 oracle。
5. `derive_fabledan` 失败的样本进入 unsupported report，不补零、不猜牌型。
6. 对 belief/hidden-hand 目标使用 mask=false；真实数据没有该标签。
7. 记录 dataset hash、schema、rule、encoder、reward 和 model revisions；训练 loss 下降不等于模型提升。
8. 新网络只应依赖 semantic 层可提供的字段。需要新字段时新增 schema/adapter 或明确缺失 mask，不伪造历史信息。

## 12. 线程、隐私和验收入口

实现入口：

- `src/guance/infrastructure/training_data/schema.py`：安全快照和 canonical JSON；
- `collector.py`：单线程语义关联/质量状态；
- `recorder.py`：有界队列、daemon writer、原子 episode/manifest；
- `legacy.py`：离线 frames.jsonl 转换；
- `derive.py`：固定 revision 的 FableDan 派生视图；
- `src/guance/application/controller.py`：默认开启、状态订阅、错误失效、停止时非阻塞关闭；
- `tests/training_data/` 与 `tests/integration/test_training_data_controller.py`：合成、隐私、并发、legacy、当前编码器对照。

本任务验收记录：`docs/TRAINING_DATA_ACCEPTANCE.md`。执行过的重点证据必须同时报告 passed、failed、skipped、unverified。当前已有基线中 `test_floating_layout_is_persisted_and_restored` 的 `sizeMode=manual` 差异是既有失败，不得归因于本功能或通过削弱测试隐藏。
