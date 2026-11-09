"""按字族打 zip（确定性）+ 算哈希。

为什么「确定性」很重要：两台机器各自打同一个字族的包，只要内容一样，
**哈希**就必须一样 —— 否则每次同步都会重传，缓存与幂等全部失效。

0.8.14 起有两处改动，都是为了让「首次全量上传」少花点带宽：

1. **字体改用 DEFLATE 压缩**（原来是 STORED）。实测这台机器的字库：中文大族
   （思源宋体 136 MB）能压到 82%，小族（< 8 MB）平均压到 **60%**，整库 6.85 GB
   大约省下三分之一。代价是打包慢一点（136 MB 约 6 秒）。
2. **哈希改用「内容哈希」**，不再用 zip 字节的哈希。原因：deflate 的输出取决于
   zlib 版本，两台机器可能压出不同字节 —— 那样哈希就会飘，幂等立刻失效。
   内容哈希 = sha256(族名 + 每个文件的「包内名 + sha256」，排序后)，与压缩方式
   无关：同一批文件在哪台机器打、压不压缩，哈希都一样。

包内仍然固定：条目排序、时间戳写死 1980-01-01、权限位写死，保证可复现。
"""

import hashlib
import json
import os
import re
import zipfile

# 包内清单：下载到另一台机器时，靠它还原**真实字族名**与文件原名。
# asset 名里的 slug 是有损的（空格变下划线、非法字符替换、超长截断），
# 只用它当目录名会把「Source Han Sans / SC」这类名字改坏。
MANIFEST = "font-sync.json"

# 同目录里算「授权文档」的文件名（小写前缀匹配）
DOC_PREFIXES = (
    "license", "licence", "ofl", "copying", "copyright", "readme",
    "授权", "版权", "说明", "许可",
)
DOC_EXT = (".txt", ".md", ".html", ".htm", ".pdf", ".rtf")
FIXED_DATE = (1980, 1, 1, 0, 0, 0)
# `!` 必须避开：GitHub 会把 release asset 名里的 `!` 改写成 `.`（2026-11-07 实测：
# 传 `Excl_!_Test__….zip` 存回 `Excl_._Test__…`）。不改写会造成「我们起的名字」和
# 「GitHub 存的名字」对不上 → 恢复匹配失败、撞 422 后取不回 asset id、删族时远端泄漏。
ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f!]+')


def slugify(family):
    """asset 名里用的 slug：保留中文，只清掉文件系统/URL 会炸的字符。"""
    name = ILLEGAL.sub("_", (family or "").strip())
    name = re.sub(r"\s+", "_", name).strip("._")
    return (name or "family")[:60]


def slug_key(s):
    """匹配用的**归一键**（不是命名！命名仍用 slugify）。

    为什么要有它：历史上 GitHub 把 asset 名里的 `!` 改写成 `.`，所以远端可能存在
    `Far_Out.`（老包，被 GitHub 改写过）、`Far_Out!`（更老、侥幸存活的）和
    `Far_Out`（0.8.0 起 slugify 直接避开 `!`）三种形态——它们都是同一个字族
    `Far Out!`。匹配（恢复跳过 / 撞 422 找回 id / 去重）必须把它们看成同一个：
    小写后把所有分隔符（空格 点 叹号 下划线 连字符）压成单个 `_`。
    真正不同的族（`A-B` vs `A_B`）会被并成同一个键——可接受：匹配只用 在
    「恢复要不要跳过 / 撞名找回 id」这类只影响便利性的路径上，删除永远走 assetId。
    """
    k = re.sub(r"[\s.!_\-]+", "_", (s or "").strip().lower())
    return k.strip("_")


def is_doc(name):
    low = name.lower()
    if not low.endswith(DOC_EXT):
        return False
    return any(low.startswith(p) for p in DOC_PREFIXES)


