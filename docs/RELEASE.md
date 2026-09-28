# 发布说明

## 构建前

```powershell
$env:PYTHONPATH="src"
python -m compileall -q src
python -m pytest -q
```

## 模型

将用户训练的 `best.npz` 放到：

```text
data/profiles/tencent_daguandan/models/best.npz
```

构建脚本必须验证文件存在、SHA-256 和模型配置，然后复制到发布目录。模型不进入默认 Git 提交。

## 打包目标

首版使用 PyInstaller onedir：

- 主程序；
- QML 资源；
- mitmdump 和 `_internal`；
- FableDan vendored runtime；
- profile 配置；
- 用户选择是否随发布包附带模型。

## 发布前实测

必须在 Windows 10 22H2 和 Windows 11 x64 中分别测试 direct 与 HTTP/mixed upstream 模式。没有真实机器证据的组合必须标记为未验收。
