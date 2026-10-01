# 跨层集成契约

## 版本和原则

`SCHEMA_VERSION = 1`。源码基线为 `957b9c3`；W 表示本地待合并扩展。跨层数据使用不可变 dataclass 或受限字典；字段增加必须兼容旧消费者，语义变化必须增加版本或明确迁移。`from_dict()` 的类型转换不等于完整 schema 验证，调用者仍需检查关键字段。

## Core

### SourceFrame

```text
host: str
payload: bytes
received_at: float
connection_id: str
sequence: int
direction: client | server | unknown  # W only, default unknown
```

`sequence` 是本地接收序号，不是服务端 action ID。`payload` 只在 protocol/infrastructure 中存在，不能写入 UI 或诊断。

### GameObservation

```text
session_id, game_id, state_version: identity
captured_at: float
level: 1..13
my_seat, first_seat, current_seat: 0..3
hand: tuple[int, ...]
remaining: tuple[int, int, int, int]
history: tuple[PublicAction, ...]
lead_action: PublicAction | None
free_lead: bool
trick: int
```

`key` 为 `(session_id, game_id, state_version)`。模型和控制器必须使用该 key 丢弃旧结果。

### PublicAction

包含 `index`、`player`、`kind`、`cards`、`combo_type`、`next_player`、`trick`。它只表示确认过的公开动作，不表示隐藏手牌。

### AdviceResult

包含候选、耗时、状态、错误码、模型指纹以及同一组状态 key。错误推荐也必须回带 key，便于诊断时确认错误属于哪个局面。

## SessionEngine

```text
feed(frame: SourceFrame) -> list[EngineUpdate]
reset(reason: str = "") -> None
latest_observation: GameObservation | None
```

基线 `feed()` 返回 `state`、`observation` 或 `error`；W 另增加结束通知和连续牌局相关处理。`state_version` 随确认的状态变化递增，不只在本人回合递增。基线 state.data 是状态摘要，不能假设存在 W 中的完整 `table` 对象。

## ViewModel

Controller 可以调用：

- `apply_status(dict)`；
- `apply_observation(dict)`；
- `apply_advice(dict)`；
- `append_log(dict)`；
- `clear_advice(reason)`。

| UI 意图 | 信号参数 | 消费者责任 |
|---|---|---|
| 开始/停止 | `startRequested()` / `stopRequested()` | 控制器启动/清理 |
| 证书授权/移除 | `authorizeRequested(bool)` / `removeCertificateRequested(bool)` | 校验同意和运行状态 |
| 修复网络 | `repairRequested()` | runtime 恢复结果反馈 |
| 模型导入 | `modelImportRequested(str)` | 本地路径校验、停止旧 worker |
| 诊断导出 | `exportDiagnosticsRequested()` | 导出有限、脱敏结构化记录 |
| 演示/浮窗 | `demoRequested()` / `floatingChanged(bool)` | 展示状态与用户偏好同步 |

W 的 `apply_table_state()`、`confirmSeatRequested(str)`、`floatingLayoutSaveRequested(dict)` 与公开计数属性不属于本次源码提交。信号发出只表示意图，必须验证实际接线，不能当成操作成功回执。

ViewModel 只发出意图信号，例如 `startRequested`、`stopRequested`、`authorizeRequested(bool)`、`modelImportRequested(str)`。UI 不能直接调用网络、代理、证书或模型 API。

## Runtime

`RuntimeService` 提供 inspect、prepare_certificate、start、stop、repair、remove_certificate 和 close。`consent=False` 必须是 no-op。helper 未 ready 时不得修改代理；stop 必须先恢复代理。

| 方法 | 主要参数/返回 | 边界 |
|---|---|---|
| 构造 | `paths, on_frame=None, on_status=None, options=...` | ports 可注入 fake，便于无系统副作用测试 |
| `inspect()` | 状态 dict | 读取诊断，不等于已启动 |
| `prepare_certificate(consent, owner_hwnd=0)` | 结果 dict | 证书动作需明确同意 |
| `start()` / `stop()` | 结果 dict | ready 后接管；恢复优先于 helper 停止 |
| `repair()` | 结果 dict | 必须检查 ok 和错误，不能盲目覆盖其他代理 |
| `remove_certificate(consent, owner_hwnd=0)` | 结果 dict | 运行中拒绝移除 |
| `close()` | None | 清理入口，仍需验证组合层确实调用 |

`RuntimeOptions` 的 direct 模式不接受 upstream，upstream_http/upstream_mixed 要求匹配的 UpstreamProxy。构造层不接受未经解析的 auto 模式，应用层必须先做选择。默认监听 `127.0.0.1:18080`、frame 上限 1,048,576 bytes、启动/停止超时 8/3 秒；配置可改变这些值。回调可能来自后台线程，UI 应通过 ViewModel 的 queued 信号交接。

## Model IPC

worker 首先发送：

```json
{"type":"loaded","metadata":{}}
```

请求为：

```json
{"type":"recommend","request_id":"example-request","observation":{}}
```

上面是合法 JSON 的 envelope 示例，空 observation 不是可执行推理请求；真实内容必须包含前述完整 GameObservation 字段。

worker 的消息类别包括 `loaded`、`load_error`、`result`、`request_error`、`protocol_error`，并接受 `stop` 请求。行长度限制为代码中的 `2*1024*1024`；因使用文本流读取，应理解为读取字符上限，不能当作任意 Unicode 序列都严格等于 2 MiB 的字节限制。

客户端默认候选请求超时 2 秒、启动等待 12 秒，待请求队列容量 1、响应队列容量 16；数值线程约束为 1..4。每条结果核对 request_id 和局面 key，过期结果不送 UI。stdout 是协议通道，不得混写调试日志。

## 安全契约

跨层禁止传递 Cookie、Authorization、私钥、原始抓包、原始 payload 和对手隐藏手牌。若新接口必须携带敏感材料，应先重新设计边界并增加专门的安全审查。
