# 掼策（Guance）

面向 Windows 10/11 的本地大掼蛋对局状态助手：在用户明确授权的本机网络环境中，解析自己账号的对局公开消息，重建可验证牌局状态，并使用本地 FableDan 模型给出推荐。

> **当前状态：开发版。** 默认只展示建议，不自动点击游戏、不自动出牌、不上传原始对局。

## 已完成的第一版能力

- 经典玩法实时聚合消息解析（发牌、首出、出牌、过牌、结算）；
- 历史复盘与保存抓包的离线解析接口；
- fail-closed 状态机：漏包、重复包、未知命令、牌数不一致时暂停；
- 只读本人手牌和公开历史的 observation 契约；
- NumPy FableDan 推理适配，要求真实模型加载成功，不静默规则回退；
- 有界、脱敏的诊断日志；
- 有代理/无显式代理的采集模式抽象；
- PySide6/QML 简洁桌面界面与首次授权说明；
- PyInstaller onedir 构建脚本。

## 本地开发

目标环境：Python 3.12 x64，Windows 10/11。

```powershell
$env:PYTHONPATH="src"
python -m compileall -q src tests
python -m pytest -q
python -m guance --demo --exit-after 1
```

## 模型

将你训练好的模型放到：

```text
data/profiles/tencent_daguandan/models/best.npz
```

模型文件被 `.gitignore` 排除，不会被意外推送到 GitHub。发布构建会在本机读取模型并将其放入本地发布目录；模型文件的 SHA-256 会写入诊断信息。

## 发布构建

需要本机已安装/准备 mitmproxy 的 Windows frozen runtime：

```powershell
python scripts/build_release.py --clean
```

输出：

```text
release/Guance/Guance.exe
```

## 文档

- `docs/ARCHITECTURE.md`：分层、进程和依赖方向；
- `docs/PROTOCOL.md`：当前验证版本的协议命令与牌码；
- `docs/USER_GUIDE.md`：普通用户操作；
- `docs/DIAGNOSTICS.md`：日志与排障；
- `docs/SECURITY.md`：证书、代理和隐私边界；
- `docs/ACCEPTANCE.md`：验收标准；
- `docs/BUILD.md`、`docs/RELEASE.md`：构建与发布。

## 第三方代码和模型

FableDan vendored runtime 的 revision 与许可证位于 `src/guance/infrastructure/model/fabledan_vendor/`。发布前请根据用途完成许可证审查。