def collect_docs(font_paths):
    """同目录下的授权 / 说明文档 —— 打包时有意义：换台机器能看到授权原话。"""
    seen, docs = set(), []
    for p in font_paths:
        d = os.path.dirname(p)
        try:
            for entry in os.scandir(d):
                if not entry.is_file():
                    continue
                if entry.path in seen or not is_doc(entry.name):
                    continue
                if os.path.getsize(entry.path) > 2 * 1024 * 1024:
                    continue
                seen.add(entry.path)
                docs.append(entry.path)
        except OSError:
            continue
    return docs


def _arcnames(paths):
    """同一目录下可能有同名文件（不同子目录），冲突时补一层目录名。"""
    used, out = {}, []
    for p in paths:
        base = os.path.basename(p)
        key = base.lower()
        if key in used:
            used[key] += 1
            parent = os.path.basename(os.path.dirname(p)) or "dir"
            base = f"{parent}/{base}"
        else:
            used[key] = 0
        out.append(base)
    return out


def content_hash(family, fonts, docs):
    """内容哈希：与压缩方式、时间戳、机器都无关。

    `fonts` = [(包内名, sha256)]，`docs` = [(包内名, sha256)]，两个都已排序。
    族名参与计算：改名就是「另一族」，与旧版本的语义保持一致。
    """
    h = hashlib.sha256()
    h.update(("family\0" + (family or "") + "\n").encode("utf-8"))
    for name, digest in fonts:
        h.update(("font\0" + name + "\0" + digest + "\n").encode("utf-8"))
    for name, digest in docs:
        h.update(("doc\0docs/" + name + "\0" + digest + "\n").encode("utf-8"))
    return h.hexdigest()


def manifest_content_hash(manifest):
    """从包内清单算出内容哈希（下载侧校验用，不需要重新解压全部文件）。"""
    m = manifest or {}
    fonts = [(f.get("name") or "", f.get("sha256") or "")
             for f in (m.get("fonts") or []) if isinstance(f, dict)]
    docs = [(d.get("name") or "", d.get("sha256") or "")
            for d in (m.get("docs") or []) if isinstance(d, dict)]
    return content_hash(str(m.get("family") or ""), fonts, docs)


