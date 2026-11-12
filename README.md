<div align="center">

# FontManager-Plugin

[![License: GPL v3](https://img.shields.io/badge/License-GPL%20v3-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgray)](https://github.com/706972745/FontManager)
[![FontManager](https://img.shields.io/badge/for-FontManager%201.3%2B-5b21b6)](https://github.com/706972745/FontManager)

**FontManager 的官方插件集 —— 每个插件独立目录，运行环境在下表标清楚，下载前先看一眼。**

主程序：[706972745/FontManager](https://github.com/706972745/FontManager) ·
插件开发规范：[docs/PLUGINS.md](https://github.com/706972745/FontManager/blob/main/docs/PLUGINS.md)

</div>

---

## ✍️ 自己写一个插件（5 分钟）

**先确认解释器**：FontManager 安装包不内置解释器（1.4.11 起），写插件前到
**设置 → 插件 → 运行时管理** 一键下载 **Python 3.13**（含 Pillow、fontTools，约 19 MB）或
**Node.js 22 LTS**（约 32 MB），也可以用系统里已有的 Python/Node —— 装没装、缺什么、
占多少空间，界面上一眼可见，随时可删、可一键补回。
**软件「设置 → 插件」里每一行都会标出该插件需要什么解释器、是否自带、还缺什么**，
缺环境时徽标变红 —— 那不是插件坏了，也不是软件版本不对。

1. 把 [`template-hello/`](template-hello/) 复制一份当起点（一百来行，每行都有注释；
   也可以直接下 [template-hello.zip](https://github.com/706972745/FontManager-Plugin/releases/latest/download/template-hello.zip)）；
2. 改 `plugin.json` 的 `id` / `name` / `menus` / `permissions`，改 `main.py` 里
   `do_family()` / `do_plugin()` 两个函数为你的功能；
3. 整个文件夹放进 设置 → 插件 →「打开插件目录」打开的目录，点「重新扫描」+ 打开开关；
4. 右键字族（或插件那一行）就能看到你的菜单项。

完整规范（plugin.json 全字段、权限表、stdin/stdout 协议、进度条消息、设置面板、运行时管理、调试方法）：
**[插件开发指南](https://github.com/706972745/FontManager/blob/main/docs/PLUGINS.md)**。
更重的依赖需求参考 [`font-sync/`](font-sync/) 与
[`specimen-export/`](specimen-export/) 的自带运行时做法。

---

## 📦 插件列表

### 🖼️ 图像导出

| 插件 | 版本 | 说明 | 下载 |
|---|---|---|---|
| [示例文本导出](specimen-export/) | 1.2.2 | 右键字族 → 把示例文本渲染成 PNG（1280px 样张）；保存位置可自设；启动时自动跑 API / 权限自检 | [ZIP](https://github.com/706972745/FontManager-Plugin/releases/latest/download/specimen-export.zip) |

### 📊 数据统计

| 插件 | 版本 | 说明 | 下载 |
|---|---|---|---|
| [字体使用统计](font-stats/) | 1.1.2 | 右键字族 → 统计它的字面数与版权分类，结果写进该字族的备注 | [ZIP](https://github.com/706972745/FontManager-Plugin/releases/latest/download/font-stats.zip) |

### 🎯 套用到设计软件

| 插件 | 版本 | 说明 | 下载 |
|---|---|---|---|
| [字体套用（PS / AI / CDR）](font-apply/) | 1.1.2 | 右键任意字族 → 直接套到 Photoshop / Illustrator / CorelDRAW 当前选中的文字上；四条菜单（三个软件 + 「自动：最近在用的那个」）。**速览界面里双击任意可复制的位置也会套用**（主程序 1.9.12+，1.9.13 起仅限速览），双击行为可在插件设置里选「直接套用」或「先点一下目标窗口」。它是个**薄插件**：套用由主程序执行（`POST /fonts/apply`），插件只挑字重、认目标、翻提示，里面没有一行 COM / JSX / PowerShell —— 主程序修好的问题，插件不会拖后腿。要求 FontManager **1.9.11+**（常驻提示要 1.9.14+，CorelDRAW 字重修复在 1.9.20+） | [ZIP](https://github.com/706972745/FontManager-Plugin/releases/latest/download/font-apply.zip) |

### ☁️ 备份 / 多机同步

| 插件 | 版本 | 说明 | 下载 |
|---|---|---|---|
| [字体库同步（GitHub）](font-sync/) | 0.8.11 | 把字体库**按字族**打包，增量同步到**你自己**的私有 GitHub 仓库（多账号、多库切换，没配好不工作）；只传变化的族；四条安全硬闸（私有性匿名探测 / 授权未知默认不传 / 令牌存 Windows 凭据管理器不在插件里 / 上传需 GitHub 登录验证）。**设置面板开在应用内窗口**（≥1.4.3；老版退回 Edge 独立窗口），含权限说明与从零教程；本地删掉字族会同步删除远端备份（可关，清单不完整时有保险丝拦删）；选库页可下拉选择已有私有库或一键新建（带防呆）；同步有实时进度条（面板 + **主页面底部进度条**，需主程序 1.4.8+）。**按需下载**：右键单族「从 GitHub 下载」、面板「补缺」页导入快照分析缺失后勾选补齐；空机一键全量恢复（逐文件 sha256 校验）。0.8.11：令牌迁出插件目录改存 Windows 凭据管理器，上传 / 复制令牌需浏览器登录对应 GitHub 账号验证（下载不需要），`plugin.json` 补运行环境自述。零第三方依赖 | [ZIP](https://github.com/706972745/FontManager-Plugin/releases/latest/download/font-sync-0.8.11.zip) |

### 运行环境总览（下载前先看）

| 插件 | 解释器 | 自带？ | 第三方库 | 系统组件 | 平台 |
|---|---|---|---|---|---|
| 示例文本导出 | Python 3.13 | ✅ `runtime/` | Pillow 13，已带 | 系统对话框组件 | Windows |
| 字体使用统计 | Python 3.13 | ✅ `runtime/` | 无（纯标准库） | 无 | Windows |
| 字体套用 | Python 3.10+ | ❌ 需软件/系统提供 | 无 | Windows PowerShell 5.1、Photoshop / Illustrator / CorelDRAW | Windows |
| 字体库同步 | Python 3.10+ | ❌ 需软件/系统提供 | 无 | Windows 凭据管理器、DPAPI、浏览器 | Windows |
| 你好，插件（模板） | Python 3.10+ | ❌ 需软件/系统提供 | 无 | 无 | 跨平台代码 |

> 「需软件/系统提供」= 到 **设置 → 插件 → 运行时管理** 下载一次共享 Python，
> 或机器上本来就有 Python 3.10+。软件插件清单里会直接标出「需要 Python」/「缺少运行时」。
> 各插件的 `plugin.json` 都写了 `requirements`，发布页表格与软件界面读的是同一份数据。
> `font-sync` 的令牌存 **Windows 凭据管理器**，插件目录里没有令牌；上传与复制令牌
> 还需在浏览器登录对应 GitHub 账号验证，下载字体不需要验证。
>
> 📖 每个插件目录里都有自己的**详细教程**（菜单用法、依赖、自检与卸载）：
> [示例文本导出](specimen-export/) · [字体使用统计](font-stats/) · [字体套用](font-apply/) · [字体库同步](font-sync/) · [模板](template-hello/)

> 每个版本的变更（也是发布页正文的来源）记在 [CHANGELOG.md](CHANGELOG.md)；
> 发布页的插件表与版本由 CI 从各插件 `plugin.json` 读取，不会和仓库脱节。

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
