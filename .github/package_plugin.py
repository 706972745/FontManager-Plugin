# -*- coding: utf-8 -*-
"""打包插件 zip（CI 与本地共用同一套清单）。

产物（写入仓库根的 dist/）：
- font-sync.zip       纯代码（压缩），普通用户装这个
- font-sync-full.zip  代码 + tests + runtime（不压缩），调试/离线用
"""
import json
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "font-sync")
OUT = os.path.join(ROOT, "dist")

CODE = ["plugin.json", "main.py", "panel.py", "panel.html", "gh.py", "packer.py",
        "store.py", "syncer.py", "README.md"]


def main():
    for f in CODE:
        p = os.path.join(SRC, f)
        if not os.path.isfile(p):
            sys.exit(f"缺少代码文件：{f}")
    version = json.load(open(os.path.join(SRC, "plugin.json"), encoding="utf-8"))["version"]

    os.makedirs(OUT, exist_ok=True)
    zp = os.path.join(OUT, "font-sync.zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in CODE:
            z.write(os.path.join(SRC, f), f)
    zf = os.path.join(OUT, "font-sync-full.zip")
    with zipfile.ZipFile(zf, "w", zipfile.ZIP_STORED) as z:
        for f in CODE:
            z.write(os.path.join(SRC, f), f)
        for base in ("tests", "runtime"):
            for root, _dirs, files in os.walk(os.path.join(SRC, base)):
                for f in files:
                    p = os.path.join(root, f)
                    z.write(p, os.path.relpath(p, SRC))

    # 自检：两个包都能完整解开，且 plugin.json 版本一致
    for path in (zp, zf):
        with zipfile.ZipFile(path) as z:
            if z.testzip() is not None:
                sys.exit(f"{os.path.basename(path)} 有损坏条目")
            v = json.loads(z.read("plugin.json").decode("utf-8"))["version"]
            assert v == version, f"{os.path.basename(path)} 版本不一致：{v}"
    print(f"font-sync.zip       {os.path.getsize(zp)} B")
    print(f"font-sync-full.zip  {os.path.getsize(zf)} B")
    print(f"版本 {version}，打包自检通过")


if __name__ == "__main__":
    main()
