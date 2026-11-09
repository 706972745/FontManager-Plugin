# 示例文本导出（specimen-export）v1.2.2

把任何字族的示例文本渲染成一张 **PNG 样张**，方便对比、分享、留档。

| | |
|---|---|
| 分类 | 🖼️ 图像导出 |
| 权限 | `read:fonts` · `read:curation` · `write:curation` |
| 运行环境 | **自带** Python 3.13 + Pillow（`runtime/`，无需安装 Python） |
| 导出位置 | 默认 `%APPDATA%\FontManager\exports\` |

## 运行环境 / 依赖

| | |
|---|---|
| 解释器 | Python 3.13，**插件 ZIP 自带**（`runtime/python.exe`） |
| 机器上要装 Python 吗 | **不用** |
| 第三方库 | **Pillow 13**（`from PIL import Image / ImageDraw / ImageFont`），已随 `runtime/` 一起打包 |
| 系统组件 | **Windows PowerShell + .NET Framework（WinForms）** —— 设置面板与文件夹选择框的兜底实现 |
| 可选 | `tkinter`：系统里装了**完整** Python 时用它画面板；自带的 embeddable Python 不含 tkinter，会自动改用上面的 PowerShell 方案 |
| 适用平台 | **Windows**（依赖 PowerShell / WinForms，且自带的是 Windows 版 Python） |

> 也就是说：别的都不用装，但**这是个 Windows 专用插件**。软件「设置 → 插件」
> 这一行会显示「自带 Python 3.13」并在悬停里列出 Pillow 与系统组件；
> 若 `runtime/` 不完整，徽标会变红提示「缺少运行时」，重新下载 ZIP 即可。

## 使用

在任意字族上**右键 → 插件 › 示例文本导出 ›**，有一项：

1. **导出示例图（PNG）**——生成 1280px 宽的样张：大字号的示例文本
   （含中文的字族会渲染「永和九年，岁在癸丑」等中文行）、字重行和字形表，
   文件名如 `Noto_Sans_S_Chinese.png`。成功后弹 toast，并把图片路径写进该字族的备注

另外两项**不需要选字体**，在 **设置 → 插件 → 示例文本导出 这一行**里操作（1.3.7+）：

2. **齿轮**（行尾）——打开插件的设置面板，里面就是导出位置（可直接改路径或点「选择…」）。
   插件自带的是 embeddable Python（不含 tkinter），面板用 Windows 自带的系统对话框画出来，
   长得朴素但功能一样；机器上装了完整 Python 时走 tkinter 版本。
3. **在行上右键** —— 「设置导出位置…」「打开导出文件夹」

> 老版本软件（1.3.6 及以前）没有齿轮和行右键，这三项都挂在字族右键的
> 「插件 ›」二级菜单里，功能一样。

## 自定义导出位置（高级）

- 菜单选「设置导出位置…」最方便；
- 或者手改插件目录里的 `config.json`：`{ "exportDir": "D:\\我的样张" }`；
- 或者设环境变量 `FONTMANAGER_EXPORT_DIR`（优先级最高）。

## 启动自检

插件每次被启用都会自动跑一遍：API 健康 → 列字体 → 真实渲染一张 `_selfcheck.png`
→ 写备注 → 越权拦截（应得 403）。结果存在导出目录的 `self-check.json`，
如果插件「开了但没反应」，先看这个文件。

## 卸载

**设置 → 插件** 关掉开关，再删掉插件目录即可；导出的图片不受影响。
