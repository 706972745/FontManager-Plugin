"""按字族打 zip（确定性）+ 算哈希。

为什么「确定性」很重要：两台机器各自打同一个字族的包，只要内容一样，
字节就必须一样、哈希就必须一样 —— 否则每次同步都会重传，缓存与幂等全部失效。
所以这里固定：条目排序、时间戳写死 1980-01-01、字体用 STORED（字体本来就压不动）、
文档用 DEFLATE、权限位写死。
"""

import hashlib
import os
import re
import zipfile

# 同目录里算「授权文档」的文件名（小写前缀匹配）
DOC_PREFIXES = (
    "license", "licence", "ofl", "copying", "copyright", "readme",
    "授权", "版权", "说明", "许可",
)
DOC_EXT = (".txt", ".md", ".html", ".htm", ".pdf", ".rtf")
FIXED_DATE = (1980, 1, 1, 0, 0, 0)
ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def slugify(family):
    """asset 名里用的 slug：保留中文，只清掉文件系统/URL 会炸的字符。"""
    name = ILLEGAL.sub("_", (family or "").strip())
    name = re.sub(r"\s+", "_", name).strip("._")
    return (name or "family")[:60]


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


def pack_family(dest_path, font_paths, docs=None):
    """把一族字体 + 文档打成 zip 写到 dest_path，返回信息字典。"""
    docs = docs if docs is not None else collect_docs(font_paths)
    fonts = sorted(set(font_paths), key=lambda s: s.lower())
    docs = sorted(set(docs), key=lambda s: s.lower())

    entries = []
    for p, arc in zip(fonts, _arcnames(fonts)):
        entries.append((arc, p, zipfile.ZIP_STORED))
    for p, arc in zip(docs, _arcnames(docs)):
        if arc.lower().endswith((".txt", ".md", ".html", ".htm")):
            entries.append(("docs/" + arc, p, zipfile.ZIP_DEFLATED))

    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    with zipfile.ZipFile(dest_path, "w", allowZip64=True) as zf:
        for arc, src, method in entries:
            info = zipfile.ZipInfo(arc, date_time=FIXED_DATE)
            info.compress_type = method
            info.create_system = 3
            info.external_attr = 0o644 << 16
            with open(src, "rb") as fh:
                data = fh.read()
            zf.writestr(info, data)

    digest = hashlib.sha256()
    size = 0
    with open(dest_path, "rb") as fh:
        while True:
            block = fh.read(1 << 20)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return {
        "hash": digest.hexdigest(),
        "size": size,
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
