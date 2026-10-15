# TD-IMPLEMENT-20261002 — 自动后台训练数据验收

## 原始需求与范围
用户授权：检查分支；在已有非 main 分支实施；增加详细、可供大模型阅读的数据文档；支持可验证的旧对局转换；默认后台采集且不新增页面；独立线程慢写盘不得阻塞或改变原有流程；先定义验收并用旧数据测试；主子代理实施。

当前分支 `codex/auto-training-data-demo`，起点 `e91b181`；已有未跟踪 `prototypes/` 是用户前一轮原型，保留。不新建仓库、worktree、分支或主会话，不提交/推送，不启动真实游戏、代理、证书、采集或模型训练。

本次接受的设计：独立 training_data infrastructure，消费 SessionEngine 已验证的安全 EngineUpdate，不直接保存报文/SettlementEvent。在线 producer 白名单冻结 + 有界 put_nowait；独立 daemon writer 进行关联、验证、JSON 编码和原子落盘。离线转换复用同一安全构建器但使用同步无丢弃模式。默认开启，无页面、无启动阻塞。异常只影响数据收集；缺失数据不得伪装完整。语义数据为真源，特征/目标为可再生派生视图；不以旧模型评分为真值，不承诺任意未来网络兼容。

## 验收映射（实现前定义）
| ID / 阻断级 | 来源 | 前提 / 输入 | 预期可观察结果 | 方法 / 所需证据 | 状态 |
|---|---|---|---|---|---|
| TD-01 / P0 | 用户1；P2-006 | 当前分支及已存在 prototypes | 保留已有分支和原型，无额外分支/worktree/提交/推送 | git status/branch/diff 前后对照 | passed |
| TD-02 / P0 | 用户4；P0-006/007 | 带未知敏感字段的合成 table/advice/error；含结算隐藏手牌的离线录制 | 输出仅白名单本人视角/公开事件/安全名次；无原始 payload、隐藏对手手牌、凭据、私钥/原始路径；不新增页面、不调用真实采集 | 敏感哨兵、序列化结构和副作用测试 | passed |
| TD-03 / P0 | 用户4；P1-002/006 | 冻结前置 observation 后修改上游容器、收到真实动作、迟到建议 | 输入不变；只关联紧接其后的确认本人动作；候选点击/推荐/重复状态不产生动作；未知语义不猜 | 状态键/动作索引/输入哈希和样本数量断言 | passed |
| TD-04 / P0 | 用户4；P0-008 | ended、verified settled、重复/冲突结算、断线、下一局 | ended 不生成终局标签；合法结算幂等；不完整/错误分级 partial/quarantine，结果不伪造；不跨局污染 | 精确事件序列、输出状态和理由断言 | passed |
| TD-05 / P0 | 用户4 | worker 写入被 Event 阻塞，队列填满；注入 OSError | producer 不等待 writer；有界队列满明确丢失/停止本轮记录并隔离，不能继续声称完整；原推荐/状态处理继续 | 阻塞 writer 测试；100 次 submit 在 0.5 秒内完成；controller fake 正常路径计数不变 | passed |
| TD-06 / P0 | 用户4；P0-005 | writer 慢/卡住，stop/close，队列有待处理内容 | 记录器 close 默认等待不超过 0.25 秒（测试宽限 0.75）；不在 stop 等待磁盘；网络清理先完成；完整原子文件或显式未完成，不写半截 ready 文件 | fake writer、恢复/关闭顺序、临时文件与 JSON 验证 | passed |
| TD-07 / P0 | 用户2；数据语义 | 双副牌、红桃级牌、大小王、pass、非跟牌合法动作、歧义配牌 | 复用验证实体牌码；红桃级牌才是逢人配；实例ID与牌型声明不混同；未知声明标未知；动作稳定 ID 与数组顺序无关 | 编码金样本/牌码验证/合法性反例 | passed |
| TD-08 / P1 | 用户2 | 语义包和当前 FableDan 适配器 | 公共 schema/rules/card/action/encoder 版本独立；非有限值/未知必需版本拒绝；重编码与当前 encode_decision 一致；缺标签给不支持原因，不补零oracle、不写假 reward | JSON 结构验证、80维/48词表派生对照、文档示例测试 | passed |
| TD-09 / P0 | 用户3/5 | 旧 frames.jsonl（双方向、有/无方向、坏base64/hash/断尾）、.mitm 支持范围 | 只离线读，不重放网络；client 忽略；缺方向默认拒绝，不猜server；损坏保留拒绝原因；输入文件不被修改 | 合成录制自动化测试、源哈希前后对照 | passed |
| TD-10 / P1 | 用户3/5 | 当前本地旧对局目录和 observations fixture | 转换至少一条已存在可解码录制，并与独立 SessionEngine 重建的本人决策输入/动作/结果逐项比较；无法转换报告原因，不能宣称全部成功 | 本地转换摘要、决策计数/字段/哈希一致；私有输出留 ignored 目录 | passed |
| TD-11 / P1 | 用户2 | 文档+生成示例 | 逐字段说明类型、必需/可空、单位、视角、可见时点、未知与过牌区别、版本和迁移、数据集整局分组、默认后台/失败/退出/CLI/隐私和未来兼容边界 | 文档与代码/示例/CLI对照 | passed |
| TD-12 / P1 | 用户4；原流程 | 现有 domain/protocol/runtime/integration/model/UI tests | 无新增失败；原先失败独立列出，不修改测试弱化要求 | 基线/最终 pytest XML；聚焦控制器异常注入测试 | passed |

