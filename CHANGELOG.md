# Changelog

本仓库所有插件共用一个发布页（[`v1`](https://github.com/706972745/FontManager-Plugin/releases/tag/v1)），
每个插件版本各自独立，以各自目录下的 `plugin.json` 为准。
版本号遵循 [SemVer](https://semver.org/)，记录格式参考
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

发布页的正文由 `.github/package_plugin.py` 依据**本文档最新一段**和**各插件清单里的版本**
自动生成（见 `.github/workflows/plugin-release.yml` 的「同步发布说明」），
所以改插件版本时请同步在这里补一条，别只改 `plugin.json`。

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
