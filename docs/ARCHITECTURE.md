# 掼策架构说明

版本：0.1.0（2026-09-28）

## 目标

掼策是 Windows 10/11 上运行的本地桌面助手：在用户授权自己的微信/大掼蛋网络环境后，读取对局公开消息，重建可验证牌局状态，用本地 FableDan 模型生成推荐。掼策不点击游戏、不自动出牌、不上传原始对局。

## 设计原则

1. 核心状态机不依赖 Qt、Windows、mitmproxy 或 NumPy。
2. 采集、解码、状态、推理、界面各自有明确边界。
3. 任何缺包、乱序、未知命令或不一致都暂停推荐，不猜测。
4. 推荐结果必须绑定 `session_id + game_id + state_version`；迟到结果直接丢弃。
5. 只将本人手牌和此前公开信息传入实时推荐；对手隐藏手牌只用于离线 QA。
6. 技术日志脱敏、限量和可导出；原始抓包不默认持久化。
7. Windows 网络代理和证书变更必须有明确同意、备份和恢复。

## 分层

```text
Presentation (PySide6/QML)
        ↓ ViewModel signals/properties
Application (session orchestration, model client, lifecycle)
        ↓ ports/contracts
Domain (cards, events, GameState, SessionEngine)
        ↑ adapters
Infrastructure (live protocol, saved capture, mitmdump, Windows proxy/cert, NumPy FableDan)
```

Domain 不导入 PySide6、win32、mitmproxy、numpy。Presentation 不解析协议、不改代理、不加载权重。

## 进程

- 主进程：窗口、ViewModel、会话编排、诊断。
- 采集子进程：内置 mitmdump 和 addon，只输出有界的服务器 WebSocket frame。
- 推理子进程：加载 best.npz，使用 JSON Lines 本机管道，限制线程和队列为 latest-only。

## 有代理/无代理

无现有代理：`微信 → 掼策采集器 → 互联网`，采集器使用 regular 模式。

有显式 HTTP/mixed 代理：`微信 → 掼策采集器 → 原代理 → 互联网`，采集器使用 upstream 模式。

PAC、纯 SOCKS、TUN/VPN 等环境在 V1 只做检测和诊断，不擅自覆盖网络路径。

## 模型

模型配置默认位于：

```text
data/profiles/tencent_daguandan/models/best.npz
```

开发机可从用户提供的本地路径复制；模型不提交到 Git。生产模式要求模型加载成功，不静默退回规则策略。

## 状态生命周期

```text
idle → preparing → starting → waiting → active → thinking → ready
                                  ↘ paused/error
active → ended → waiting
```

`GameObservation` 是模型输入边界；`AdviceResult` 是 UI 输出边界。