## 初始证据与限制
- Python：py -3.12，3.12.14 x64；pytest 9.1.1。
- 基线命令（cwd D:\project\guance）：`python -m pytest -q tests/domain tests/protocol tests/runtime tests/integration tests/model --junitxml=build/acceptance/td-20261002/baseline.xml`，退出码 1：89 passed / 1 failed。既有失败为 floating_layout sizeMode 不匹配（test_floating_layout_is_persisted_and_restored）；不在本次修复范围。
- 已发现 9 个旧 frames.jsonl 会话（不含重复 games 子切片），其中一个会话 1067 行均缺 direction；不能静默按 server 解释。
- 实机长时间性能、系统突然断电、实际训练收益及新模型任意架构兼容：未验证，非本次自动测试通过即可宣称。
- 最终通过/失败/跳过/未验证及放行结论：pending。

## Final validation

- Passed: core recorder/collector tests, legacy converter tests, FableDan derivation tests, controller isolation tests: **54 passed**; impacted non-UI layers and integration: **94 passed, 1 pre-existing failure**; full suite: **229 passed, 2 pre-existing floating-window failures, 1 skipped**; AST syntax, `git diff --check`, canonical conversion and source-hash checks passed.
- Passed old-data evidence: 9 top-level JSONL sessions audited; 8 converted, 1 rejected for missing direction; 160 canonical decision records validated; 160/160 independent SessionEngine field comparisons; current adapter 156 derived, 4 rejected for pre-existing ambiguous public-action semantics, 7 warnings for no exact entity action index.
- Failed: no new functional failure established. Existing failures: `test_floating_layout_is_persisted_and_restored` (sizeMode manual mismatch) and `test_floating_fit_grows_only_and_manual_close_flushes_geometry` (manual vs fit); unrelated to training-data code and not weakened.
- Skipped: real Windows long-run/kill/power-loss; `.mitm` converter support intentionally rejected pending strict direction adapter; real model weights/training quality; no network/real capture.
- Unverified: production performance under sustained capture, crash recovery, actual model improvement, and user approval of default-on collection policy.
- Release decision: **not a production release**. This task implements and tests the background data path; training use still requires offline dataset review and independent model evaluation.

Evidence files: `build/acceptance/td-20261002/focused-tests.xml`, `build/acceptance/td-20261002/full-impacted.xml`, `build/acceptance/td-20261002/full.xml`, `private_data/training-data-validation/local-conversion-summary.json` (ignored local evidence).
