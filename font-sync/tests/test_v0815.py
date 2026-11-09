"""0.8.15：单族同步不吃「指纹闸」（强制重传）。

背景（用户 2026-11-09 指出）：判断「变没变」的第一道闸是
`fingerprint` = 排序后的 `路径|字节大小|mtime`，只 stat、不读内容。
如果字体被换成**字节数完全一样、mtime 又停在同一个秒**的另一版
（例如替换成同规格的子集/完整版），指纹一模一样 —— 右键「同步这个字族」
会被判「未变化」跳过，远端永远是旧的，而且**没有任何入口能解套**。

修法：单族同步是用户点名的意图，`build_plan(force=True)` 跳过第一道闸，
仍然由第二道闸（打包后的内容哈希）决定传不传 —— 只是白打一次包。
"""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import store  # noqa: E402
import syncer  # noqa: E402


class FakeApi:
    def __init__(self, faces):
        self._faces = faces

    def faces(self, page=2000):
        return list(self._faces)


class FakeGh:
    """run_sync 用得到的那几个方法。"""

    def __init__(self):
        self.uploads, self.deleted, self.commits = [], [], []

    def find_or_create_bucket(self, owner, name):
        return 1, "lib-0001"

    def upload_asset(self, owner, name, release_id, asset, blob):
        self.uploads.append(asset)
        return {"id": 1000 + len(self.uploads)}

    def delete_asset(self, owner, name, asset_id):
        self.deleted.append(asset_id)

    def commit_files(self, owner, name, branch, files, message, retries=6):
        self.commits.append((dict(files), message))


