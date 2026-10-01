# 协议结构与字段字典

更新：2026-10-01。业务源码基线：`957b9c3`。本文来自解析器与合成测试，不是官方 `.proto` 或完整服务端协议。

## 1. 证据与版本

本文使用三种标记：

- **B（基线）**：已提交源码中的读取方式，可以通过对应函数核对；不等于真实环境已全面验证。
- **W（待合并）**：本地研究或实现修正，本次只发布说明，不提交对应业务代码。
- **U（未知）**：未解码或业务含义不确定，不应据此更新状态或推断隐藏信息。

为了保留兼容性，源码中的内部连接白名单和 profile 标识不在此次文档整理中迁移。本文只使用“对局服务”“历史服务”等中性名称，不发布具体服务地址和真实 payload。

## 2. 输入与信任边界

实时输入为 `SourceFrame` 的二进制 `payload`。连接筛选、方向筛选属于采集/会话边界；`parse_aggregate(raw)` 本身只接收字节，不验证用户授权、不连接网络。

大厅或其他连接不能仅凭一个相同 command 就当成牌局来源。已保存文件读取也是离线操作，不会向服务器重发消息。

## 3. 二进制外层

所有位置采用从零开始、左闭右开的字节范围。外层和记录头的整数使用 **big-endian**。

### 3.1 消息与聚合头

| 位置 | 长度 | 源码行为 | 等级 |
|---|---:|---|---|
| `raw[0:2]` | 2 | 整个消息长度，必须等于 `len(raw)` | B |
| `raw[2:8]` | 6 | 未在此解析器中解释 | U |
| `raw[8:10]` | 2 | 标记 `FF FD`；不匹配则返回无目标聚合 | B |
| 聚合前其他字节 | 可变 | 不赋予业务意义 | U |
| `raw[a:a+2]` | 2 | 聚合长度，必须等于 `len(raw)-a` | B |
| `raw[a+2:a+4]` | 2 | 子记录数，范围 `1..128` | B |

候选聚合起点来自 `range(24, min(512, len(raw)-3))`，所以 512 不包含在内。各记录必须首尾连续，最后一条必须恰好结束于消息末尾。只有一个合法候选时接受；多个候选报歧义错误；零个候选返回空事件。

### 3.2 子记录

设记录起点为 `p`，`size = uint16_be(raw[p:p+2])`，结束位置 `end = p + 2 + size`。

| 相对偏移 | 长度 | 名称/含义 | 约束 |
|---|---:|---|---|
| `[0:2]` | 2 | `size`，不含该长度字段自身 | `size >= 16`，`end <= len(raw)` |
| `[2:4]` | 2 | 记录类型 | `15815` 为当前目标类型 |
| `[4:6]` | 2 | 版本 | 目标类型要求 `2` |
| `[6:14]` | 8 | 未解释结构区 | U，不当作玩家或业务 ID |
| `[14:18]` | 4 | payload 长度声明 `declared` | 必须等于 `len(payload)+2`，不是简单等于 payload 长度 |
| `[18:end-p]` | 可变 | payload | 进入 wire reader |

非目标记录类型被跳过；目标类型版本不支持则拒绝。以上结构常量来自 `bundle_at()`/`extract_bundle()`/`parse_aggregate()`。

## 4. Protobuf wire 原语

`wire.fields()` 返回 `dict[int, list[tuple[wire_type, value]]]`，保留重复字段和出现次序，不在通用层假定每个字段只能出现一次。

```text
tag = varint()
field_number = tag >> 3
wire_type = tag & 7
```

| wire type | 值 | reader 结果 |
|---:|---|---|
| 0 | varint，最多 10 字节，检查 64 位溢出 | `int` |
| 1 | fixed64，固定 8 字节 | `bytes`，由使用者决定是否解释成 double |
| 2 | 长度 varint 后跟指定长度内容 | `bytes`，可能是 message、packed 或原始字节 |
| 5 | fixed32，固定 4 字节 | `bytes` |
| 其他 | 未实现 | `ValueError` |

字段号必须为 `1..536870911`。reader 校验截断与越界；不支持所有 Protobuf 类型语义，也不会自动做 ZigZag、有符号整数、字符串或枚举转换。

