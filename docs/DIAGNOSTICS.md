# 诊断与日志

## 日志位置

默认目录为 `%LOCALAPPDATA%\Guance\logs`。日志是 JSON Lines，使用轮转文件并限制最近记录数量。日志只记录生命周期、状态版本、错误码、耗时、脱敏连接信息和模型加载摘要。

禁止记录 Cookie、Authorization、私钥、原始 payload、完整抓包、模型权重和隐藏手牌。字符串、URL query、bytes 和敏感字段由模块级 `redact()` 处理，但日志调用方仍必须避免把秘密传入日志。正则脱敏是纵深防御，不是任意输入都已匿名化的保证。

`DiagnosticLog` 默认单文件上限 8 MiB、保留 4 份轮转、内存 recent 队列 300 条。字典/列表最多处理 100 项、字符串截至 1600 字符；bytes 只保留长度，Path 只保留文件名。时间字段使用 UTC，界面或人工分析时应明确时区。

## 导出包

用户主动导出后生成 ZIP，内容包括：

- `manifest.json`：版本、Python/platform 摘要和隐私声明；
- `recent.jsonl`：有限条脱敏日志。

不会压缩任意用户目录，不自动上传，也不包含原始 `.mitm`、权重或证书私钥。分享前仍应由用户复核。

## 错误码

| 错误码 | 含义 | 优先动作 |
|---|---|---|
| `CERTIFICATE_OPERATION` | 证书适配操作抛出异常 | 查看证书状态和脱敏错误，不跳过授权 |
| `HELPER_NOT_FOUND` / `ADDON_NOT_FOUND` | 缺少 runtime 输入 | 核对完整发布目录 |
| `HELPER_START_FAILED` | 采集 helper 未启动 | 检查 runtime 和权限 |
| `PROXY_RESTORE_FAILED` | 代理恢复未成功 | 停止继续操作，核对冲突与恢复提示 |
| `frame_dropped`（state） | frame 超限或格式无效 | 保留错误摘要，不分享原始 frame |
| `protocol_rejected` | 命令、版本或结构不接受 | 不要手工放宽校验 |
| `state_invalid` / `state_out_of_order` | 牌局动作无法一致重建 | 停止使用当前建议，等待新局 |
| `engine_paused` | 会话已因前一个错误暂停 | 优先查前一个根因，不将重复暂停视为新故障 |
| `MODEL_INPUT_OR_INFERENCE` | 模型输入或推理失败 | 查看模型摘要和状态版本 |
| `stale_advice_dropped`（event） | 推荐对应的局面已过期 | 等待新一轮计算 |

错误码区分大小写；`state`、`event`、`model_status` 和 `error_code` 不是同一个字段。模型加载/超时还会通过 `model_status=error/timeout` 反馈，不虚构统一大写错误码。W 新增 `PROXY_RECOVERY_CONFLICT`、`PROXY_LEASE_INVALID` 等恢复码，不属于基线已发布保证。

## 排障顺序

1. 先按主界面提示执行下一步；
2. 打开诊断页确认错误码和最近状态；
3. 需要支持时导出脱敏包；
4. 网络问题使用应用内修复动作，不要求用户手改注册表；
5. 退出前确认代理已恢复、helper 已关闭。
