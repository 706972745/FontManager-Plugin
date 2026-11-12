"""0.9.3：打包白名单防复发单测 —— 本地模块一个都不能漏。

0.9.2 的现场故障：`meta.py` 源码里有、`syncer.py:20` 里也 import 了，但两份打包
白名单都没列它。CI 打出来的包里缺这个文件，装到用户机器上就是

    main.py → import panel → import syncer → import meta
    ModuleNotFoundError: No module named 'meta'

激活失败，主程序于是显示「插件没在运行」，而真正的原因只留在
`%APPDATA%\\FontManager\\activation.log` 里，用户根本看不到。

这条测试把「源码能 import」和「打包会带上」绑在一起：用 `ast` 解析
`font-sync/*.py` 里的每一句 `import x` / `from x import y`，取出模块名，筛掉
标准库和第三方库（判据：与本目录某个 `.py` 同名即为本地模块），然后断言本地
模块**每一个**都出现在两份白名单里：

- `.github/package_plugin.py` 的 `FONT_SYNC_CODE`（CI 的 plugin-release.yml 用它打包）
- `font-sync/build_zip.py` 的 `FILES`（本地给同事打包用）

两份清单都不 `import`：`package_plugin.py` 顶层会建目录写文件、`build_zip.py`
顶层要解析命令行参数，import 一下就可能跑起来。这里只用 `ast` 读字面量列表，
所以清单要是改成了「运行时拼出来的」，测试会直接 fail 提醒去改测试本身。
"""

import ast
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # font-sync/
ROOT = os.path.dirname(HERE)  # 仓库根
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 两份白名单：(给人看的说明, 文件路径, 变量名)
MANIFESTS = (
    ("CI 打包清单", os.path.join(ROOT, ".github", "package_plugin.py"), "FONT_SYNC_CODE"),
    ("本地打包清单", os.path.join(HERE, "build_zip.py"), "FILES"),
)

def local_module_names():
    """font-sync 目录下所有 `.py` 的模块名集合（文件名去掉后缀）。"""
    return {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}


def imported_module_names(path):
    """AST 解析一个 .py，返回它 import 过的顶层模块名（去重、排序）。

    只取相对 import 之外的第一段（`from a.b import c` → `a`）；相对 import
    （`from . import x`，level > 0）在插件里不会出现，出现也不该参与比对。
    """
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return sorted(names)


def local_imports():
    """fonts-sync/*.py 里 import 到的本地模块名集合（这些必须进包）。"""
    local = local_module_names()
    needed = set()
    for name in sorted(os.listdir(HERE)):
        if not name.endswith(".py"):
            continue
        needed.update(set(imported_module_names(os.path.join(HERE, name))) & local)
    return needed


def literal_list(path, var_name):
    """从源码里读一个「顶层 `var_name = [...]`」的字符串列表字面量。

    读不到（文件不在、变量改名、值不是字面量列表）返回 None —— 调用方据此
    fail，而不是让测试静默通过。
    """
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        try:
            tree = ast.parse(fh.read(), path)
        except SyntaxError:
            return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not (isinstance(target, ast.Name) and target.id == var_name):
                continue
            if not isinstance(node.value, ast.List):
                return None
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                return None
            if not all(isinstance(x, str) for x in value):
                return None
            return value
    return None


class PackagingWhitelist(unittest.TestCase):
    """本地模块 → 两份打包白名单，一个都不能漏。"""

    def test_reads_both_whitelists(self):
        """先确认两份清单都能按约定的格式读到，否则后面两条断言没有意义。"""
        for label, path, var in MANIFESTS:
            with self.subTest(manifest=label):
                files = literal_list(path, var)
                if files is None:
                    self.fail(
                        f"{label}格式变了，改这条测试：在 {os.path.relpath(path, ROOT)} 里"
                        f"找不到顶层字面量列表 `{var} = [...]`。"
                        f"两处白名单是 font-sync 的打包事实来源，必须能被静态读到。"
                    )
                self.assertTrue(files, f"{label} 的 {var} 是空列表，font-sync 会打成空包")

    def test_scan_finds_local_modules(self):
        """AST 扫描本身要能扫出东西，避免「扫了个寂寞但全绿」。"""
        needed = local_imports()
        self.assertTrue(
            needed,
            "没在 font-sync/*.py 里扫到任何本地 import —— "
            "可能是插件改成了单文件、或这条测试的扫描逻辑坏了，改这条测试。",
        )
        # 这几个是 0.9.x 的核心模块，扫不到基本就是扫描坏了
        for must in ("gh", "panel", "store", "syncer"):
            self.assertIn(must, needed, f"扫描漏了 {must}，检查 imported_module_names()")

    def test_every_local_module_is_packaged(self):
        """每个被 import 的本地模块，都必须在两份白名单里。"""
        needed = local_imports()
        missing_report = []
        for label, path, var in MANIFESTS:
            files = literal_list(path, var)
            if files is None:  # 由 test_reads_both_whitelists 报具体原因，这里跳过
                self.fail(
                    f"{label}格式变了，改这条测试：读不到 "
                    f"{os.path.relpath(path, ROOT)} 的 `{var}`。"
                )
            packaged = {f for f in files}
            for module in sorted(needed):
                filename = f"{module}.py"
                if filename in packaged:
                    continue
                missing_report.append(
                    f"缺模块 `{module}` → 把 \"{filename}\" 加进 {label}"
                    f"（{os.path.relpath(path, ROOT)} 的 {var}），"
                    f"建议位置：store.py 之后、syncer.py 之前"
                )
        if missing_report:
            self.fail(
                "有本地模块没进打包白名单，装到用户机器上会 ModuleNotFoundError"
                "（主程序只会显示「插件没在运行」）：\n  - " + "\n  - ".join(missing_report)
            )

    def test_nothing_extra_needed(self):
        """反向核对：白名单里的 .py 本地模块都得真实存在，别写了个拼错的名字。"""
        local = local_module_names()
        for label, path, var in MANIFESTS:
            files = literal_list(path, var)
            if files is None:
                self.fail(
                    f"{label}格式变了，改这条测试：读不到 "
                    f"{os.path.relpath(path, ROOT)} 的 `{var}`。"
                )
            for filename in files:
                if not filename.endswith(".py"):
                    continue
                with self.subTest(manifest=label, file=filename):
                    self.assertIn(
                        filename[:-3],
                        local,
                        f"{label}里列的 {filename} 在 font-sync/ 下不存在（拼错了？）",
                    )


if __name__ == "__main__":
    unittest.main()
