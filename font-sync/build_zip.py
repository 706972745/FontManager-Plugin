"""打包成可直接发给同事的 ZIP（不依赖 GitHub、不依赖装 Python）。

两种包：

    python build_zip.py                     # 精简包（~100 KB）：机器上要有 Python 3.10+
    python build_zip.py --with-runtime      # 自带运行时（~20 MB 压缩后）：解压就能用

自带运行时会从同仓的其它插件里挑一个现成的 `runtime/`（Python 3.13 便携版），
并在打包出来的 `plugin.json` 里补 `interpreterPath` —— 源目录里的 plugin.json 保持干净。

**绝不会**打进包里的东西：`secret.bin`（令牌！）、`config.json`、`state.json`、
`__pycache__`、`*.pyc`。这里用白名单而不是黑名单，新增文件得自己加进来。
"""

import argparse
import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_NAME = "font-sync"
DATE = (1980, 1, 1, 0, 0, 0)

# 白名单：只有这些进包
FILES = ["main.py", "gh.py", "store.py", "packer.py", "syncer.py", "panel.py",
         "panel.html", "plugin.json", "README.md"]
DIRS = [("tests", (".py",))]


def plugin_version():
    with open(os.path.join(HERE, "plugin.json"), encoding="utf-8") as fh:
        return json.load(fh).get("version") or "0.0.0"


def find_runtime():
    """同仓其它插件里的便携运行时（优先 specimen-export）。"""
    parent = os.path.dirname(HERE)
    for name in ("specimen-export", "font-stats"):
        cand = os.path.join(parent, name, "runtime")
        if os.path.isfile(os.path.join(cand, "python.exe")):
            return cand
    for entry in sorted(os.listdir(parent)):
        cand = os.path.join(parent, entry, "runtime")
        if os.path.isfile(os.path.join(cand, "python.exe")):
            return cand
    return None


def add_bytes(zf, arc, data):
    info = zipfile.ZipInfo(arc, date_time=DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o644 << 16
    zf.writestr(info, data)


def add_file(zf, path, arc, method=zipfile.ZIP_DEFLATED):
    info = zipfile.ZipInfo(arc, date_time=DATE)
    info.compress_type = method
    info.create_system = 3
    info.external_attr = 0o644 << 16
    with open(path, "rb") as fh:
        zf.writestr(info, fh.read())


def build(out_path, with_runtime=False, runtime_dir=None):
    ver = plugin_version()
    runtime = runtime_dir or (find_runtime() if with_runtime else None)
    if with_runtime and not runtime:
        raise SystemExit("没找到现成的 runtime/（同仓其它插件里也没有）")

    manifest = json.load(open(os.path.join(HERE, "plugin.json"), encoding="utf-8"))
    if runtime:
        manifest["interpreterPath"] = "runtime/python.exe"
        manifest["description"] = manifest.get("description", "") + \
            "（自带运行时，机器上不用装 Python）"

    count = 0
    with zipfile.ZipFile(out_path, "w", allowZip64=True) as zf:
        for name in FILES:
            src = os.path.join(HERE, name)
            if not os.path.isfile(src):
                raise SystemExit(f"缺文件：{name}")
            if name == "plugin.json":
                add_bytes(zf, f"{PLUGIN_NAME}/{name}",
                          json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
            else:
                add_file(zf, src, f"{PLUGIN_NAME}/{name}")
            count += 1
        for folder, exts in DIRS:
            base = os.path.join(HERE, folder)
            if not os.path.isdir(base):
                continue
            for root, _dirs, files in os.walk(base):
                for f in sorted(files):
                    if not f.endswith(exts):
                        continue
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, HERE).replace("\\", "/")
                    add_file(zf, full, f"{PLUGIN_NAME}/{rel}")
                    count += 1
        if runtime:
            for root, _dirs, files in os.walk(runtime):
                for f in files:
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, runtime).replace("\\", "/")
                    # 运行时里的 .pyc 不必要，其它都压（dll/exe 也能压掉一半）
                    if rel.lower().endswith((".pyc",)):
                        continue
                    add_file(zf, full, f"{PLUGIN_NAME}/runtime/{rel}")
                    count += 1

    size = os.path.getsize(out_path)
    tag = "自带运行时" if runtime else "精简（需系统 Python 3.10+）"
    print(f"已生成 {out_path}")
    print(f"  版本 {ver} · {tag} · {count} 个条目 · {size/1048576:.2f} MB")
    if runtime:
        print(f"  运行时来自 {runtime}")
    return out_path


def main():
    ap = argparse.ArgumentParser(description="打包 font-sync 插件 ZIP")
    ap.add_argument("-o", "--out", default="")
    ap.add_argument("--with-runtime", action="store_true")
    ap.add_argument("--runtime-dir", default="")
    args = ap.parse_args()

    ver = plugin_version()
    out = args.out or os.path.join(
        os.path.dirname(os.path.dirname(HERE)), "plug-zips-out",
        f"{PLUGIN_NAME}.zip" if not args.with_runtime else f"{PLUGIN_NAME}-full.zip")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    build(out, args.with_runtime, args.runtime_dir or None)
    print(f"（版本号 {ver} 只用于显示；ZIP 文件名固定，方便同事反复下同一个链接）")


if __name__ == "__main__":
    main()
