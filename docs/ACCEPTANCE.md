# 验收标准总入口

请以 [`ACCEPTANCE_STANDARD.md`](ACCEPTANCE_STANDARD.md) 作为 Guance 每次新增或修改功能后的权威验收流程。它按 P0（安全与核心可用性）、P1（核心功能）、P2（体验与发布）排序，包含：

- 变更路径到验收 ID 的映射；
- 每次必跑的 Python 3.12 x64 自动化命令；
- 运行时、GUI、打包和 Windows 实机验收流程；
- 缺陷修复必须增加的回归测试；
- 验收记录模板和最终放行规则。

补充矩阵：

- [`UI_ACCEPTANCE.md`](UI_ACCEPTANCE.md)：UI 控件与实机动作矩阵；
- [`DIAGNOSTICS.md`](DIAGNOSTICS.md)：诊断码、日志和隐私边界；
- [`USER_GUIDE.md`](USER_GUIDE.md)：面向用户的操作说明。

任何 P0 项失败，或任何真实 Windows 项未验证，都不得宣称生产验收通过。

## 文档与仓库整理边界

2026-10-01 的本次整理仅涉及 README、公开文档、忽略规则和原有 `Guance.spec` 的跟踪。业务源码基线为 `957b9c3`，未提交的协议、UI、runtime 或构建脚本修正不随此次发布。

| 检查 | 必需证据 |
|---|---|
| 字段准确 | scalar/nested、wire、缺省值、presence、parser 与 domain 限制逐项核对 |
| 版本准确 | 区分已提交基线、待合并实现、未知含义，不宣称未执行测试通过 |
| 文档完整 | README 导航、内部链接、JSON 示例、命令和目录模板有效 |
| 中性术语 | 公开 Markdown 无目标平台名称、目标服务地址和个人数据 |
| Git 范围 | 暂存清单只含确认的文档、`.gitignore`、`Guance.spec` |
| 资产边界 | 模型、CA、抓包、日志、IDE 和本地自动化记录不进入提交 |
| 推送 | 检查远程领先情况，不强推；推送后校对远程 commit |

这是文档验收，不替代产品 P0/P1/P2 验收。原有业务失败不通过此次文字修复或降级标准，其修复和回归留在对应开发任务中。