def verify_package(zip_path):
    """校验下载回来的包，返回内容哈希；不通过就抛 RuntimeError。

    注意：**不要再拿「zip 文件的 sha256」去比对资产名里的 hash** —— 0.8.14 起
    资产名里装的是内容哈希（与压缩方式无关），zip 字节哈希天然对不上。
    完整性靠两件事：`testzip()` 的 CRC 全查（能抓住截断/损坏），
    以及「清单算出的内容哈希 == 资产名里的 hash」。
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()
            if bad:
                raise RuntimeError(f"包内文件损坏（CRC 校验失败）：{bad}")
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"不是完整的 zip（可能下载中断）：{e}") from None
    manifest = read_manifest(zip_path)
    if not manifest:
        raise RuntimeError("包里没有清单，无法确认内容是否完整")
    return manifest_content_hash(manifest)


def pack_family(dest_path, font_paths, docs=None, family=""):
    """把一族字体 + 文档打成 zip 写到 dest_path，返回信息字典。

    `family` 只是写进包内清单的**名字**，不参与内容判定：
    同一个字族在两台机器上打包，只要文件内容和名字一样，哈希就一样。
    所以清单里绝不能放时间戳 / 设备号 / 绝对路径 —— 否则幂等立刻失效。
    """
    docs = docs if docs is not None else collect_docs(font_paths)
    fonts = sorted(set(font_paths), key=lambda s: s.lower())
    docs = sorted(set(docs), key=lambda s: s.lower())

    entries = []
    manifest_fonts = []
    hash_fonts = []
    for p, arc in zip(fonts, _arcnames(fonts)):
        # 字体也压：实测小族能省四成，中文大族省近两成（见模块说明）
        entries.append((arc, p, zipfile.ZIP_DEFLATED))
        digest = sha256_path(p)
        manifest_fonts.append({"name": arc, "size": os.path.getsize(p),
                               "sha256": digest})
        hash_fonts.append((arc, digest))
    manifest_docs = []
    hash_docs = []
    for p, arc in zip(docs, _arcnames(docs)):
        digest = sha256_path(p)
        manifest_docs.append({"name": arc, "size": os.path.getsize(p),
                              "sha256": digest})
        hash_docs.append((arc, digest))
        if arc.lower().endswith((".txt", ".md", ".html", ".htm")):
            entries.append(("docs/" + arc, p, zipfile.ZIP_DEFLATED))

    manifest = {
        "version": 1,
        "family": family or "",
        "fonts": manifest_fonts,
        "docs": manifest_docs,
    }
    blob = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")

    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    with zipfile.ZipFile(dest_path, "w", allowZip64=True) as zf:
        info = zipfile.ZipInfo(MANIFEST, date_time=FIXED_DATE)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = 0o644 << 16
        zf.writestr(info, blob)
        for arc, src, method in entries:
            info = zipfile.ZipInfo(arc, date_time=FIXED_DATE)
            info.compress_type = method
            info.create_system = 3
            info.external_attr = 0o644 << 16
            with open(src, "rb") as fh:
                data = fh.read()
            zf.writestr(info, data)

    size = 0
    with open(dest_path, "rb") as fh:
        while True:
            block = fh.read(1 << 20)
            if not block:
                break
            size += len(block)
    return {
        "hash": content_hash(family or "", hash_fonts, hash_docs),
        "size": size,
        "rawSize": sum(f["size"] for f in manifest_fonts),
        "fonts": len(fonts),
        "docs": len(docs),
    }


def sha256_path(path, chunk=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def asset_name(family, meta):
    return f"{slugify(family)}__{meta['hash'][:12]}.zip"


def parse_asset_name(name):
    """`<slug>__<hash12>.zip` → (slug, hash12)；不是我们发的包就返回 (None, None)。

    只认这个格式：用户手工塞进 release 的其他文件不该被当成字体包下载。
    """
    base = (name or "").strip()
    if not base.lower().endswith(".zip"):
        return None, None
    stem = base[:-4]
    if "__" not in stem:
        return None, None
    slug, digest = stem.rsplit("__", 1)
    if len(digest) != 12 or not all(c in "0123456789abcdef" for c in digest.lower()):
        return None, None
    return (slug or None), digest.lower()


def read_manifest(zip_path):
    """读包内清单；老包（没有清单）返回 None。"""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            raw = zf.read(MANIFEST)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def unpack_family(zip_path, dest_dir, skip_existing=True):
    """把一族的字体解到 dest_dir，文档放 dest_dir/docs/。

    返回 {"fonts": [...], "docs": [...], "skipped": [...], "conflicts": [...]}。

    冲突策略（**宁可留下不覆盖**）：目标位置已有同名文件时
    - 内容一样 → 计入 skipped（重复恢复不该反复写盘）；
    - 内容不一样 → 计入 conflicts，**不覆盖**。覆盖别人的字体是不可逆操作，
      要做也是用户明确点了「覆盖」之后的事。
    """
    out = {"fonts": [], "docs": [], "skipped": [], "conflicts": []}
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            if info.is_dir() or name == MANIFEST:
                continue
            is_doc = name.startswith("docs/")
            rel = name[5:] if is_doc else name
            if not rel or rel.startswith("..") or ":" in rel:
                continue  # 拒绝路径穿越（恶意 / 坏包）
            target_dir = os.path.join(dest_dir, "docs") if is_doc else dest_dir
            os.makedirs(target_dir, exist_ok=True)
            target = os.path.join(target_dir, os.path.basename(rel))
            data = zf.read(info)
            if os.path.exists(target):
                try:
                    with open(target, "rb") as fh:
                        same = fh.read() == data
                except OSError:
                    same = False
                if same:
                    out["skipped"].append(target)
                    continue
                if skip_existing:
                    out["conflicts"].append(target)
                    continue
            with open(target, "wb") as fh:
                fh.write(data)
            out["docs" if is_doc else "fonts"].append(target)
    return out

