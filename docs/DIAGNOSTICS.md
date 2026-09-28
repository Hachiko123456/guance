# 诊断与日志

## 默认日志

日志保存在 `%LOCALAPPDATA%\Guance\logs`，采用 JSON Lines 和轮转文件。默认只记录：

- 生命周期状态；
- 代理模式（direct/upstream）；
- 连接主机名的脱敏版本；
- 协议命令号和长度；
- 状态版本、错误码和耗时；
- 模型加载耗时、模型指纹和推荐耗时。

不记录 Cookie、Authorization、私钥、原始 payload、原始 `.mitm` 或对手隐藏手牌。

## 诊断包

用户点击“导出诊断”后生成 ZIP，仅包含：

- manifest；
- 最近有限条脱敏日志；
- 版本和运行环境；
- 当前状态摘要；
- 模型加载状态和 SHA-256。

诊断包不自动上传，用户自行决定是否分享。

## 常见错误码

| 错误码 | 含义 |
|---|---|
| `CERTIFICATE_CONSENT_REQUIRED` | 尚未明确授权证书 |
| `CERTIFICATE_NOT_TRUSTED` | 当前用户证书库未确认信任 |
| `PROXY_CONFLICT` | 系统代理在掼策接管期间被其他程序修改 |
| `CAPTURE_START_FAILED` | 采集器未能启动 |
| `CAPTURE_FRAME_DROPPED` | frame 超过限制或格式无效 |
| `PROTOCOL_REJECTED` | 未知命令、包结构或消息版本 |
| `STATE_INVALID` | 牌局校验失败，已暂停推荐 |
| `MODEL_REQUIRED` | 模型未加载，生产模式不允许规则回退 |
| `MODEL_INPUT_OR_INFERENCE` | 模型输入或推理失败 |
| `STALE_ADVICE_DROPPED` | 推荐对应的状态已经过期 |

## 排障原则

1. 先看主界面给出的用户动作，不要求用户阅读堆栈。
2. 再打开诊断页检查最后错误码。
3. 导出脱敏诊断包。
4. 网络问题先点击“修复网络”，不要求用户手改注册表。