- `scalar(group,n)`：缺失默认 0；存在时要求唯一且 wire=0。
- `blobs(group,n)`：允许重复，要求每项 wire=2。
- `one_blob(group,n)`：要求恰好一个 wire=2 字段。
- `packed_field(group,n)`：缺失为空列表；存在时解码一个 bytes 中的连续 varint，不支持同字段分成多个 packed 块。
- W 的 `optional_scalar()` 返回 `(present,value)`，用来区分“字段缺失”与“显式发送 0”；不是所有 scalar 都改用了 presence 语义。

未知 body 字段通常由业务读取函数忽略；**未知 command 会报错**。两种情况不能混为一谈。

## 5. Envelope 与命令映射

```text
payload.field 1              command，wire=0
payload.field 3              wrapper，wire=2，恰好一个
wrapper.<expected variant>   body，wire=2，恰好一个
```

wrapper 的字段号集合必须恰好等于 `{expected_variant}`；其他额外 variant 也会导致拒绝。

| command | variant | B 行为 | W 的变化 |
|---:|---:|---|---|
| 14 | 22 | 信息事件 | 无 |
| 112 | 21 | 信息事件 | 无 |
| 3000 | 1 | 信息事件 | 无 |
| 3001 | 2 | 发牌事件 | 修正首家提示，不直接确定本人座位 |
| 3002 | 3 | 信息事件 | 不推断未知业务含义 |
| 3003 | 4 | 信息事件 | 读取中性数值证据 |
| 3004 | 5 | 首出事件 | 保留字段存在性 |
| 3005 | 6 | 出牌事件 | 读取可选结束名次 |
| 3006 | 7 | 过牌事件 | 无 |
| 3007 | 8 | 未识别，会拒绝 | 接受为信息事件 |
| 3008 | 9 | 结算事件 | 完成名次/生命周期相关修正 |
| 3009 | 10 | 信息事件 | 无 |
| 3010 | 11 | 信息事件 | 会话层仍需单独处理重连风险 |
| 3011 | 12 | 信息事件 | 无 |
| 3019 | 26 | 信息事件 | 有合适牌引用时生成座位候选事件 |

“信息事件”只是 parser 的输出类别，不等于状态层保证永远忽略它，例如会话可能将连接变化当作需要暂停的条件。

## 6. 实时 body 字段字典

### 6.1 3001：发牌

| 路径 | wire | 意义和源码行为 | 缺失/校验 | 等级 |
|---|---:|---|---|---|
| `1` | 0 | 初始手牌数量 | 默认 0；必须与牌列表长度都等于 27 | B |
| `2` | 2 | packed 实体牌码 | 缺失为空，因此不满足 27 张约束 | B |
| `3` | 0 | 首家提示，不应直接等同本人座位 | B 直接赋给 `my_seat`；W 作为可选提示 | W 修正 |
| `4` | 0 | 级牌 | parser 默认 0，合法范围交给状态层 | B |
| `6` | 2 | 辅助 nested message | 缺失允许；错误结构拒绝 | W |
| `6.3` | 0 | 另一份首家提示 | 与顶层 `3` 同时出现且冲突时拒绝 | W |

`3` 是 scalar，`6.3` 才是嵌套路径。W 创建 `DealEvent(my_seat=-1, ...)`，保留提示与 presence；绝不靠该字段单独断言本人身份。B 的旧行为是已知限制，不应作为线上可靠映射引用。

### 6.2 3004：首出

| 路径 | wire | 意义 | 处理 |
|---|---:|---|---|
| `1` | 0 | 首家绝对座位 | B 缺失默认 0；W 同时返回 `first_seat_present=False` |

状态层再检查座位与其他证据。值为 0 与字段存在并不是同一件事；W 并非在所有场景都因为 presence=false 拒绝。

### 6.3 3005：出牌

| 路径 | wire | 项目名称 | 缺失/约束 | 等级 |
|---|---:|---|---|---|
| `1` | 0 | `finish_rank` | B 不读取；W 缺失为 `None`，存在值由领域层验证 | W |
| `2` | 0 | `player` | 默认 0，状态层验证是否当前行动者 | B |
| `3` | 0 | `next_player` | `18446744073709551615` 即 `2^64-1` 转为 `None`；缺失是 0，不是 None | B |
| `4` | 0 | `combo_type` | 默认 0；领域要求正值，模型层解析牌型语义 | B |
| `5` | 0 | `remaining_after` | 默认 0；必须与状态扣牌后的数量一致 | B |
| `6` | 0 | 本次牌张数 | 必须等于 `len(field 7)` 且非零 | B |
| `7` | 2 | packed `cards` | 不去重、不截断；实体合法性与重复牌由状态层校验 | B |

