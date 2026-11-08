# -*- coding: utf-8 -*-
"""打包插件 zip（CI 与本地共用同一套清单）。

产物（写入仓库根的 dist/）：

- `font-sync-<版本>.zip`  纯代码（压缩），普通用户装这个；文件名带版本号，
                          runtime 由主程序启用插件时自动注入，不用（也不该）打进包里
- `font-sync.zip`         上者的无版本号别名（历史链接
                          releases/latest/download/font-sync.zip 继续可用）
- `font-sync-full.zip`    代码 + tests（不压缩），调试用
- `font-stats.zip`        整个插件目录（含自带 runtime/），解压即用
- `specimen-export.zip`   同上
- `template-hello.zip`    模板（无 runtime），给写插件的人当起点

> 后三个以前是手工上传的：仓库改了版本、release 里的包不变，用户下载到的
> 是旧代码（例如没有 settings 齿轮）。现在统一由这里打包，CI 每次推送都覆盖。

另外生成 `dist/release-notes.md`（发布页正文）：插件清单里的版本 + `CHANGELOG.md`
最新一段拼出来，CI 用 `gh release edit` 覆盖到 `v1` 发布页 —— 版本改了、说明跟着变，
不用再手工维护一份表格。
"""
import json
import os
import re
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "dist")

# font-sync 走「纯代码」策略：它零第三方依赖，runtime 由主程序注入
FONT_SYNC = "font-sync"
FONT_SYNC_CODE = ["plugin.json", "main.py", "panel.py", "panel.html", "gh.py", "packer.py",
                  "store.py", "syncer.py", "README.md"]

# 其它插件「整目录」打包：自带 runtime/，用户解压后直接是一个文件夹
FULL_DIR_PLUGINS = ["font-stats", "specimen-export", "template-hello"]

# 打包时一律跳过的东西（本地跑过就会有，进包只会变大）
SKIP_DIRS = {"__pycache__", ".git", "dist", ".pytest_cache"}
SKIP_SUFFIX = (".pyc", ".pyo")
SKIP_FILES = {".DS_Store", "Thumbs.db"}


