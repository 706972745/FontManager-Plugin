"""不依赖主程序、不联网的核心单测。

跑法（插件目录下）：
    python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import packer  # noqa: E402
import store  # noqa: E402
import syncer  # noqa: E402


class FakeApi:
    def __init__(self, faces):
        self._faces = faces

    def faces(self, page=2000):
        return list(self._faces)


def make_tree(root, layout):
    """layout: {相对路径: 字节数}。用随机但确定的内容，便于比哈希。"""
    made = {}
    for rel, size in layout.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write((rel.encode("utf-8") * (size // max(1, len(rel)) + 1))[:size])
        made[rel] = path
    return made


class PackerTests(unittest.TestCase):
    def test_same_content_same_hash(self):
        with tempfile.TemporaryDirectory() as d:
            files = make_tree(d, {"a/T.ttf": 4096, "b/U.otf": 2048})
            paths = list(files.values())
            m1 = packer.pack_family(os.path.join(d, "x1.zip"), paths)
            m2 = packer.pack_family(os.path.join(d, "x2.zip"), paths)
            self.assertEqual(m1["hash"], m2["hash"], "同一批文件两次打包必须字节相同")
            self.assertEqual(m1["fonts"], 2)

    def test_order_does_not_matter(self):
        with tempfile.TemporaryDirectory() as d:
            files = make_tree(d, {"a/T.ttf": 1024, "b/U.otf": 512})
            paths = list(files.values())
            m1 = packer.pack_family(os.path.join(d, "x1.zip"), paths)
            m2 = packer.pack_family(os.path.join(d, "x2.zip"), list(reversed(paths)))
            self.assertEqual(m1["hash"], m2["hash"], "文件顺序不该影响哈希")

    def test_content_change_changes_hash(self):
        with tempfile.TemporaryDirectory() as d:
            files = make_tree(d, {"a/T.ttf": 1024})
            p = files["a/T.ttf"]
            m1 = packer.pack_family(os.path.join(d, "x1.zip"), [p])
            with open(p, "wb") as fh:
                fh.write(b"different content" * 100)
            m2 = packer.pack_family(os.path.join(d, "x2.zip"), [p])
            self.assertNotEqual(m1["hash"], m2["hash"])

    def test_docs_are_picked_up(self):
        with tempfile.TemporaryDirectory() as d:
            files = make_tree(d, {"a/T.ttf": 256, "a/OFL.txt": 128, "a/readme.md": 64,
                                  "a/其他文件.bin": 32})
            docs = packer.collect_docs([files["a/T.ttf"]])
            names = sorted(os.path.basename(p) for p in docs)
            self.assertEqual(names, ["OFL.txt", "readme.md"])
            meta = packer.pack_family(os.path.join(d, "x.zip"), [files["a/T.ttf"]], docs)
            self.assertEqual(meta["docs"], 2)

    def test_same_basename_in_two_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            files = make_tree(d, {"a/T.ttf": 128, "b/T.ttf": 128})
            meta = packer.pack_family(os.path.join(d, "x.zip"), list(files.values()))
            import zipfile
            with zipfile.ZipFile(os.path.join(d, "x.zip")) as zf:
                names = zf.namelist()
            self.assertEqual(len(names), 2, "同名文件不能互相覆盖")
            self.assertEqual(len(set(names)), 2)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.files = make_tree(d, {
            "alpha/A-Regular.ttf": 1024,
            "alpha/A-Bold.ttf": 1024,
            "beta/B.ttf": 2048,
            "weird/W.ttf": 512,
        })
        self.faces = [
            {"path": self.files["alpha/A-Regular.ttf"], "family": "Alpha", "copyrightClass": "free"},
            {"path": self.files["alpha/A-Bold.ttf"], "family": "Alpha", "copyrightClass": "free"},
            {"path": self.files["beta/B.ttf"], "family": "Beta", "copyrightClass": "paid"},
            {"path": self.files["weird/W.ttf"], "family": "Weird", "copyrightClass": "unknown"},
        ]
        self.cfg = {"sync": {"excludeCopyright": ["unknown"],
                             "maxFamilyBytes": 10 * 1024 * 1024}}
        self.state = {"families": {}, "pendingDeletes": []}

    def tearDown(self):
        self.tmp.cleanup()

    def test_unknown_is_excluded_by_default(self):
        todo, skipped = syncer.build_plan(FakeApi(self.faces), self.cfg, self.state, log=lambda *_: None)
        names = sorted(t["family"] for t in todo)
        self.assertEqual(names, ["Alpha", "Beta"])
        self.assertIn("Weird", skipped["license"])

    def test_plan_orders_small_families_first(self):
        todo, _ = syncer.build_plan(FakeApi(self.faces), self.cfg, self.state, log=lambda *_: None)
        sizes = [t["bytes"] for t in todo]
        self.assertEqual(sizes, sorted(sizes), "小族优先，大族不至于一开始就卡住整轮")

    def test_unchanged_family_is_skipped(self):
        todo, _ = syncer.build_plan(FakeApi(self.faces), self.cfg, self.state, log=lambda *_: None)
        alpha = next(t for t in todo if t["family"] == "Alpha")
        self.state["families"]["Alpha"] = {"fingerprint": alpha["fingerprint"], "hash": "x" * 64}
        todo2, skipped2 = syncer.build_plan(FakeApi(self.faces), self.cfg, self.state, log=lambda *_: None)
        self.assertNotIn("Alpha", [t["family"] for t in todo2])
        self.assertEqual(skipped2["unchanged"], 1)

    def test_touch_keeps_fingerprint_stable_but_content_change_does_not(self):
        fp1 = syncer.fingerprint([
            {"path": self.files["beta/B.ttf"]},
        ])
        fp2 = syncer.fingerprint([
            {"path": self.files["beta/B.ttf"]},
        ])
        self.assertEqual(fp1, fp2)
        with open(self.files["beta/B.ttf"], "ab") as fh:
            fh.write(b"x")
        fp3 = syncer.fingerprint([{"path": self.files["beta/B.ttf"]}])
        self.assertNotEqual(fp1, fp3, "内容变了指纹必须变")

    def test_too_big_family_is_reported(self):
        cfg = {"sync": {"excludeCopyright": [], "maxFamilyBytes": 1024}}
        todo, skipped = syncer.build_plan(FakeApi(self.faces), cfg, self.state, log=lambda *_: None)
        big = [f for f, _ in skipped["tooBig"]]
        self.assertIn("Alpha", big)  # 两文件 2048 > 1024
        self.assertNotIn("Alpha", [t["family"] for t in todo])

    def test_single_family_mode(self):
        todo, _ = syncer.build_plan(FakeApi(self.faces), self.cfg, self.state,
                                    only_family="Beta", log=lambda *_: None)
        self.assertEqual([t["family"] for t in todo], ["Beta"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (store.STATE_PATH, store.CONFIG_PATH, store.SECRET_PATH)
        store.STATE_PATH = os.path.join(self.tmp.name, "state.json")
        store.CONFIG_PATH = os.path.join(self.tmp.name, "config.json")
        store.SECRET_PATH = os.path.join(self.tmp.name, "secret.bin")
        store._reset_cache()

    def tearDown(self):
        store.STATE_PATH, store.CONFIG_PATH, store.SECRET_PATH = self.old
        store._reset_cache()
        self.tmp.cleanup()

    def test_state_roundtrip_creates_device_id(self):
        st = store.load_state()
        self.assertTrue(st["deviceId"])
        st["families"]["A"] = {"hash": "h"}
        store.save_state(st)
        again = store.load_state()
        self.assertEqual(again["families"]["A"]["hash"], "h")
        self.assertEqual(again["deviceId"], st["deviceId"])

    def test_config_defaults_merge(self):
        store.save_config({"repoName": "x", "sync": {"deleteAfterDays": 3}})
        cfg = store.load_config()
        self.assertEqual(cfg["repoName"], "x")
        self.assertEqual(cfg["sync"]["deleteAfterDays"], 3)
        self.assertEqual(cfg["sync"]["excludeCopyright"], ["unknown"],
                         "没写的字段要回落到默认值")

    def test_token_is_encrypted_on_disk(self):
        ok = store.upsert_account("alice", "ghp_example_token_for_test_only_000")
        if not ok:
            self.skipTest("这台机器没有 DPAPI")
        with open(store.SECRET_PATH, "rb") as fh:
            raw = fh.read()
        self.assertTrue(raw.startswith(b"DPAPI1"))
        self.assertNotIn(b"ghp_example", raw, "明文绝不能落盘")
        self.assertEqual(store.token_for("alice"), "ghp_example_token_for_test_only_000")

    def test_multiple_accounts_and_switch(self):
        if not store.upsert_account("alice", "ghp_aaaa"):
            self.skipTest("这台机器没有 DPAPI")
        store.upsert_account("bob", "ghp_bbbb")
        names = [n for n, _ in store.accounts()]
        self.assertEqual(names, ["alice", "bob"])
        self.assertEqual(store.current_account()[0], "bob", "新加的账号默认变成当前账号")
        store.set_current("alice")
        store._reset_cache()
        self.assertEqual(store.current_account(), ("alice", "ghp_aaaa"),
                         "切换账号要落盘")
        self.assertEqual(store.token_for("bob"), "ghp_bbbb")
        store.remove_account("alice")
        self.assertEqual(store.current_account()[0], "bob")

    def test_legacy_single_token_migrates(self):
        # 老版本：DPAPI 里就是一段裸 token
        if not store.upsert_account(store.LEGACY_ACCOUNT, "ghp_legacy_token"):
            self.skipTest("这台机器没有 DPAPI")
        store._reset_cache()
        self.assertEqual(store.current_account(), (store.LEGACY_ACCOUNT, "ghp_legacy_token"))
        # 拿到真名之后迁移，旧的 _legacy 不能留
        store.upsert_account("carol", "ghp_legacy_token")
        names = [n for n, _ in store.accounts()]
        self.assertEqual(names, ["carol"])
        self.assertEqual(store.current_account(), ("carol", "ghp_legacy_token"))

    def test_configured_needs_account_token_and_repo(self):
        self.assertFalse(store.configured(), "空配置必须判定为未配置")
        if not store.upsert_account("alice", "ghp_aaaa"):
            self.skipTest("这台机器没有 DPAPI")
        self.assertFalse(store.configured(), "没填仓库名还不算配好")
        store.save_config({"repoName": "my-fonts"})
        self.assertTrue(store.configured())

    def test_remember_repo_keeps_recent_first_and_dedupes(self):
        store.remember_repo("alice", "lib")
        store.remember_repo("alice", "other")
        store.remember_repo("alice", "lib")
        names = [r["name"] for r in store.load_config()["recentRepos"]]
        self.assertEqual(names, ["lib", "other"])

    def test_mask(self):
        self.assertEqual(store.mask(""), "(未设置)")
        self.assertEqual(store.mask("short"), "***")
        self.assertTrue(store.mask("ghp_abcdefghijklmnop").startswith("ghp_abc"))


class FakeRepoGh:
    """只实现 check_repo 用到的 repo_state。"""

    def __init__(self, state):
        self._state = state

    def repo_state(self, owner, name):
        return dict(self._state)


class CheckRepoTests(unittest.TestCase):
    BASE = {"exists": True, "private": True, "empty": False,
            "defaultBranch": "main", "anonymousReadable": False}

    def run_case(self, **patch):
        st = dict(self.BASE)
        st.update(patch)
        return syncer.check_repo(FakeRepoGh(st), "alice", "fonts")

    def test_ok(self):
        res = self.run_case()
        self.assertTrue(res["ok"])
        self.assertIn("main", res["message"])

    def test_missing_repo_will_be_created(self):
        res = self.run_case(exists=False, private=None, empty=None, anonymousReadable=None)
        self.assertTrue(res["ok"])
        self.assertTrue(res.get("willCreate"))

    def test_public_repo_is_refused(self):
        res = self.run_case(private=False, anonymousReadable=True)
        self.assertFalse(res["ok"])
        self.assertIn("公开", res["message"])

    def test_anonymous_readable_is_refused(self):
        res = self.run_case(anonymousReadable=True)
        self.assertFalse(res["ok"])
        self.assertIn("匿名", res["message"])

    def test_empty_repo_will_be_initialised(self):
        res = self.run_case(empty=True)
        self.assertTrue(res["ok"])
        self.assertTrue(res.get("willInit"))

    def test_blank_fields(self):
        res = syncer.check_repo(FakeRepoGh(self.BASE), "", "")
        self.assertFalse(res["ok"])


class GhTests(unittest.TestCase):
    def test_error_text_is_readable(self):
        import gh
        self.assertEqual(gh._error_text(b'{"message":"Not Found"}'), "Not Found")
        self.assertTrue(gh._error_text(b'{"message":"Validation Failed","errors":[{"code":"x"}]}'))


if __name__ == "__main__":
    unittest.main()
