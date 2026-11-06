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

## ⚙️ 插件行快捷入口（FontManager 1.3.7+）

装好之后，**设置 → 插件** 里每个插件那一行本身也是入口，不用先找一款字体：

| 操作 | 效果 |
|---|---|
| 点行尾的 **齿轮** | 打开这个插件自己的**设置面板**（只有声明了 `settings` 的插件才有齿轮，没声明的行里不会出现按钮） |
| 在**这一行任意位置右键** | 弹出插件菜单：该插件里「不需要选中字体」的功能（打开导出目录、改保存位置、清缓存…），加上「打开插件设置」「打开插件目录」 |
| 点行尾的 **文件夹** | 直接打开这个插件所在的目录（改配置、看日志） |

齿轮是灰的 = 插件没在运行，把左边的开关打开即可。
插件自己的配置项（比如示例文本导出的保存位置）就收在齿轮里，不用再去翻 `config.json`。

> 开发者：这两处入口都在 `plugin.json` 里声明 —— 齿轮对应 `"settings": { "title": …, "hook": "settings" }`，
> 行右键对应 `"menus": [{ "title": …, "on": "plugin" }]`。细节见
> [插件开发指南](https://github.com/706972745/FontManager/blob/main/docs/PLUGINS.md#设置面板settings-字段--settings-钩子)。

## 🔐 权限

每个插件在自己的 `plugin.json` 里声明所需权限，FontManager 只给声明过的：插件拿到的是
**受限令牌**，越权请求被 API 直接拒绝（**403**，正文会写明缺哪一项）。

| 权限 | 能做什么 | 写它的理由 |
|---|---|---|
| `read:fonts` | 读字体 / 字族清单、启用状态 | 「库里有没有这款字」 |
| `write:fonts` | **启用 / 停用 / 重扫** | 改的是系统级状态，**能不给就不给** |
| `read:snapshot` / `write:snapshot` | 看 / 建 / 删 / 应用方案快照 | 批量切换字体方案 |
| `read:curation` / `write:curation` | 读 / 改你写的标签、备注、评分、收藏 | 备注里可能有私事，`read:` 也要慎重 |
| `export` | 触发打包导出、查进度 | 导出字体包 |
| `import` | 导入字体包 | 装字体 |

判断顺序是 **401（没令牌）→ 403（没这项权限）→ 422（请求体格式错）**：权限检查在解析
请求体**之前**，所以缺权限永远是 403，看到 422 说明权限够了、是参数写错了。

**报 403 怎么修**：把缺的那项加进**插件自己**的 `plugin.json` → `permissions`，然后
设置 → 插件 →「重新扫描」（或关掉开关再打开）——令牌是启用时签发的，不重启不会变。
**不需要改 FontManager。** 想知道自己手里到底有什么，读 `GET /health` 的 `permissions`
（`tokenName` 是令牌名，`allPermissions` 才是全部可用权限）。

> ⚠️ **别写 `"permissions": ["*"]`**：主程序不会过滤，写了就是**全权**。
> 权限边界靠的是这一行写得够窄。
>
> 完整权限表、`perm_for` 的判定逻辑、加新接口的规矩，见
> [插件开发指南 · 权限](https://github.com/706972745/FontManager/blob/main/docs/PLUGINS.md#权限403-到底是谁拦的)。
> 所有请求都可查（`%APPDATA%\FontManager\activation.log`）。

## 🛠️ 自己写一个

插件是**进程外程序**：主程序不加载插件代码，插件崩了不影响软件。
通信只有两条：stdin/stdout 的 JSON 行（握手 + 调用），以及本地 HTTP API（受限令牌）。

完整字段表、握手协议、调试方法见 **[插件开发指南](https://github.com/706972745/FontManager/blob/main/docs/PLUGINS.md)**。
两个插件本身就是参考实现；想要**零依赖**的插件，像它们一样把解释器放进 `runtime/`
并在 `plugin.json` 写 `"interpreterPath": "runtime/python.exe"`，或者入口直接用 `.exe`。

## 📜 License

GPL-3.0 —— 与主程序 [FontManager](https://github.com/706972745/FontManager) 一致
（FontManager 迭代自 TheHolyOneZ 的开源项目 [ZFontManager 0.8.0](https://github.com/TheHolyOneZ/ZFontManager)）。