def manifest(plugin):
    path = os.path.join(ROOT, plugin, "plugin.json")
    if not os.path.isfile(path):
        sys.exit(f"缺少清单：{plugin}/plugin.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def zip_font_sync(out_dir):
    src = os.path.join(ROOT, FONT_SYNC)
    version = manifest(FONT_SYNC)["version"]
    for f in FONT_SYNC_CODE:
        if not os.path.isfile(os.path.join(src, f)):
            sys.exit(f"缺少代码文件：font-sync/{f}")

    zp = os.path.join(out_dir, "font-sync.zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in FONT_SYNC_CODE:
            z.write(os.path.join(src, f), f)

    # 带版本号的正式产物名（用户要的是「文件名一眼看出版本」）；
    # font-sync.zip 保留为别名，旧链接不断
    zv = os.path.join(out_dir, f"font-sync-{version}.zip")
    shutil.copyfile(zp, zv)

    zf = os.path.join(out_dir, "font-sync-full.zip")
    with zipfile.ZipFile(zf, "w", zipfile.ZIP_STORED) as z:
        for f in FONT_SYNC_CODE:
            z.write(os.path.join(src, f), f)
        for base in ("tests", "runtime"):
            base_dir = os.path.join(src, base)
            if not os.path.isdir(base_dir):
                continue
            for root, dirs, files in os.walk(base_dir):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for f in files:
                    if f.endswith(SKIP_SUFFIX) or f in SKIP_FILES:
                        continue
                    p = os.path.join(root, f)
                    z.write(p, os.path.relpath(p, src))
    return [zp, zv, zf], version


def zip_plugin_dir(plugin, out_dir):
    src = os.path.join(ROOT, plugin)
    version = manifest(plugin)["version"]
    out = os.path.join(out_dir, f"{plugin}.zip")
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, dirs, files in os.walk(src):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for f in sorted(files):
                if f.endswith(SKIP_SUFFIX) or f in SKIP_FILES:
                    continue
                p = os.path.join(root, f)
                # 顶层保留插件目录名：解压出来就是 <plugin>/，直接放进插件目录即可
                arc = os.path.join(plugin, os.path.relpath(p, src))
                z.write(p, arc)
                count += 1
    return out, version, count


def verify(path, plugin, version):
    """自检：能完整解开、plugin.json 可读、版本一致、没有缓存文件混进去。"""
    with zipfile.ZipFile(path) as z:
        if z.testzip() is not None:
            sys.exit(f"{os.path.basename(path)} 有损坏条目")
        names = z.namelist()
        bad = [n for n in names if "__pycache__" in n or n.endswith(SKIP_SUFFIX)]
        if bad:
            sys.exit(f"{os.path.basename(path)} 混进了 {len(bad)} 个缓存文件，例如 {bad[0]}")
        # font-sync 的包是平铺的（plugin.json 在根），其它是 <plugin>/plugin.json
        cand = [n for n in ("plugin.json", f"{plugin}/plugin.json") if n in names]
        if not cand:
            sys.exit(f"{os.path.basename(path)} 里找不到 plugin.json")
        got = json.loads(z.read(cand[0]).decode("utf-8"))["version"]
        if got != version:
            sys.exit(f"{os.path.basename(path)} 版本不一致：包内 {got}，源码 {version}")


def one_liner(plugin):
    """表格里的一句话说明：优先 summary，否则取 description 的第一句。"""
    m = manifest(plugin)
    text = (m.get("summary") or m.get("description") or "").strip()
    text = re.split(r"[；。]", text)[0]
    return text if len(text) <= 60 else text[:59] + "…"


def changelog_section():
    """CHANGELOG.md 里最新的一段（第一个 `## ` 到下一个 `## ` 之前）。"""
    path = os.path.join(ROOT, "CHANGELOG.md")
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    out, capturing = [], False
    for line in lines:
        if line.startswith("## "):
            if capturing:
                break
            capturing = True
        if capturing:
            out.append(line)
    return "\n".join(out).strip()


def write_release_notes(downloads):
    """生成发布页正文。downloads：{插件目录: 资产文件名}。"""
    repo = "https://github.com/706972745/FontManager-Plugin"
    rows = [
        "| 插件 | 版本 | 下载 | 说明 |",
        "|---|---|---|---|",
    ]
    order = [FONT_SYNC] + FULL_DIR_PLUGINS
    for plugin in order:
        m = manifest(plugin)
        asset = downloads[plugin]
        rows.append(
            f"| [{m['name']}]({repo}/tree/main/{plugin}) | {m['version']} "
            f"| [`{asset}`]({repo}/releases/download/v1/{asset}) | {one_liner(plugin)} |"
        )

    parts = [
        "## 插件合集 v1（自带运行时）",
        "",
        "每个插件一个独立目录，下载对应 ZIP 解压后放进 FontManager 的插件目录即可；",
        "`font-stats` / `specimen-export` 自带 Python 运行时（Python 3.13 + Pillow + fontTools），",
        "机器上不用装任何东西。安装步骤见 [README](" + repo + "#-安装)。",
        "",
        *rows,
        "",
        "> `font-sync-full.zip` 是带测试与运行时的调试包，普通使用装 `font-sync-<版本>.zip`。",
        "",
    ]
    section = changelog_section()
    if section:
        # 发布页正文里整体降一级标题（## 日期 → ### 日期，### 修复 → #### 修复），层级才顺
        demoted = "\n".join(
            "#" + line if line.startswith("#") else line for line in section.splitlines()
        )
        parts += ["---", "", demoted, ""]
    parts += [
        "---",
        "",
        "插件开发规范：[docs/PLUGINS.md](https://github.com/706972745/FontManager/blob/main/docs/PLUGINS.md)",
        "· 主程序：[706972745/FontManager](https://github.com/706972745/FontManager)",
        "",
    ]
    out = os.path.join(OUT, "release-notes.md")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(parts))
    print(f"release-notes.md {os.path.getsize(out)} B")
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    made = []

    fs_zips, fs_version = zip_font_sync(OUT)
    for p in fs_zips:
        verify(p, FONT_SYNC, fs_version)
        made.append(p)
    downloads = {FONT_SYNC: f"font-sync-{fs_version}.zip"}

    versions = {FONT_SYNC: fs_version}
    for plugin in FULL_DIR_PLUGINS:
        out, version, count = zip_plugin_dir(plugin, OUT)
        verify(out, plugin, version)
        versions[plugin] = version
        downloads[plugin] = f"{plugin}.zip"
        made.append(out)
        print(f"{plugin}.zip {os.path.getsize(out) / 1048576:.1f} MB（{count} 个文件，v{version}）")

    for p in made:
        print(f"  {os.path.basename(p):28s} {os.path.getsize(p):>10d} B")
    write_release_notes(downloads)
    print("版本：" + "、".join(f"{k} {v}" for k, v in versions.items()))
    print("打包自检通过")


if __name__ == "__main__":
    main()