结束名次在 W 中仅允许有效结束动作赋值；不能因为剩余张数为 0 就在 UI 猜一个名次。

### 6.4 3006：过牌

| 路径 | wire | 项目名称 | 约束 |
|---|---:|---|---|
| `2` | 0 | `player` | 默认 0，状态层验证行动者 |
| `3` | 0 | `next_player` | sentinel 与 3005 相同 |
| `4` | 0 | `reset_flag` | 默认 0，状态层结合已观察轮转规则处理 |

当前观察的过牌没有实体牌列表，标准输出 `cards=()`、`remaining_after=None`、`combo_type=None`。parser 不会把额外未知 body 字段自动当成出牌，也没有“拒绝所有额外字段”的通用保证。

### 6.5 3008：结算

| 路径 | wire | 意义 | 约束 |
|---|---:|---|---|
| `3` | 2 | packed `ranks_by_seat` | 状态层检查名次排列 |
| `13` | 2，repeated | 按消息顺序组成各家剩余手牌对象 | parser 接受 0 或 4 个对象 |
| `13[].1` | 0 | 对象内牌数 | 必须等于本对象 `2` 的解码长度 |
| `13[].2` | 2 | packed 实体牌码 | 仅用于一致性验证，不输出到公开观察 |

**parser 与 domain 的限制不同：** parser 接受没有 `13` 的结构，不代表领域结算可以成功；当前领域完整验证要求四家的材料。验证只临时使用，不能进入 UI、日志或模型。

### 6.6 3019：座位辅助证据（W）

| 路径 | wire | 意义/读取方式 |
|---|---:|---|
| `2` | 0，可选 | 候选座位；缺失输出 0 并标记 presence=false |
| `3` | 2 | nested message，逐项遍历其所有字段值 |
| `3.<任一字段>[]` | 2 | 必须是 nested entry |
| entry.`2` | 2 | packed 牌引用，存在则收集 |
| entry.`1` | 未作为数量约束读取 | 某些合成 fixture 带数量，但当前函数未验证它；不能写成已校验 |

没有牌引用、结构错误或空结果时退回 `InformationalEvent`。有牌引用才生成候选；领域层需要与初始本人手牌严格匹配并检查座位等条件。连接/序号保护只有在对应元数据均非空/非零时才执行，不能说成无条件跨连接防护。B 只把该 command 当信息事件。

### 6.7 3003：中性证据（W）

| 路径 | wire/长度 | 保留值 | 未确认部分 |
|---|---|---|---|
| `4` | 2，恰好 4 原始字节 | 四个 byte 的 tuple | 不自动解释为四人的最终倍率 |
| `5` | 2，nested message | 见下方 | 不自动解释为加倍成功 |
| `5.1`、`5.2` | 1，各 8 字节 | `struct.unpack('<d', ...)` 的有限 double | 两个数值的业务标签未知 |
| `5.4` | 0，可选且至多一次 | 整数 flag | 枚举意义未知 |

重复、截断、错误 wire type 或非有限 double 等情况会放弃对应可选证据，返回 `None`，不是让所有信息消息导致整局失败。B 不保留这些值。

## 7. 牌码与模型编号

```text
rank = code & 0x0F
suit = (code >> 4) & 0x0F
copy = code >> 8
```

普通牌 suit 为 1/2/3/4，rank 为 1..13；王的 suit=0、rank=14/15；copy 为 0/1。无效组合和负值拒绝。两张牌面相同但 copy 不同的牌是不同实体牌。

| 牌 | code |
|---|---|
| 第一副黑桃 2 | `0x12 = 18` |
| 第二副黑桃 2 | `0x112 = 274` |
| 第一副小王 | `0x0E = 14` |
| 第二副大王 | `0x10F = 271` |

模型普通牌编号为 `(rank-1)*4 + suit_map[suit] + 54*copy`，`suit_map={1:2,2:0,3:3,4:1}`；王为 `52 + rank-14 + 54*copy`。两种编号不可直接混用。

## 8. 合成示例

下列是仅含公开信息的人工示例，不是网络请求，也不是抓包摘录。表示座位 0 打出一张第一副黑桃 2，下一位为 3，剩余 26 张。

