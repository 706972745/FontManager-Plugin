# 你好，插件（template-hello）v1.0.1

最小可用的插件模板：右键一个字族，看它有多少字面。改 `plugin.json` 的
`id` / `name` / `menus` 和 `main.py` 里的逻辑，就是一个新插件。

## 运行环境 / 依赖

| | |
|---|---|
| 解释器 | **Python 3.10+**，本插件**不自带** |
| 从哪来 | FontManager「设置 → 插件 → 运行时管理」下载的 Python，或系统里已装的 Python 3.10+ |
| 第三方库 | **无**（只用 `json` / `sys` / `urllib` 等标准库） |
| 系统组件 | 无 |
| 适用平台 | Windows / Linux / macOS（主程序目前只发布 Windows 版） |

**没装 Python 时会怎样**：软件「设置 → 插件」里这一行会出现红色的「缺少运行时」
徽标，打开开关会失败并提示到「运行时管理」下载 —— 这**不是插件坏了，也不是软件
版本不对**。装好 Python（或在运行时管理里点「下载补全」）后重新打开开关即可。

想做成「下载即用、用户什么都不用装」的插件：把便携 Python 放进 `runtime/`，
并在 `plugin.json` 里写 `"interpreterPath": "runtime/python.exe"`，参考
[`font-stats/`](../font-stats/) 或 [`specimen-export/`](../specimen-export/)。

## 使用

1. 复制整个 `template-hello/` 目录，改 `plugin.json` 里的 `id`（必须全局唯一）；
2. 把文件夹放进 设置 → 插件 →「打开插件目录」打开的目录，点「重新扫描」；
3. 打开这一行的开关，右键任意字族 → 插件 › 你好，插件。

插件协议（stdin/stdout JSON 行、权限、菜单、设置面板）见
[插件开发指南](https://github.com/706972745/FontManager/blob/main/docs/PLUGINS.md)。
