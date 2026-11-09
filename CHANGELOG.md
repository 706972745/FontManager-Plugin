# Changelog

本仓库所有插件共用一个发布页（[`v1`](https://github.com/706972745/FontManager-Plugin/releases/tag/v1)），
每个插件版本各自独立，以各自目录下的 `plugin.json` 为准。
版本号遵循 [SemVer](https://semver.org/)，记录格式参考
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

发布页的正文由 `.github/package_plugin.py` 依据**本文档最新一段**和**各插件清单里的版本**
自动生成（见 `.github/workflows/plugin-release.yml` 的「同步发布说明」），
所以改插件版本时请同步在这里补一条，别只改 `plugin.json`。

## 2026-11-09

> 「下载了用不了」多半不是插件或软件版本的问题，是**环境没配齐**。这一版把每个插件
> 的解释器 / 第三方库 / 系统组件写进 `plugin.json`，发布页和软件插件列表都直接标出来。

### 新增

- **插件自述运行环境**：四个插件的 `plugin.json` 都加了 `requirements`
  （`runtime` / `libraries` / `system` / `platforms` / `note`）。发布页插件表新增
  「运行环境」列，FontManager 1.5.4 的「设置 → 插件」每一行也会显示徽标
  （自带 / 需要 Python / 无需环境 / **缺少运行时**）与完整依赖悬停说明。
- **各插件补齐「运行环境 / 依赖」章节**：`font-stats` 说明只用标准库、包里的
  Pillow/fontTools 是冗余；`specimen-export` 说明实际需要 Pillow + PowerShell/WinForms；
  `font-sync` 说明零第三方库但依赖 Windows 凭据管理器；`template-hello` 新建 README。

### 安全（font-sync 0.8.11）

- **令牌绝不在插件目录**：改存 **Windows 凭据管理器**（`FontManager/font-sync/vault`），
  回退时存 `%APPDATA%\FontManager\font-sync\vault.bin`（DPAPI 加盐密文），
  两者都不可用就只留内存。旧版插件目录里的 `secret.bin` 首次读取时自动迁移并删除。
  插件 ZIP、插件目录、代码、面板快照里都翻不到令牌。
- **上传 / 复制令牌前必须验证是账号本人**：改用 GitHub OAuth Device Flow ——
  在浏览器登录「令牌所属」的 GitHub 账号并输入一次性验证码，插件核对登录名一致才放行；
  临时 `access_token` 只用来读一次 `/user`，随即丢弃，不落盘、不写日志。
  插件里只有公开的 Client ID，**绝无 client_secret**。验证票据只在本进程内存保留 5 分钟，
  换账号 / 换令牌立即作废；插件菜单直接触发的上传没有票据会被拒绝。
- **下载字体回本机不需要验证**（用户口径）：恢复 / 补缺 / 空机全量恢复路径一律不检查。
- 新增 `verify.py`、`/api/verify/*` 端点与面板验证弹窗；115 项单测全绿。

### 修复（font-sync 0.8.12）

- **「连不上 GitHub 的登录验证服务：HTTP Error 400: Bad Request」其实不是连不上**：
  `POST /login/device/code` 返回的 400 被原样抛给了用户，GitHub 的错误体被丢掉。
  实测两种成因——OAuth App 没勾 `Enable Device Flow`（400）与 Client ID 不存在（404）——
  现在都翻成人话并指出该点哪里；弹窗新增「先检测」按钮，保存前就能确诊。
- **预演只报「278 个族」被当成「插件只认了 278 个族」**：库里有 4055 个族，其中 3786 个
  版权未知被默认规则挡掉、另有 9 个超过 85 MB 单族上限，旧文案只说「跳过 3786 个族」。
  现在预演结果固定写成「全库 N 族 → 本次会传 M 族（跳过 … 族版权未知、… 族超大）」，
  并在设置页写明「个人字库九成以上是版权未知，想全量备份就关掉这一项」。
- **新增「改用令牌校验」兜底**：不想建 OAuth App（或 GitHub 死活不放行 Device Flow）时，
  点一下就用保险箱里的令牌打 `GET /user`，登录名与当前账号一致才放行。仍是 GitHub 侧的
  身份校验，只是少了「浏览器一次性验证码」这一层现场证明，面板会标明用的是哪种方式。

### 版本

- `font-sync` 0.8.12 · `font-stats` 1.1.2 · `specimen-export` 1.2.2 · `template-hello` 1.0.1。

## 2026-11-08

> 这一版主要是修「下载到的包和仓库不是一回事」：release 资产曾长期落后于仓库。

### 修复

- **release 里的包终于跟着仓库走**：`plugin-release.yml` 的监听路径以前只有 `font-sync/**`，
  `font-stats.zip` / `specimen-export.zip` 是 11-05 手工传的旧包 —— 仓库里改了代码，
  用户下载到的还是旧版本（`specimen-export` 因此丢了设置面板）。
  现在四个插件目录任一变动都会重新打包，覆盖上传到同一个 `v1` 发布页；
  `package_plugin.py` 也改成遍历目录，并自检「包内版本 == 源码版本、无缓存文件、能完整解开」。
- **specimen-export 1.2.1 —— 齿轮真的能开面板了**：插件自带的 `runtime/` 是
  embeddable Python，**不含 tkinter**，原来的设置面板和「选择文件夹」兜底都依赖它，
  等于点了没反应。现在没有 tkinter 时改用 Windows 自带的 `System.Windows.Forms`
  画同一个面板（脚本用 `-EncodedCommand` 传入，中文路径不会乱码）。
- **font-stats 1.1.1 —— 版权分类说人话**：备注里写的是 `free_personal` / `unknown`
  这类内部代码，现在按主程序界面上的叫法输出「免费商用 / 个人免费 / 收费 / 限时免费 / 未知」，
  与 README 的承诺一致。

### 新增

- **`template-hello.zip`**：模板也能直接下载，不用先 clone 仓库；
  README 的「自己写一个插件」一节补了链接。
- 打包自检覆盖全部四个插件：版本一致性、缓存文件（`__pycache__` / `.pyc`）、压缩包完整性；
  任一不过就让 CI 失败，不再出现「发出去的包和源码不一致」。

### 其它

- 插件目录、版本、接口没有任何破坏性改动，老配置与 `pluginsEnabled` 记录照常可用。

## 2026-11-05

- `specimen-export` 1.2.0：新增 `settings` 钩子（设置面板里改导出位置）。
- `font-stats` 1.1.0：统计字面数、按版权分类汇总，写入字体备注。
- `font-sync` 0.8.10：随本仓库持续维护。
