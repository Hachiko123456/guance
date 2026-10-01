# 架构说明

版本：2026-10-01。本文描述当前提交基线的边界；工作区中尚未合并的修正会明确标注，不把它们当作已发布能力。

## 目标和约束

Guance 的核心流程是：在用户授权后取得公开二进制消息，解析成标准化事件，使用状态机确认牌局，再把公开局面送入本地模型。应用只给建议，不替用户点击或出牌。

必须保持：

1. 领域层不依赖 Qt、Windows、采集运行时或 NumPy；
2. UI 不解析原始报文、不写代理、不加载权重；
3. 采集器不决定牌局语义；
4. 未知、冲突或过期数据不继续推荐；
5. 对手隐藏手牌不进入实时 observation、模型或诊断；
6. 证书和代理操作必须有用户 consent，并能恢复原状态。

## 分层

```text
Presentation
  PySide6 / QML / ViewModel
        ↓ Qt signals/properties
Application
  Controller / Session orchestration / lifecycle
        ↓ contracts and ports
Domain
  cards / events / GameState / SessionEngine
        ↑ adapters
Infrastructure
  protocol / saved capture / runtime / Windows / model
```

### Domain

`domain/cards.py` 定义实体牌编码、牌面拆分和完整牌组；`domain/events.py` 定义不可变事件；`domain/state.py` 校验行动者、牌、余牌、轮次、结束顺序；`domain/session.py` 连接事件、状态和 observation。

Domain 不知道消息来自 WebSocket、文件还是测试 fixture，也不知道 UI 如何显示牌。

### Infrastructure

- `protocol/wire.py`：受限 Protobuf wire 和 tnetstring 原语；
- `protocol/live.py`：实时外层聚合、command/variant 和事件解析；
- `protocol/saved_capture.py`：离线读取服务器 WebSocket frame；
- `protocol/replay.py`：历史回放 body，当前存在接口待修复项；
- `capture/`：采集 helper 和有界 frame 输出；
- `windows/`：证书和系统代理适配；
- `model/`：本地 NumPy 模型、worker 和 JSON Lines 客户端。

协议层是纯解析器，不能以解析成功代替状态合法。模型层不能以模型分数代替牌局事实。

### Application

Controller 连接 ViewModel 信号、SessionEngine、RuntimeService、InferenceClient 和 DiagnosticLog。它负责启动、停止、异常恢复、状态版本绑定和清理顺序，但不在 QML 中实现这些规则。

### Presentation

`GuanceViewModel` 对外暴露已经复制和标准化的字典/属性；QML 只消费这些属性。后台线程通过 queued signal 进入 Qt 线程，不直接操作控件。

## 生命周期

```text
idle
  → preparing
  → starting
  → waiting
  → connected
  → confirming_seat / ready / thinking
  → ended / settled
  ↘ error / stopping / stopped
```

以上是展示层状态概览，不是所有状态都必然逐个出现的领域状态图。启动约束是：检查配置和证书 → 获得明确同意 → 准备 helper → helper ready → 接管代理 → 等待新牌局。停止约束是：恢复原代理应先于停止 helper，终止推理请求并清理 worker。异常中断和退出接线仍需实机验收，不能仅凭方法存在就宣称安全恢复成功。

## 进程与 IPC

主进程承载 Qt、Controller、ViewModel 和会话；模型 worker 是持久 Python 子进程，通过 stdin/stdout JSON Lines 通信。请求队列容量为 1，只保留最新 observation；结果必须回带 `session_id/game_id/state_version/request_id`，旧结果丢弃。

采集 runtime 是外部 helper，输出受限的服务器 frame。主进程不能把原始 payload 直接传给 QML。IPC 单行大小和日志大小均有上限。

## 数据边界

```text
SourceFrame -> ProtocolEvent -> GameState -> GameObservation -> AdviceResult
```

- `SourceFrame`：传输元数据和 bytes；
- `ProtocolEvent`：Deal/Start/Action/Settlement/Informational；
- `GameState`：可验证牌局内部状态；
- `GameObservation`：本人手牌 + 公开动作 + 当前局面；
- `AdviceResult`：候选、状态版本和模型指纹。

结算手牌只用于离线一致性检查，不能穿过 `GameObservation`。

## 基线与待合并实现

| 边界 | 基线 `957b9c3` | 本地待合并修正 |
|---|---|---|
| 本人座位 | 直接读取发牌字段，存在解释风险 | 未确认座位、牌引用证据与人工回退 |
| 公开桌面快照 | state 摘要与 observation | 更完整桌面快照、记牌器、结束名次 |
| 输入元数据 | SourceFrame 无 direction，SourceMeta 无 message_index | 增加方向/索引并统一消费者 |
| 退出与崩溃恢复 | 需核对 bootstrap 与 runtime 清理接线 | 单实例、持久租约、启动恢复、aboutToQuit 清理 |
| 用户证书路径 | 用户数据目录下 certs | 与 helper confdir 统一 |
| 浮窗布局 | 原有浮窗 | 两列出牌、固定记牌器、自动尺寸；仍未完成验收 |

本次文档/Git 整理不提交这些业务修正。不应仅凭文档说明在基线部署真实采集。

## 构建边界

PyInstaller 通过 [Guance.spec](../Guance.spec) 收集 QML、必要 Qt 插件、模型和采集 runtime。源码包、模型资产、用户配置和每台机器的证书状态是不同生命周期，不能只复制 exe 代替完整 onedir 目录。

## 相关入口

- [协议字段字典](PROTOCOL.md)
- [跨层契约](INTEGRATION_CONTRACT.md)
- [构建](BUILD.md)
- [安全](SECURITY.md)
- [验收标准](ACCEPTANCE_STANDARD.md)