```text
command: 3005, variant: 6
body:
  field 2 / wire 0 -> 0
  field 3 / wire 0 -> 3
  field 4 / wire 0 -> 1
  field 5 / wire 0 -> 26
  field 6 / wire 0 -> 1
  field 7 / wire 2 -> packed [18]
body bytes (hex): 10 00 18 03 20 01 28 1A 30 01 3A 01 12
```

它会形成 `ActionEvent(player=0, kind='play', cards=(18,), next_player=3, combo_type=1, remaining_after=26)`。是否允许该动作仍取决于当前 `GameState`，解码成功不是业务合法证明。

## 9. 保存的 WebSocket 抓包

保存文件使用 tnetstring：`ASCII长度:body + type-tag`。支持 bytes `,`、UTF-8 string `;`、integer `#`、float `^`、boolean `!`、null `~`、list `]`、dictionary `}`。长度最多 64 MiB，递归深度小于 64，字典键值必须成对。

读取路径：flow dict → request/host 白名单 → websocket/messages。每个 message 至少六项：

| 索引 | 名称 | 筛选 |
|---:|---|---|
| 0 | opcode | 只接受 `2`（binary） |
| 1 | from_client | 必须 false |
| 2 | body | 必须 bytes |
| 3 | timestamp | 转为 float |
| 4 | dropped | 必须 false |
| 5 | injected | 必须 false |

读取器用本地递增 sequence，不是服务端动作 ID。W 增加 `SourceFrame.direction='server'` 与 `SourceMeta.message_index` 等接口修正；B 的 `iter_events()` 已使用后者但 dataclass 尚无该字段，存在接口不一致，需独立修复。

## 10. 历史 HTTP body 格式与限制

`extract_replay_bodies()` 对 `.mitm` 读取 request/response，匹配内部历史服务及路径前缀，要求 HTTP 200。非 `.mitm` 文件直接按 response body bytes 读取。支持 identity、gzip、deflate，解压限制 64 MiB 并要求 decoder 到达 EOF。

| 路径 | 读取方式/含义 |
|---|---|
| top.`1` | 单个 metadata message |
| metadata.`2[]` | teams message，当前读取但未驱动后续状态 |
| top.`2[]` | frame message |
| frame.`1[]` | event envelope，要求仅一个 variant 字段 |
| event.`11` | 初始手牌 payload |
| initial.`1[]` | hand message；内部 `1` 为座位，`2[]` 为 card message |
| card.`1/2/3` | suit/rank/copy scalar |
| event.`16` | 当前跳过 |
| event.`17` | action payload |
| action.`1/3/5` | player/trick/next seat scalar |
| action.`11` | play message；内部 `1` 牌型，`2[]` card message |
| action.`12` | pass 标志存在性，与 `11` 必须二选一 |
| event.`18` | settlement payload；当前读取其 `3` scalar |

**已知实现缺口（B 与当前工作区均存在）：** `iter_replay_events()` 使用不被 `ActionEvent` 接受的 `source_index`/`trick` 关键字；结算创建缺少 `remaining_hands`；收集初始手牌但未产出 `DealEvent`；`observer_seat` 参数未使用。因此这段格式说明是解析研究参考，不宣称端到端历史恢复可用，也不提供“已通过 106 动作”等不可复核的发布保证。

## 11. 校验分层与测试

| 层 | 负责 | 不能替代 |
|---|---|---|
| wire/aggregate | 字节边界、wire、版本、command/variant | 用户授权、牌型合法性 |
| domain | 牌码、行动者、公开重复牌、余牌、顺序、结算一致性 | 模型对配牌语义的唯一解释 |
| model adapter | 根据牌型、配牌和历史约束生成唯一解释与合法候选 | 补齐丢失的实时消息 |

对应源码：[wire](../src/guance/infrastructure/protocol/wire.py)、[live](../src/guance/infrastructure/protocol/live.py)、[saved_capture](../src/guance/infrastructure/protocol/saved_capture.py)、[replay](../src/guance/infrastructure/protocol/replay.py)、[events](../src/guance/domain/events.py)、[state](../src/guance/domain/state.py)。

基线包含 [实时合成测试](../tests/protocol/test_live_protocol.py)；W 的新增字段、presence 和 saved-reader 回归仍需随修正单独提交。建议命令：`python -m pytest tests/protocol tests/domain -q`。真实包兼容、隐私、安全和跨版本行为仍需针对明确版本补充证据；本次没有读取或发布真实抓包。
