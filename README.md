<div align="center">

# FontManager-Plugin

[![License: GPL v3](https://img.shields.io/badge/License-GPL%20v3-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgray)](https://github.com/706972745/FontManager)
[![FontManager](https://img.shields.io/badge/for-FontManager%201.3%2B-5b21b6)](https://github.com/706972745/FontManager)

**FontManager 的官方插件集 —— 每个插件独立目录、自带运行环境，下载即用。**

主程序：[706972745/FontManager](https://github.com/706972745/FontManager) ·
插件开发规范：[docs/PLUGINS.md](https://github.com/706972745/FontManager/blob/main/docs/PLUGINS.md)

</div>

---

## 📦 插件列表

### 🖼️ 图像导出

| 插件 | 版本 | 说明 | 下载 |
|---|---|---|---|
| [示例文本导出](specimen-export/) | 1.2.0 | 右键字族 → 把示例文本渲染成 PNG（1280px 样张）；保存位置可自设；启动时自动跑 API / 权限自检 | [ZIP](https://github.com/706972745/FontManager-Plugin/releases/latest/download/specimen-export.zip) |

### 📊 数据统计

| 插件 | 版本 | 说明 | 下载 |
|---|---|---|---|
| [字体使用统计](font-stats/) | 1.1.0 | 右键字族 → 统计它的字面数与版权分类，结果写进该字族的备注 | [ZIP](https://github.com/706972745/FontManager-Plugin/releases/latest/download/font-stats.zip) |

> 两个插件都**自带 Python 运行时**（`runtime/`，Python 3.13 + Pillow + fontTools，各约 56 MB）——
> 机器上**不用装任何东西**，下载解压即可。`plugin.json` 里的 `interpreterPath` 让主程序直接使用插件自带的环境。
>
> 📖 每个插件目录里都有自己的**详细教程**（菜单用法、配置项、自检与卸载）：
> [示例文本导出](specimen-export/) · [字体使用统计](font-stats/)

## 🚀 安装

**方式一（推荐）：Release 下载**

1. 到 [Releases](https://github.com/706972745/FontManager-Plugin/releases/latest) 下载想要的插件 ZIP
2. 解压出插件文件夹（如 `specimen-export`）
3. 打开 FontManager → **设置 → 插件 → 「打开插件目录」**，把文件夹放进去
4. **设置 → 插件** 里打开插件开关（或点「重新扫描」）
5. 在任意字族上**右键 → 插件 ›** 使用

**方式二：直接下目录**

进入对应插件目录 → 右上角 **Code → Download ZIP**，后续步骤相同。

## 🔐 权限

每个插件在自己的 `plugin.json` 里声明所需权限（如 `read:fonts`、`write:curation`），
FontManager 只给声明过的：插件拿到的是**受限令牌**，越权请求会被 API 直接拒绝（403）。
所有请求都可查（`%APPDATA%\FontManager\activation.log`）。

## 🛠️ 自己写一个

插件是**进程外程序**：主程序不加载插件代码，插件崩了不影响软件。
通信只有两条：stdin/stdout 的 JSON 行（握手 + 调用），以及本地 HTTP API（受限令牌）。

完整字段表、握手协议、调试方法见 **[插件开发指南](https://github.com/706972745/FontManager/blob/main/docs/PLUGINS.md)**。
两个插件本身就是参考实现；想要**零依赖**的插件，像它们一样把解释器放进 `runtime/`
并在 `plugin.json` 写 `"interpreterPath": "runtime/python.exe"`，或者入口直接用 `.exe`。

## 📜 License

GPL-3.0 —— 与主程序 [FontManager](https://github.com/706972745/FontManager) 一致
（FontManager 迭代自 TheHolyOneZ 的开源项目 [ZFontManager 0.8.0](https://github.com/TheHolyOneZ/ZFontManager)）。