def write_font(path, size, fill=b"A"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(fill * size)
    return path


class ForceResyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.font = write_font(os.path.join(self.root, "alpha", "A-Regular.ttf"), 4096)
        self.api = FakeApi([{"path": self.font, "family": "Alpha",
                             "copyrightClass": "free"}])
        self.cfg = {"sync": {"excludeCopyright": [], "maxFamilyBytes": 10 << 20,
                             "deleteRemovedFamilies": True, "deleteAfterDays": 7,
                             "batchFamilies": 100}}
        self.state = {"deviceId": "dev-test", "families": {}, "pendingDeletes": [],
                      "lastSyncAt": 0}
        self._old_state_path = store.STATE_PATH
        self._old_ensure = syncer.ensure_repo
        store.STATE_PATH = os.path.join(self.root, "state.json")
        syncer.ensure_repo = lambda *a, **k: ("owner", "repo", "main")
        self.logs = []

    def tearDown(self):
        syncer.ensure_repo = self._old_ensure
        store.STATE_PATH = self._old_state_path
        self.tmp.cleanup()

    def _prime_unchanged(self):
        """把 Alpha 记成「已同步且没变化」。"""
        todo, _ = syncer.build_plan(self.api, self.cfg, self.state, log=lambda *_: None)
        item = todo[0]
        self.state["families"]["Alpha"] = {"fingerprint": item["fingerprint"],
                                           "hash": "h" * 64, "assetId": 7,
                                           "asset": "Alpha__000000000000.zip"}
        return item["fingerprint"]

    # ── 闸 1 的行为 ────────────────────────────────────────────────────
    def test_force_false_still_skips_unchanged(self):
        self._prime_unchanged()
        todo, skipped = syncer.build_plan(self.api, self.cfg, self.state,
                                          log=lambda *_: None)
        self.assertEqual(todo, [], "指纹没变就该跳过（这是正常路径，不能被改坏）")
        self.assertEqual(skipped["unchanged"], 1)

    def test_force_true_enters_packing_even_when_fingerprint_matches(self):
        self._prime_unchanged()
        todo, _ = syncer.build_plan(self.api, self.cfg, self.state,
                                    log=lambda *_: None, force=True)
        self.assertEqual([t["family"] for t in todo], ["Alpha"],
                         "force=True 必须把族放进 todo，交给内容哈希判定")

    def test_only_family_implies_force(self):
        """右键单族同步必须走强制通道 —— 这是用户点名的意图。"""
        self._prime_unchanged()
        gh = FakeGh()
        syncer.run_sync(gh, self.api, self.cfg, self.state,
                        only_family="Alpha", log=self.logs.append)
        self.assertEqual(len(gh.uploads), 1,
                         "单族同步必须真的走一次打包+上传判定，不能被闸 1 挡掉")

    # ── 真正的 bug 复现：等长替换 + mtime 冻结 ─────────────────────────
    def test_same_size_same_mtime_swap_is_uploaded_by_single_family_sync(self):
        gh = FakeGh()
        syncer.run_sync(gh, self.api, self.cfg, self.state,
                        only_family="Alpha", log=self.logs.append)
        self.assertEqual(len(gh.uploads), 1)
        first_asset = gh.uploads[0]
        first_hash = self.state["families"]["Alpha"]["hash"]

        st = os.stat(self.font)
        # 换成另一版：字节数完全一样、内容不同、mtime 复原到原值
        write_font(self.font, st.st_size, fill=b"B")
        os.utime(self.font, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(os.stat(self.font).st_size, st.st_size)
        self.assertEqual(int(os.stat(self.font).st_mtime), int(st.st_mtime),
                         "前提：指纹确实没变")

        fp_now = syncer.fingerprint([{"path": self.font}])
        self.assertEqual(fp_now, self.state["families"]["Alpha"]["fingerprint"],
                         "前提：指纹逐位相同，旧逻辑到这里就跳过了")

        syncer.run_sync(gh, self.api, self.cfg, self.state,
                        only_family="Alpha", log=self.logs.append)
        self.assertEqual(len(gh.uploads), 2, "等长替换也必须重新上传")
        self.assertNotEqual(gh.uploads[1], first_asset, "内容变了，资产名（hash12）必须变")
        self.assertNotEqual(self.state["families"]["Alpha"]["hash"], first_hash,
                            "账目里的内容哈希要更新")
        self.assertEqual(self.state["families"]["Alpha"]["assetId"], 1002)

    def test_full_sync_keeps_fingerprint_gate(self):
        """全量同步仍吃指纹闸：这是速度与正确性的既有取舍，别被 force 顺手改掉。"""
        gh = FakeGh()
        syncer.run_sync(gh, self.api, self.cfg, self.state, log=self.logs.append)
        st = os.stat(self.font)
        write_font(self.font, st.st_size, fill=b"B")
        os.utime(self.font, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.logs.clear()
        summary = syncer.run_sync(gh, self.api, self.cfg, self.state,
                                  log=self.logs.append)
        self.assertEqual(len(gh.uploads), 1, "全量同步下指纹没变就是跳过")
        self.assertEqual(summary["skipped"]["unchanged"], 1)

    # ── 闸 2 不能被绕过 ───────────────────────────────────────────────
    def test_force_still_blocks_when_content_really_unchanged(self):
        gh = FakeGh()
        syncer.run_sync(gh, self.api, self.cfg, self.state,
                        only_family="Alpha", log=self.logs.append)
        self.logs.clear()
        syncer.run_sync(gh, self.api, self.cfg, self.state,
                        only_family="Alpha", log=self.logs.append)
        self.assertEqual(len(gh.uploads), 1,
                         "内容真没变时只该白打一次包，不许重复上传")
        self.assertTrue(any("内容未变" in m for m in self.logs),
                        f"要明说为什么没传：{self.logs}")
        # 账目里的哈希/资产名不能因为重扫而被清掉
        self.assertEqual(self.state["families"]["Alpha"]["assetId"], 1001)

    def test_force_does_not_bypass_license_or_size_limits(self):
        self._prime_unchanged()
        cfg = {"sync": {"excludeCopyright": ["free"], "maxFamilyBytes": 1024,
                        "deleteRemovedFamilies": True, "deleteAfterDays": 7}}
        todo, skipped = syncer.build_plan(self.api, cfg, self.state,
                                          log=lambda *_: None, force=True)
        self.assertEqual(todo, [], "force 只跳指纹闸，版权/体积规则照旧生效")
        self.assertEqual(skipped["license"], ["Alpha"])


if __name__ == "__main__":
    unittest.main()
