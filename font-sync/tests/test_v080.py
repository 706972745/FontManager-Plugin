"""0.8.0 回归：GitHub asset 名改写（`!`→`.`）兼容、保险丝提示去重、补缺匹配。

背景（2026-11-07 实测）：GitHub 会把 release asset 名里的 `!` 改写成 `.`，
传 `Excl_!_Test__….zip` 存回 `Excl_._Test__…`。所以远端可能同时存在
`Far_Out.`（被改写的老包）与 `Far_Out`（0.8.0 起直接避开）——匹配必须归一。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
TESTS = os.path.dirname(os.path.abspath(__file__))
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

import packer  # noqa: E402
import store  # noqa: E402
import syncer  # noqa: E402

from test_core import FakeApi, FakeSyncGh, make_tree  # noqa: E402


class SlugCompatTests(unittest.TestCase):
    """命名（slugify）从此避开 `!`；匹配（slug_key）把历史形态归一。"""

    def test_slugify_avoids_bang(self):
        self.assertEqual(packer.slugify("Far Out!"), "Far_Out")
        self.assertEqual(packer.slugify("!Y2KBUG"), "Y2KBUG")
        self.assertNotIn("!", packer.slugify("AmazObitaemOstrovCrazy!"))
        self.assertEqual(packer.slugify("中文字族!"), "中文字族", "中文要保留")

    def test_slug_key_unifies_all_history_forms(self):
        forms = ["Far Out!", "Far Out.", "Far_Out", "Far Out", "Far_Out!"]
        self.assertEqual(len({packer.slug_key(x) for x in forms}), 1)

    def test_slug_key_collapses_mixed_separators(self):
        self.assertEqual(packer.slug_key("Wow!_Font"), packer.slug_key("Wow._Font"))
        self.assertEqual(packer.slug_key("Wow  Font"), packer.slug_key("Wow_Font"))
        self.assertNotEqual(packer.slug_key("Alpha"), packer.slug_key("Beta"))

    def test_parse_asset_name_still_strict(self):
        self.assertEqual(packer.parse_asset_name("Far_Out.__df93992d6d76.zip"),
                         ("Far_Out.", "df93992d6d76"))
        self.assertEqual(packer.parse_asset_name("Far_Out!__df93992d6d76.zip"),
                         ("Far_Out!", "df93992d6d76"))
        self.assertEqual(packer.parse_asset_name("not-ours.zip"), (None, None))


class FindAssetIdCompatTests(unittest.TestCase):
    """撞 422 后按名字找回 id：名字被 GitHub 改写也要找得到。"""

    class Gh:
        def __init__(self, assets):
            self._assets = assets

        def list_assets(self, owner, name, release_id):
            return self._assets

    def test_finds_rewritten_name(self):
        gh = self.Gh([{"name": "Far_Out.__df93992d6d76.zip", "id": 555}])
        self.assertEqual(
            syncer._find_asset_id(gh, "o", "r", 1, "Far_Out!__df93992d6d76.zip"), 555)

    def test_digest_must_match(self):
        gh = self.Gh([{"name": "Far_Out.__aaaaaaaaaaaa.zip", "id": 555}])
        self.assertIsNone(
            syncer._find_asset_id(gh, "o", "r", 1, "Far_Out!__df93992d6d76.zip"))

    def test_finds_exact_name(self):
        gh = self.Gh([{"name": "Alpha__111111111111.zip", "id": 7}])
        self.assertEqual(
            syncer._find_asset_id(gh, "o", "r", 1, "Alpha__111111111111.zip"), 7)


class RestoreMatchTests(unittest.TestCase):
    """恢复比对：改写过名字的老包要认出「本地已有」；only 支持列表。"""

    ROW = {"slug": "Far_Out.", "asset": "Far_Out.__df93992d6d76.zip",
           "assetId": 7, "size": 10, "tag": "lib-0001"}

    def test_rewritten_asset_is_recognized_as_local(self):
        items, skipped = syncer.plan_restore([self.ROW], ["Far Out!"], overwrite=False)
        self.assertEqual(items, [], "老包名字带点，本地族名带叹号 —— 也必须认成同一个族")
        self.assertEqual([s["family"] for s in skipped], ["Far Out!"])

    def test_overwrite_ignores_local(self):
        items, _ = syncer.plan_restore([self.ROW], ["Far Out!"], overwrite=True)
        self.assertEqual(len(items), 1)

    def test_only_accepts_list(self):
        remote = [dict(self.ROW), {"slug": "Beta", "asset": "Beta__111111111111.zip",
                                   "assetId": 8, "size": 10, "tag": "lib-0001"}]
        items, _ = syncer.plan_restore(remote, [], only=["Far Out!"])
        self.assertEqual([i["slug"] for i in items], ["Far_Out."])
        items, _ = syncer.plan_restore(remote, [], only="Beta")
        self.assertEqual([i["slug"] for i in items], ["Beta"])
        items, _ = syncer.plan_restore(remote, [], only=None)
        self.assertEqual(len(items), 2)


class FuseDedupTests(unittest.TestCase):
    """FS-2：保险丝拦下后，同一批缺席不再每轮复读；清单恢复后解除。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        layout = {f"f{i:02d}/F{i:02d}.ttf": 512 for i in range(30)}
        layout.update({"a/A.ttf": 512, "b/B.ttf": 512, "c/C.ttf": 512,
                       "d/D.ttf": 512, "e/E.ttf": 512})
        self.files = make_tree(self.tmp.name, layout)
        # 族名 → 库内文件路径
        self.fam_file = {f"f{i:02d}": f"f{i:02d}/F{i:02d}.ttf" for i in range(30)}
        self.fam_file.update({"a": "a/A.ttf", "b": "b/B.ttf", "c": "c/C.ttf",
                              "d": "d/D.ttf", "e": "e/E.ttf"})
        self.cfg = {"sync": {"excludeCopyright": [], "maxFamilyBytes": 10 << 20,
                             "deleteRemovedFamilies": True, "deleteAfterDays": 7}}
        self.state = {"deviceId": "dev", "families": {}, "pendingDeletes": []}
        self.old_state_path = store.STATE_PATH
        store.STATE_PATH = os.path.join(self.tmp.name, "state.json")
        self.old_ensure = syncer.ensure_repo
        syncer.ensure_repo = lambda *a, **k: ("owner", "repo", "main")
        self._prime()

    def tearDown(self):
        syncer.ensure_repo = self.old_ensure
        store.STATE_PATH = self.old_state_path
        self.tmp.cleanup()

    def _prime(self):
        """让全部 35 个族处于「已同步且没变化」。"""
        faces = [{"path": self.files[rel], "family": fam, "copyrightClass": "free"}
                 for fam, rel in self.fam_file.items()]
        todo, _ = syncer.build_plan(FakeApi(faces), self.cfg, self.state,
                                    log=lambda *_: None)
        for i, item in enumerate(todo):
            self.state["families"][item["family"]] = {
                "fingerprint": item["fingerprint"], "hash": "h", "assetId": 100 + i,
                "asset": item["family"] + "__x.zip", "bucket": "lib-0001",
                "size": 1, "fonts": 1, "docs": 0}

    def _run(self, keys, logs):
        api = FakeApi([{"path": self.files[self.fam_file[k]], "family": k,
                        "copyrightClass": "free"} for k in keys])
        return syncer.run_sync(FakeSyncGh(), api, self.cfg, self.state,
                               log=logs.append)

    def test_second_identical_round_gets_short_log(self):
        partial = ["a", "b", "c", "d", "e"]           # 35 个族只见回 5 个 → 保险丝
        logs1 = []
        self._run(partial, logs1)
        self.assertIn("lastGone", self.state, "保险丝触发要记下这一批缺席")
        self.assertTrue(any("跳过删除" in m for m in logs1))
        logs2 = []
        self._run(partial, logs2)
        self.assertTrue(any("与上一轮完全相同" in m for m in logs2),
                        "同一批缺席，第二轮该用短句")
        self.assertFalse(any("跳过删除" in m for m in logs2))

    def test_recovery_clears_memory(self):
        partial = ["a", "b", "c", "d", "e"]
        self._run(partial, [])
        self.assertIn("lastGone", self.state)
        self._run(sorted(self.fam_file), [])          # 清单恢复 → gone 为空
        self.assertNotIn("lastGone", self.state, "恢复后要去重记忆清掉")

    def test_nothing_is_deleted_while_fused(self):
        gh = FakeSyncGh()
        summary = self._run(["a", "b", "c", "d", "e"], [])
        self.assertEqual(gh.deleted, [])
        self.assertEqual(summary["removed"], [])


class DispatchTests(unittest.TestCase):
    """面板通用动作入口的最小校验（不联网、不碰面板服务）。"""

    def test_restore_missing_requires_families(self):
        import main as fs_main
        self.assertFalse(fs_main.dispatch_action("restoreMissing", {"families": []})["ok"])
        self.assertFalse(fs_main.dispatch_action("restoreMissing", {})["ok"])

    def test_unknown_action_rejected(self):
        import main as fs_main
        self.assertFalse(fs_main.dispatch_action("nope", {})["ok"])


if __name__ == "__main__":
    unittest.main()
