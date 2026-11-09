"""0.8.14 回归：大包上传的三条风险 —— 断线重试、502 空壳、首次全量太大。

背景（用户 2026-11-09 问「这些风险能不能修」）：
1. 包断了要等下一轮重传 —— GitHub 不支持断点续传，能做的是**整包自动重传**
   （`_upload_blob` 再兜一次）与 **socket 中断重试**（`gh.request` 补 OSError /
   http.client 异常；以前 ConnectionResetError 会直接穿透成「同步失败」）。
2. 502 会留下 `state=starter` 的空 asset，下次同名上传撞 422 会被当「已传过」，
   远端留一个 0 字节坏包 —— 现在失败后自动清空壳。
3. 首次全量 6.85 GB —— 字体改用 DEFLATE（实测小族省四成、大族省近两成），
   同时把哈希改成**内容哈希**，这样压缩方式/机器不同也不会让幂等失效。
"""

import hashlib
import os
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
TESTS = os.path.dirname(os.path.abspath(__file__))
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

import gh as ghmod  # noqa: E402
import packer  # noqa: E402
import syncer  # noqa: E402

from test_core import make_tree  # noqa: E402


class ContentHashTests(unittest.TestCase):
    def test_hash_is_computed_from_content_not_zip_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            files = make_tree(d, {"a/T.ttf": 4096, "a/OFL.txt": 64})
            meta = packer.pack_family(os.path.join(d, "x.zip"), [files["a/T.ttf"]],
                                      docs=[files["a/OFL.txt"]], family="F")
            expect = packer.content_hash(
                "F",
                [("T.ttf", packer.sha256_path(files["a/T.ttf"]))],
                [("OFL.txt", packer.sha256_path(files["a/OFL.txt"]))])
            self.assertEqual(meta["hash"], expect)

    def test_hash_stable_across_repack(self):
        with tempfile.TemporaryDirectory() as d:
            files = make_tree(d, {"a/T.ttf": 2048})
            m1 = packer.pack_family(os.path.join(d, "1.zip"), [files["a/T.ttf"]], family="F")
            m2 = packer.pack_family(os.path.join(d, "2.zip"), [files["a/T.ttf"]], family="F")
            self.assertEqual(m1["hash"], m2["hash"])

    def test_doc_content_change_changes_hash(self):
        """文档也纳入内容哈希（旧版只记文档名字和大小）。"""
        with tempfile.TemporaryDirectory() as d:
            files = make_tree(d, {"a/T.ttf": 512, "a/OFL.txt": 64})
            m1 = packer.pack_family(os.path.join(d, "1.zip"), [files["a/T.ttf"]],
                                    docs=[files["a/OFL.txt"]], family="F")
            with open(files["a/OFL.txt"], "wb") as fh:
                fh.write(b"license changed " * 20)
            m2 = packer.pack_family(os.path.join(d, "2.zip"), [files["a/T.ttf"]],
                                    docs=[files["a/OFL.txt"]], family="F")
            self.assertNotEqual(m1["hash"], m2["hash"])


class DeflateTests(unittest.TestCase):
    def test_fonts_are_deflated(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a", "T.ttf")
            os.makedirs(os.path.dirname(path))
            with open(path, "wb") as fh:
                fh.write(b"A" * (512 * 1024))       # 高度可压
            zip_path = os.path.join(d, "x.zip")
            meta = packer.pack_family(zip_path, [path], family="F")
            with zipfile.ZipFile(zip_path) as zf:
                info = zf.getinfo("T.ttf")
                self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)
            self.assertLess(meta["size"], 200 * 1024, "字体要真的被压小")
            self.assertEqual(meta["rawSize"], 512 * 1024)

    def test_round_trip_identical_after_deflate(self):
        with tempfile.TemporaryDirectory() as d:
            src = make_tree(os.path.join(d, "src"), {"a/T.ttf": 8192})
            zip_path = os.path.join(d, "x.zip")
            packer.pack_family(zip_path, [src["a/T.ttf"]], family="F")
            out = os.path.join(d, "out")
            packer.unpack_family(zip_path, out)
            with open(src["a/T.ttf"], "rb") as a, open(os.path.join(out, "T.ttf"), "rb") as b:
                self.assertEqual(hashlib.sha256(a.read()).hexdigest(),
                                 hashlib.sha256(b.read()).hexdigest())


class _FakeGh:
    def __init__(self, fail_times=0, purged=0):
        self.fail_times = fail_times
        self.calls = 0
        self.purged = purged
        self.purge_called = 0

    def upload_asset(self, owner, name, release_id, asset, blob, timeout=600):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ghmod.GhError(0, "连接被重置")
        return {"id": 7}

    def purge_empty_assets(self, owner, name, release_id, asset_name=""):
        self.purge_called += 1
        return self.purged


class UploadRetryTests(unittest.TestCase):
    def setUp(self):
        self._sleep = syncer.time.sleep

    def tearDown(self):
        syncer.time.sleep = self._sleep

    def test_retries_once_then_succeeds(self):
        syncer.time.sleep = lambda s: None
        fake = _FakeGh(fail_times=1)
        logs = []
        out = syncer._upload_blob(fake, "o", "r", 1, "a.zip", b"x", log=logs.append)
        self.assertEqual(out, {"id": 7})
        self.assertEqual(fake.calls, 2)
        self.assertTrue(any("重传" in line for line in logs), logs)

    def test_purges_starter_after_final_failure(self):
        syncer.time.sleep = lambda s: None
        fake = _FakeGh(fail_times=9, purged=1)
        logs = []
        with self.assertRaises(ghmod.GhError):
            syncer._upload_blob(fake, "o", "r", 1, "a.zip", b"x", log=logs.append)
        self.assertEqual(fake.purge_called, 1, "最终失败后必须清空壳 asset")
        self.assertTrue(any("空壳" in line for line in logs), logs)


class _Resp:
    status = 200

    def __init__(self, body=b'{"ok": 1}'):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class SocketRetryTests(unittest.TestCase):
    """传大包断在半路 = ConnectionResetError，以前会直接穿透成「同步失败」。"""

    def setUp(self):
        self._sleep = ghmod.time.sleep
        ghmod.time.sleep = lambda s: None

    def tearDown(self):
        ghmod.time.sleep = self._sleep

    def test_connection_reset_is_retried(self):
        client = ghmod.Gh("tok")
        calls = {"n": 0}

        def flaky(req, timeout):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接")
            return _Resp()

        client._open = flaky
        status, data = client.request("POST", "https://uploads.github.com/x", data=b"blob")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": 1})
        self.assertEqual(calls["n"], 3)

    def test_incomplete_read_is_retried(self):
        client = ghmod.Gh("tok")
        calls = {"n": 0}

        def flaky(req, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ghmod.http.client.IncompleteRead(b"partial", 100)
            return _Resp()

        client._open = flaky
        status, _ = client.request("POST", "https://uploads.github.com/x", data=b"blob")
        self.assertEqual(status, 200)
        self.assertEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
