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
                names = [n for n in zf.namelist() if n != packer.MANIFEST]
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


class RemovedFamilyTests(unittest.TestCase):
    """本地删掉字族 → 远端跟着删（默认开，可关）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.files = make_tree(d, {"a/A.ttf": 1024, "b/B.ttf": 1024})
        self.faces = [
            {"path": self.files["a/A.ttf"], "family": "Alpha", "copyrightClass": "free"},
            {"path": self.files["b/B.ttf"], "family": "Beta", "copyrightClass": "free"},
        ]
        self.cfg = {"sync": {"excludeCopyright": [], "maxFamilyBytes": 10 << 20}}
        self.state = {"families": {"Gone": {"assetId": 999, "asset": "Gone__x.zip",
                                            "hash": "h"}}, "pendingDeletes": []}

    def tearDown(self):
        self.tmp.cleanup()

    def test_gone_family_is_detected_on_full_sync(self):
        todo, skipped = syncer.build_plan(FakeApi(self.faces), self.cfg, self.state,
                                          log=lambda *_: None)
        self.assertEqual(skipped["gone"], ["Gone"], "上次同步过、本地没有了的族要报出来")
        self.assertEqual([x["family"] for x in todo], ["Alpha", "Beta"])

    def test_single_family_sync_never_infers_deletion(self):
        _, skipped = syncer.build_plan(FakeApi(self.faces), self.cfg, self.state,
                                       only_family="Alpha", log=lambda *_: None)
        self.assertEqual(skipped["gone"], [],
                         "单族同步只知道这一个族，不能推断别的族被删了")

    def test_default_keeps_deleting(self):
        import store
        self.assertTrue(store.DEFAULT_CONFIG["sync"]["deleteRemovedFamilies"],
                        "默认应该跟着删（备份要和库一致）")

    def test_guard_stops_mass_deletion(self):
        ok, why = syncer._safe_to_remove(3, 100, ["x"] * 97)
        self.assertFalse(ok, "本地只读到 3 个族却要删 97 个，必须拦下来")
        self.assertIn("扫描", why)

    def test_guard_allows_normal_case(self):
        self.assertTrue(syncer._safe_to_remove(95, 100, ["x"] * 5)[0], "正常删几个要放行")
        self.assertTrue(syncer._safe_to_remove(2, 5, ["x"])[0], "小库不做比例判断")
        self.assertTrue(syncer._safe_to_remove(0, 0, [])[0], "没有要删的直接放行")


class FakeSyncGh:
    """只实现 run_sync 会用到的那几个方法。"""

    def __init__(self):
        self.deleted, self.commits, self.uploads = [], [], []

    def find_or_create_bucket(self, owner, name):
        return 1, "lib-0001"

    def upload_asset(self, owner, name, release_id, asset, blob):
        self.uploads.append(asset)
        return {"id": 1000 + len(self.uploads)}

    def delete_asset(self, owner, name, asset_id):
        self.deleted.append(asset_id)

    def commit_files(self, owner, name, branch, files, message, retries=6):
        self.commits.append((dict(files), message))


class RemoveSyncTests(unittest.TestCase):
    """跑一遍 run_sync，确认「本地没了」的族真的被从远端删掉、记账也写进去。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.files = make_tree(self.tmp.name, {"a/A.ttf": 1024, "b/B.ttf": 1024})
        self.api = FakeApi([
            {"path": self.files["a/A.ttf"], "family": "Alpha", "copyrightClass": "free"},
            {"path": self.files["b/B.ttf"], "family": "Beta", "copyrightClass": "free"},
        ])
        self.cfg = {"sync": {"excludeCopyright": [], "maxFamilyBytes": 10 << 20,
                             "deleteRemovedFamilies": True, "deleteAfterDays": 7}}
        self.state = {"deviceId": "dev", "families": {}, "pendingDeletes": [],
                      "lastSyncAt": 0}
        self.old_state_path = store.STATE_PATH
        store.STATE_PATH = os.path.join(self.tmp.name, "state.json")
        self.old_ensure = syncer.ensure_repo
        syncer.ensure_repo = lambda *a, **k: ("owner", "repo", "main")

    def tearDown(self):
        syncer.ensure_repo = self.old_ensure
        store.STATE_PATH = self.old_state_path
        self.tmp.cleanup()

    def _prime(self):
        """先让 Alpha/Beta 处于「已同步且没变化」，只剩 Gone 该被删。"""
        todo, _ = syncer.build_plan(self.api, self.cfg, self.state, log=lambda *_: None)
        for item in todo:
            self.state["families"][item["family"]] = {
                "fingerprint": item["fingerprint"], "hash": "h", "assetId": 1,
                "asset": item["family"] + "__x.zip"}
        self.state["families"]["Gone"] = {"fingerprint": "old", "hash": "h",
                                          "assetId": 999, "asset": "Gone__old.zip"}

    def test_gone_family_asset_is_deleted_and_state_cleared(self):
        self._prime()
        gh = FakeSyncGh()
        summary = syncer.run_sync(gh, self.api, self.cfg, self.state, log=lambda *_: None)
        self.assertEqual(gh.deleted, [999], "远端那份应该被删掉")
        self.assertNotIn("Gone", self.state["families"], "本地记录要一起清掉")
        self.assertEqual([r["family"] for r in summary["removed"]], ["Gone"])
        self.assertTrue(gh.commits, "删除也要留一条记账")
        record = json.loads(list(gh.commits[0][0].values())[0].decode("utf-8"))
        self.assertEqual([r["family"] for r in record["removed"]], ["Gone"],
                         "记账文件里要写明删了哪个族（换机器时能对上）")
        self.assertEqual(record["families"], [], "这一轮没有新上传")

    def test_setting_off_keeps_remote_copy(self):
        self._prime()
        self.cfg["sync"]["deleteRemovedFamilies"] = False
        gh = FakeSyncGh()
        summary = syncer.run_sync(gh, self.api, self.cfg, self.state, log=lambda *_: None)
        self.assertEqual(gh.deleted, [], "关掉设置就不许删远端")
        self.assertIn("Gone", self.state["families"])


class FakeEnsureGh:
    """ensure_repo 只需要这几个方法。"""

    def __init__(self, exists=True, private=True, readable=False, empty=False):
        self.state = {"exists": exists, "private": private,
                      "anonymousReadable": readable, "defaultBranch": "main",
                      "empty": empty}
        self.created = []

    def whoami(self):
        return {"login": "me"}

    def repo_state(self, owner, name):
        return dict(self.state)

    def create_private_repo(self, name, description=""):
        self.created.append(name)
        self.state["exists"] = True
        return {"full_name": f"me/{name}"}

    def init_empty_repo(self, owner, name, branch):
        self.state["empty"] = False


class RepoSwitchTests(unittest.TestCase):
    """换库防呆：旧库的同步记录必须清掉，否则新库永远是空的。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (store.STATE_PATH, store.CONFIG_PATH, store.SECRET_PATH)
        store.STATE_PATH = os.path.join(self.tmp.name, "state.json")
        store.CONFIG_PATH = os.path.join(self.tmp.name, "config.json")
        store.SECRET_PATH = os.path.join(self.tmp.name, "secret.bin")
        store._reset_cache()

    def tearDown(self):
        (store.STATE_PATH, store.CONFIG_PATH, store.SECRET_PATH) = self.old
        store._reset_cache()
        self.tmp.cleanup()

    def _state(self, repo):
        return {"repo": repo,
                "families": {"A": {"fingerprint": "f", "hash": "h", "assetId": 1},
                             "B": {"fingerprint": "f", "hash": "h", "assetId": 2}},
                "pendingDeletes": [{"assetId": 9}]}

    def test_switching_repo_clears_records_from_the_old_one(self):
        st = self._state("me/old")
        cfg = {"owner": "me", "repoName": "new"}
        owner, name, branch = syncer.ensure_repo(FakeEnsureGh(), cfg, st, log=lambda *_: None)
        self.assertEqual(st["repo"], "me/new")
        self.assertEqual(st["families"], {},
                         "换库后还留着旧库的指纹，build_plan 会把所有族判成未变化")
        self.assertEqual(st["pendingDeletes"], [],
                         "旧库的 asset id 在新库指向别的东西，不能带过去")

    def test_same_repo_keeps_its_records(self):
        st = self._state("me/same")
        cfg = {"owner": "me", "repoName": "same"}
        syncer.ensure_repo(FakeEnsureGh(), cfg, st, log=lambda *_: None)
        self.assertEqual(len(st["families"]), 2, "同一个库重复同步不该清记录")
        self.assertEqual(st["pendingDeletes"], [{"assetId": 9}])

    def test_repo_name_is_case_insensitive(self):
        st = self._state("me/Fonts")
        cfg = {"owner": "me", "repoName": "fonts"}
        syncer.ensure_repo(FakeEnsureGh(), cfg, st, log=lambda *_: None)
        self.assertEqual(len(st["families"]), 2, "只是大小写不同，算同一个库")

    def test_public_repo_is_still_refused(self):
        st = self._state("")
        cfg = {"owner": "me", "repoName": "x"}
        with self.assertRaises(RuntimeError) as ctx:
            syncer.ensure_repo(FakeEnsureGh(private=False), cfg, st, log=lambda *_: None)
        self.assertIn("不是私有", str(ctx.exception))


class RepoNameTests(unittest.TestCase):
    """仓库名规则：前后端同一套，后端必须兜底。"""

    def setUp(self):
        import panel
        self.panel = panel

    def test_accepts_normal_names(self):
        for name in ("fontmanager-fonts", "my.fonts_2", "A1", "a" * 100):
            self.assertEqual(self.panel.repo_name_problem(name), "", name)

    def test_rejects_what_github_rejects(self):
        bad = {
            "": "不能空",
            "我的字体": "中文",
            "my fonts": "中文和空格",
            "a/b": "只能用",
            "a" * 101: "100",
            ".hidden": "点开头",
            ".": "点开头",
        }
        for name, hint in bad.items():
            problem = self.panel.repo_name_problem(name)
            self.assertTrue(problem, f"{name!r} 应该被拒")
            self.assertIn(hint, problem, f"{name!r} 的提示不对：{problem}")


class ProgressTests(unittest.TestCase):
    """同步进度：阶段、逐族计数、回调崩了也不能影响同步。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.files = make_tree(self.tmp.name, {"a/A.ttf": 1024, "b/B.ttf": 1024})
        self.api = FakeApi([
            {"path": self.files["a/A.ttf"], "family": "Alpha", "copyrightClass": "free"},
            {"path": self.files["b/B.ttf"], "family": "Beta", "copyrightClass": "free"},
        ])
        self.cfg = {"sync": {"excludeCopyright": [], "maxFamilyBytes": 10 << 20,
                             "deleteRemovedFamilies": True, "deleteAfterDays": 7}}
        self.state = {"deviceId": "dev", "repo": "", "families": {},
                      "pendingDeletes": [], "lastSyncAt": 0}
        self.old_state = store.STATE_PATH
        store.STATE_PATH = os.path.join(self.tmp.name, "state.json")
        self.old_ensure = syncer.ensure_repo
        syncer.ensure_repo = lambda *a, **k: ("owner", "repo", "main")

    def tearDown(self):
        syncer.ensure_repo = self.old_ensure
        store.STATE_PATH = self.old_state
        self.tmp.cleanup()

    def test_reports_every_stage_with_growing_counter(self):
        events = []
        syncer.run_sync(FakeSyncGh(), self.api, self.cfg, self.state,
                        log=lambda *_: None,
                        progress=lambda phase, **kw: events.append((phase, kw)))
        phases = [e[0] for e in events]
        for stage in ("scan", "plan", "connect", "pack", "upload", "done"):
            self.assertIn(stage, phases, f"缺阶段 {stage}")
        ups = [kw for ph, kw in events if ph == "upload"]
        self.assertEqual([u["done"] for u in ups], [1, 2], "每传完一个族计数要加一")
        self.assertEqual({u["total"] for u in ups}, {2})
        self.assertGreater(ups[-1]["bytes"], 0)
        self.assertIn("total", events[phases.index("plan")][1])

    def test_dry_run_ends_with_done(self):
        events = []
        syncer.run_sync(FakeSyncGh(), self.api, self.cfg, self.state, dry_run=True,
                        log=lambda *_: None,
                        progress=lambda phase, **kw: events.append((phase, kw)))
        self.assertEqual(events[-1][0], "done")
        self.assertIn("预演", events[-1][1]["message"])
        self.assertNotIn("upload", [e[0] for e in events], "预演不该有上传阶段")

    def test_broken_callback_does_not_break_the_sync(self):
        def boom(*_a, **_kw):
            raise RuntimeError("UI 炸了")
        summary = syncer.run_sync(FakeSyncGh(), self.api, self.cfg, self.state,
                                  log=lambda *_: None, progress=boom)
        self.assertEqual(summary["uploaded"], 2, "进度回调抛异常不能把同步带崩")

    def test_panel_exposes_progress(self):
        import urllib.request
        import panel as panel_mod
        panel = panel_mod.Panel({}, progress=lambda: {"phase": "upload", "done": 3,
                                                     "total": 9, "running": True}).start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{panel.port}/api/progress",
                                         headers={"X-Panel-Key": panel.key})
            with urllib.request.urlopen(req, timeout=8) as r:
                body = json.loads(r.read())
            self.assertTrue(body["ok"])
            self.assertEqual(body["progress"]["done"], 3)
            self.assertTrue(body["progress"]["running"])
        finally:
            panel.stop()


class DuplicateAssetTests(unittest.TestCase):
    """上传撞 422（同名 asset 已存在）时也要把状态记上。

    实机踩到的：撞名被「按成功处理」但没有写 state.families，
    结果那一族永远不记账 —— 每次同步都重新打包、重新撞 422，
    本地删掉它时也找不到要删哪个 asset。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.files = make_tree(self.tmp.name, {"a/A.ttf": 1024})
        self.api = FakeApi([
            {"path": self.files["a/A.ttf"], "family": "Alpha", "copyrightClass": "free"},
        ])
        self.cfg = {"sync": {"excludeCopyright": [], "maxFamilyBytes": 10 << 20,
                             "deleteRemovedFamilies": True, "deleteAfterDays": 7}}
        self.state = {"deviceId": "dev", "repo": "", "families": {},
                      "pendingDeletes": [], "lastSyncAt": 0}
        self.old_state = store.STATE_PATH
        store.STATE_PATH = os.path.join(self.tmp.name, "state.json")
        self.old_ensure = syncer.ensure_repo
        syncer.ensure_repo = lambda *a, **k: ("owner", "repo", "main")

    def tearDown(self):
        syncer.ensure_repo = self.old_ensure
        store.STATE_PATH = self.old_state
        self.tmp.cleanup()

    def _gh(self, found):
        gh = FakeSyncGh()
        gh.upload_asset = lambda owner, name, rid, asset, blob: {}   # 422
        gh.list_assets = lambda owner, name, rid: found
        return gh

    def test_collision_records_state_even_without_asset_id(self):
        summary = syncer.run_sync(self._gh([]), self.api, self.cfg, self.state,
                                  log=lambda *_: None)
        self.assertEqual(summary["uploaded"], 1, "撞名按成功计")
        self.assertIn("Alpha", self.state["families"], "撞名也必须记账，否则下次又重传")
        self.assertIsNone(self.state["families"]["Alpha"]["assetId"])
        self.assertEqual(self.state["pendingDeletes"], [],
                         "撞的是同一个 asset，绝不能塞进待删队列")

    def test_collision_recovers_the_existing_asset_id(self):
        orig = syncer.packer.asset_name
        syncer.packer.asset_name = lambda family, meta: f"{family}.zip"
        try:
            gh = self._gh([{"name": "Alpha.zip", "id": 4242}])
            syncer.run_sync(gh, self.api, self.cfg, self.state, log=lambda *_: None)
        finally:
            syncer.packer.asset_name = orig
        self.assertEqual(self.state["families"]["Alpha"]["assetId"], 4242,
                         "取回 id 才能在本地删族时删掉远端那份")
        self.assertEqual(self.state["pendingDeletes"], [])


class UploadContentTypeTests(unittest.TestCase):
    """上传必须带 Content-Type，且不能把任何 422 都当成功。

    实机踩到的：urllib 对带 data 的 POST 默认发 application/x-www-form-urlencoded，
    GitHub 直接 422（custom / content_type can't be ...），而代码把 422 一律当
    「同名 asset 已存在」按成功处理 —— **界面显示同步完成，远端一个包都没有**。
    """

    def setUp(self):
        import gh as ghmod
        self.gh = ghmod

    def test_upload_sends_octet_stream(self):
        c = self.gh.Gh("t")
        seen = {}

        def fake(method, url, **kw):
            seen["method"] = method
            seen["url"] = url
            seen.update(kw)
            return 201, {"id": 7}

        c.request = fake
        obj = c.upload_asset("o", "r", 42, "a.zip", b"x")
        self.assertEqual(obj["id"], 7)
        self.assertEqual(seen.get("content_type"), "application/octet-stream",
                         "不带 Content-Type 会被 GitHub 422 拒掉")
        self.assertIn("/releases/42/assets", seen["url"])

    def test_duplicate_422_counts_as_success(self):
        c = self.gh.Gh("t")
        body = b'{"message":"Validation Failed","errors":[{"code":"already_exists"}]}'

        def fake(method, url, **kw):
            raise self.gh.GhError(422, "already exists", body)

        c.request = fake
        self.assertIsNone(c.upload_asset("o", "r", 1, "a.zip", b"x"))

    def test_other_422_is_an_error(self):
        c = self.gh.Gh("t")
        body = b'{"message":"Validation Failed","errors":[{"code":"custom","field":"content_type"}]}'

        def fake(method, url, **kw):
            raise self.gh.GhError(422, "content_type can't be ...", body)

        c.request = fake
        with self.assertRaises(self.gh.GhError):
            c.upload_asset("o", "r", 1, "a.zip", b"x")

    def test_duplicate_detector_ignores_noise(self):
        self.assertTrue(self.gh._is_duplicate_asset(
            b'{"errors":[{"code":"already_exists"}]}'))
        self.assertFalse(self.gh._is_duplicate_asset(b'{"errors":[{"code":"custom"}]}'))
        self.assertFalse(self.gh._is_duplicate_asset(b"not json"))
        self.assertFalse(self.gh._is_duplicate_asset(None))


class FamilyCapTests(unittest.TestCase):
    """单族上限：默认 85 MB（用户 2026-11-07 定的），且只在一个地方定义。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (store.STATE_PATH, store.CONFIG_PATH, store.SECRET_PATH)
        store.STATE_PATH = os.path.join(self.tmp.name, "state.json")
        store.CONFIG_PATH = os.path.join(self.tmp.name, "config.json")
        store.SECRET_PATH = os.path.join(self.tmp.name, "secret.bin")
        store._reset_cache()

    def tearDown(self):
        (store.STATE_PATH, store.CONFIG_PATH, store.SECRET_PATH) = self.old
        store._reset_cache()
        self.tmp.cleanup()

    def test_default_cap_is_85_megabytes(self):
        self.assertEqual(store.DEFAULT_CONFIG["sync"]["maxFamilyBytes"], 85 * 1024 * 1024,
                         "默认单族上限应当是 85 MB")

    def test_panel_reports_the_default_when_config_is_empty(self):
        import panel
        st = panel.snapshot()
        self.assertEqual(st["sync"]["maxFamilyMB"], 85,
                         "没存过配置时，面板要显示 85 而不是 400")

    def test_panel_roundtrips_a_custom_cap(self):
        import panel
        n = panel._save_sync_rules({"maxFamilyMB": 120, "deleteAfterDays": 7,
                                    "excludeUnknown": True, "deleteRemoved": True},
                                   "owner", "repo")
        self.assertEqual(n, 120)

    def test_omitted_cap_falls_back_to_default_not_zero(self):
        import panel
        panel._save_sync_rules({"maxFamilyMB": "", "deleteAfterDays": 7,
                                "excludeUnknown": True, "deleteRemoved": True},
                               "owner", "repo")
        cfg = store.load_config()
        self.assertEqual(cfg["sync"]["maxFamilyBytes"], 85 * 1024 * 1024,
                         "字段空着要回落到默认值，不能变成 1 MB")


class StoreLayoutTests(unittest.TestCase):
    """三个状态文件的位置（不碰它们，只检查路径）。"""

    def test_state_files_live_inside_the_plugin_dir(self):
        """config/secret/state 必须和 main.py 同级。

        反例（0.1.1）：PLUGIN_DIR 多剥了一层目录，三个文件全写到 plugins\\ 下，
        「打开同步状态目录」也打开错的层级。
        """
        here = os.path.dirname(os.path.abspath(store.__file__))
        self.assertEqual(store.PLUGIN_DIR, here, "PLUGIN_DIR 必须就是插件目录")
        for path in (store.CONFIG_PATH, store.SECRET_PATH, store.STATE_PATH):
            self.assertEqual(os.path.dirname(path), here, f"{path} 不在插件目录里")


class PanelTests(unittest.TestCase):
    """设置面板：没 key 一律拒、有 key 能读能存、页面文件存在且引用的 id 都在。"""

    def setUp(self):
        import panel
        self.panel_mod = panel
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (store.CONFIG_PATH, store.STATE_PATH, store.SECRET_PATH)
        store.CONFIG_PATH = os.path.join(self.tmp.name, "config.json")
        store.STATE_PATH = os.path.join(self.tmp.name, "state.json")
        store.SECRET_PATH = os.path.join(self.tmp.name, "secret.bin")
        store._reset_cache()

    def tearDown(self):
        (store.CONFIG_PATH, store.STATE_PATH, store.SECRET_PATH) = self.saved
        store._reset_cache()
        self.tmp.cleanup()

    def test_permission_table_covers_every_write_action(self):
        rows = self.panel_mod.PERMISSION_TABLE
        scopes = {r["scope"] for r in rows}
        self.assertIn("repo", scopes, "建仓/上传/记账都必须写明要 repo")
        writes = [r for r in rows if r["api"].startswith(("POST", "PATCH", "DELETE"))
                  and "本地 API" not in r["api"]]
        self.assertTrue(writes)
        for r in writes:
            self.assertEqual(r["scope"], "repo", f"{r['api']} 是写操作，scope 必须写清楚")
        local = [r for r in rows if "本地 API" in r["api"]]
        self.assertTrue(local, "恢复后要让主程序重扫，这条本地 API 权限必须写出来")
        for r in local:
            self.assertEqual(r["scope"], "write:fonts")

    def test_scope_flags_distinguish_classic_and_fine_grained(self):
        flags = self.panel_mod._scope_flags(["repo", "read:user"])
        self.assertTrue(flags["known"])
        self.assertTrue(flags["repo"])
        self.assertFalse(self.panel_mod._scope_flags(["public_repo"])["repo"],
                         "public_repo 不够，建私有仓会被拒")
        unknown = self.panel_mod._scope_flags([])
        self.assertFalse(unknown["known"], "fine-grained 不返回 scope，不能假装知道")
        self.assertIsNone(unknown["repo"])

    def test_snapshot_never_leaks_the_token(self):
        store.upsert_account("someone", "ghp_realtoken_should_not_appear_0001")
        snap = self.panel_mod.snapshot()
        blob = json.dumps(snap, ensure_ascii=False)
        self.assertNotIn("ghp_realtoken", blob, "面板快照里绝不能出现令牌原文")
        self.assertEqual(snap["login"], "someone")
        self.assertFalse(snap["configured"], "只存了令牌、没填仓库时还不算配好")

    def test_panel_requires_a_key_and_saves_config(self):
        import urllib.error
        import urllib.request

        panel = self.panel_mod.Panel({}).start()
        base = f"http://127.0.0.1:{panel.port}"
        try:
            def call(path, body=None, key=True):
                headers = {"Content-Type": "application/json"}
                if key:
                    headers["X-Panel-Key"] = panel.key
                data = json.dumps(body).encode("utf-8") if body is not None else None
                req = urllib.request.Request(base + path, data=data, headers=headers,
                                             method="POST" if data else "GET")
                try:
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        return resp.status, resp.read().decode("utf-8")
                except urllib.error.HTTPError as e:
                    return e.code, e.read().decode("utf-8")

            self.assertEqual(call("/api/state", key=False)[0], 403, "没 key 必须拒")
            self.assertEqual(call("/?k=wrong", key=False)[0], 403, "key 不对也不能给页面")

            code, body = call("/api/state")
            self.assertEqual(code, 200)
            self.assertFalse(json.loads(body)["state"]["configured"])

            code, body = call("/api/save", {"repoName": "  "})
            self.assertEqual(code, 200)
            self.assertFalse(json.loads(body)["ok"], "空仓库名不能被保存")

            code, body = call("/api/save", {"repoName": "my-fonts", "owner": "",
                                            "excludeUnknown": True, "maxFamilyMB": 300,
                                            "deleteAfterDays": 3})
            data = json.loads(body)
            self.assertTrue(data["ok"])
            self.assertEqual(data["state"]["repoName"], "my-fonts")
            self.assertEqual(data["state"]["sync"]["maxFamilyMB"], 300)
            cfg = store.load_config()
            self.assertEqual(cfg["sync"]["maxFamilyBytes"], 300 * 1024 * 1024)
            self.assertIn("unknown", cfg["sync"]["excludeCopyright"])
        finally:
            panel.stop()

    def test_repo_picker_needs_an_account_first(self):
        """没账号时，选库接口要明确让人先去存令牌，而不是抛异常。"""
        import urllib.request

        panel = self.panel_mod.Panel({}).start()
        base = f"http://127.0.0.1:{panel.port}"

        def call(path, body):
            req = urllib.request.Request(
                base + path, data=json.dumps(body).encode("utf-8"),
                headers={"X-Panel-Key": panel.key, "Content-Type": "application/json"},
                method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())

        try:
            for path in ("/api/repos", "/api/repo/use", "/api/repo/create"):
                r = call(path, {"repoName": "x"})
                self.assertFalse(r["ok"], path)
                self.assertIn("账号", r["error"], f"{path} 应提示先保存账号")
        finally:
            panel.stop()

    def test_html_has_pick_or_create_buttons(self):
        """选库页必须有「刷新列表 / 用这个库 / 建好并用它」三个按钮。

        用户原话：教程那段看不懂，希望「有个按钮直接在这里建库或选库」。
        这几个 id 是那个交互的锚点，改名要同步改 JS。
        """
        from pathlib import Path
        html = (Path(HERE) / "panel.html").read_text(encoding="utf-8")
        for anchor in ('id="repoRefresh"', 'id="repoUse"', 'id="repoCreate"',
                       'id="repoPick"', 'id="newRepoName"', 'id="currentRepo"'):
            self.assertIn(anchor, html)

    def test_dryrun_and_sync_are_post_only(self):
        """「预演 / 立即同步」必须是 POST；GET 应当被拒。

        回归用例：前端原来用「有没有 body」决定方法，这两个按钮没传 body，
        于是被发成 GET，后端 POST 分支根本轮不到 —— 用户看到的就是一句
        `unknown endpoint`，看着像接口不存在，其实是方法错了。
        """
        import urllib.request
        import urllib.error

        fired = []
        panel = self.panel_mod.Panel({"dry": lambda: fired.append("dry"),
                                     "sync": lambda: fired.append("sync")}).start()
        base = f"http://127.0.0.1:{panel.port}"

        def call(path, method):
            req = urllib.request.Request(
                base + path, data=b"{}",
                headers={"X-Panel-Key": panel.key, "Content-Type": "application/json"},
                method=method)
            try:
                with urllib.request.urlopen(req, timeout=8) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")

        try:
            for path in ("/api/dryrun", "/api/sync"):
                code, body = call(path, "POST")
                self.assertEqual(code, 200, f"{path} POST 应当成功：{body}")
                self.assertTrue(body["ok"], body)
            self.assertEqual(fired, ["dry", "sync"], "两个动作都要真的被调用")
            code, body = call("/api/dryrun", "GET")
            self.assertEqual(code, 404, "动作接口不接受 GET")
            self.assertEqual(body["error"], "unknown endpoint")
        finally:
            panel.stop()

    def test_open_dir_reports_a_missing_directory(self):
        """目录不在了要说清楚，别静默成功 —— 用户只会觉得「点了没用」。"""
        import urllib.request

        panel = self.panel_mod.Panel({}).start()
        old_dir = store.PLUGIN_DIR
        store.PLUGIN_DIR = os.path.join(self.tmp.name, "nope")
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{panel.port}/api/open_dir", data=b"{}",
                headers={"X-Panel-Key": panel.key, "Content-Type": "application/json"},
                method="POST")
            with urllib.request.urlopen(req, timeout=8) as r:
                body = json.loads(r.read())
            self.assertFalse(body["ok"])
            self.assertIn("目录不存在", body["error"])
            self.assertTrue(body["path"].endswith("nope"))
        finally:
            store.PLUGIN_DIR = old_dir
            panel.stop()

    def test_advanced_page_offers_copy_path(self):
        """打开目录可能被系统策略挡住，「复制路径」是保底通道，不能没有。"""
        from pathlib import Path
        html = (Path(HERE) / "panel.html").read_text(encoding="utf-8")
        for anchor in ('id="openDir"', 'id="copyDir"', "navigator.clipboard"):
            self.assertIn(anchor, html)

    def test_panel_js_sends_body_for_every_action(self):
        """只读接口（state / progress）之外，每个 api() 调用都要显式传参数。

        api() 按路径决定 GET/POST，不再靠 body 猜；这条测试盯住调用点，
        避免有人又把 `api('xxx')` 写回来 —— 那会把动作发成 GET → unknown endpoint。
        """
        import re
        from pathlib import Path
        html = (Path(HERE) / "panel.html").read_text(encoding="utf-8")
        read_only = ("state", "progress", "restore/info")
        bad = []
        for m in re.finditer(r"api\('([^']+)'\s*([,)])", html):
            name, tail = m.group(1), m.group(2)
            if name in read_only:
                continue
            if tail == ")":
                bad.append(name)
        self.assertEqual(bad, [], f"这些调用没传参数，可能被发成 GET：{bad}")

    def test_panel_html_ids_are_all_defined(self):
        import re
        from pathlib import Path
        html = (Path(HERE) / "panel.html").read_text(encoding="utf-8")
        ids = set(re.findall(r'\bid="([^"]+)"', html))
        refs = set(re.findall(r"\$\('([^']+)'\)", html))
        self.assertEqual(sorted(refs - ids), [], "JS 引用了不存在的元素 id")
        for tab in ("account", "repo", "restore", "perm", "guide", "adv"):
            self.assertIn(f'id="tab-{tab}"', html)
        self.assertIn("settings/tokens/new?scopes=repo", html,
                      "教程里必须给「已预勾 repo」的建令牌直链")


class SystemFontTests(unittest.TestCase):
    """Windows 自带字体绝不能进同步范围：不可再分发，也不属于字体库。"""

    def test_system_faces_are_excluded(self):
        faces = [
            {"path": r"C:\Windows\Fonts\arial.ttf", "family": "Arial", "source": "system"},
            {"path": r"L:\库\MyFont\b.ttf", "family": "MyFont", "source": "managed"},
        ]
        groups = syncer.group_by_family(faces)
        self.assertEqual(list(groups), ["MyFont"])

    def test_missing_source_field_still_works(self):
        """老版本 API 不返回 source，不能因此把整个同步变成空操作。"""
        faces = [{"path": r"L:\库\X\a.ttf", "family": "X"}]
        self.assertEqual(list(syncer.group_by_family(faces)), ["X"])


class AssetNameTests(unittest.TestCase):
    def test_parses_our_asset_names(self):
        slug, digest = packer.parse_asset_name("Source_Han_Sans__0123456789ab.zip")
        self.assertEqual(slug, "Source_Han_Sans")
        self.assertEqual(digest, "0123456789ab")

    def test_rejects_foreign_files(self):
        for name in ("manifest.json", "abc.zip", "abc__short.zip",
                     "abc__zzzzzzzzzzzz.zip", ""):
            self.assertEqual(packer.parse_asset_name(name), (None, None), name)


class ManifestTests(unittest.TestCase):
    def test_manifest_carries_real_family_name(self):
        with tempfile.TemporaryDirectory() as d:
            files = make_tree(d, {"a/思源黑体 SC.otf": 512})
            zip_path = os.path.join(d, "x.zip")
            packer.pack_family(zip_path, [files["a/思源黑体 SC.otf"]],
                               family="思源黑体 SC / Source Han Sans SC")
            manifest = packer.read_manifest(zip_path)
            self.assertEqual(manifest["family"], "思源黑体 SC / Source Han Sans SC")
            self.assertEqual([f["name"] for f in manifest["fonts"]], ["思源黑体 SC.otf"])
            self.assertEqual(len(manifest["fonts"][0]["sha256"]), 64)

    def test_manifest_is_deterministic(self):
        """同内容同族名 → 字节相同。清单里放时间戳会立刻毁掉跨机幂等。"""
        with tempfile.TemporaryDirectory() as d:
            files = make_tree(d, {"a/T.ttf": 1024})
            p = files["a/T.ttf"]
            m1 = packer.pack_family(os.path.join(d, "1.zip"), [p], family="F")
            m2 = packer.pack_family(os.path.join(d, "2.zip"), [p], family="F")
            self.assertEqual(m1["hash"], m2["hash"])
            m3 = packer.pack_family(os.path.join(d, "3.zip"), [p], family="G")
            self.assertNotEqual(m1["hash"], m3["hash"], "族名进了清单，改名就该变哈希")

    def test_old_package_without_manifest_reads_as_none(self):
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.zip")
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("T.ttf", b"x" * 32)
            self.assertIsNone(packer.read_manifest(path))


class UnpackTests(unittest.TestCase):
    def test_round_trip_restores_identical_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            src = make_tree(os.path.join(d, "src"), {"a/T.ttf": 2048, "a/OFL.txt": 128})
            zip_path = os.path.join(d, "x.zip")
            packer.pack_family(zip_path, [src["a/T.ttf"]], docs=[src["a/OFL.txt"]],
                               family="F")
            out = os.path.join(d, "out")
            res = packer.unpack_family(zip_path, out)
            self.assertEqual(len(res["fonts"]), 1)
            self.assertTrue(os.path.exists(os.path.join(out, "T.ttf")))
            self.assertTrue(os.path.exists(os.path.join(out, "docs", "OFL.txt")))
            with open(os.path.join(out, "T.ttf"), "rb") as fh:
                with open(src["a/T.ttf"], "rb") as orig:
                    self.assertEqual(fh.read(), orig.read(), "字体字节必须一模一样")

    def test_same_file_is_skipped_not_rewritten(self):
        with tempfile.TemporaryDirectory() as d:
            src = make_tree(os.path.join(d, "src"), {"a/T.ttf": 512})
            zip_path = os.path.join(d, "x.zip")
            packer.pack_family(zip_path, [src["a/T.ttf"]], family="F")
            out = os.path.join(d, "out")
            packer.unpack_family(zip_path, out)
            again = packer.unpack_family(zip_path, out)
            self.assertEqual(again["fonts"], [], "第二次恢复不该重复写盘")
            self.assertEqual(len(again["skipped"]), 1)

    def test_different_file_is_never_overwritten_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            src = make_tree(os.path.join(d, "src"), {"a/T.ttf": 512})
            zip_path = os.path.join(d, "x.zip")
            packer.pack_family(zip_path, [src["a/T.ttf"]], family="F")
            out = os.path.join(d, "out")
            os.makedirs(out)
            with open(os.path.join(out, "T.ttf"), "wb") as fh:
                fh.write(b"local version")
            res = packer.unpack_family(zip_path, out)
            self.assertEqual(len(res["conflicts"]), 1)
            with open(os.path.join(out, "T.ttf"), "rb") as fh:
                self.assertEqual(fh.read(), b"local version", "没勾覆盖就不能动本地文件")

    def test_path_traversal_is_rejected(self):
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "evil.zip")
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("../evil.ttf", b"x")
            out = os.path.join(d, "out")
            packer.unpack_family(path, out)
            self.assertFalse(os.path.exists(os.path.join(d, "evil.ttf")))

    def test_dangerous_family_name_becomes_safe_dir(self):
        self.assertNotIn("/", syncer._safe_dirname("A/B"))
        self.assertNotIn(":", syncer._safe_dirname("C:\\x"))
        self.assertEqual(syncer._safe_dirname("  "), "family")


class FakeRestoreGh:
    """远端：按 asset 名发 zip。只实现 run_restore 用得到的方法。"""

    def __init__(self, packages, private=True, readable=False):
        self.packages = packages  # [{"name":..., "blob": bytes}]
        self.state = {"exists": True, "private": private, "anonymousReadable": readable,
                      "defaultBranch": "main", "empty": False}

    def whoami(self):
        return {"login": "me"}

    def repo_state(self, owner, name):
        return dict(self.state)

    def list_releases_simple(self, owner, name):
        return [{"id": 7, "tag_name": "lib-0001"}]

    def list_assets(self, owner, name, release_id):
        return [{"id": i + 1, "name": p["name"], "size": len(p["blob"]),
                 "created_at": f"2026-01-0{i+1}T00:00:00Z"}
                for i, p in enumerate(self.packages)]

    def download_asset(self, owner, name, asset_id, dest_path, chunk=1 << 20):
        blob = self.packages[int(asset_id) - 1]["blob"]
        with open(dest_path, "wb") as fh:
            fh.write(blob)
        return len(blob)


class FakeScanApi(FakeApi):
    def __init__(self, faces):
        super().__init__(faces)
        self.posts = []

    def post(self, path, body=None):
        self.posts.append(path)
        return {"ok": True}


class RestoreTests(unittest.TestCase):
    """新机器恢复：远端有、本机没有 → 下载解压、记账、让主程序重扫。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (store.STATE_PATH, store.CONFIG_PATH, store.SECRET_PATH)
        store.STATE_PATH = os.path.join(self.tmp.name, "state.json")
        store.CONFIG_PATH = os.path.join(self.tmp.name, "config.json")
        store.SECRET_PATH = os.path.join(self.tmp.name, "secret.bin")
        store._reset_cache()
        self.lib = os.path.join(self.tmp.name, "library")
        os.makedirs(self.lib)
        self.cfg = {"owner": "me", "repoName": "fonts", "restoreDir": self.lib}

    def tearDown(self):
        (store.STATE_PATH, store.CONFIG_PATH, store.SECRET_PATH) = self.old
        store._reset_cache()
        self.tmp.cleanup()

    def _package(self, family, size=1024):
        src = make_tree(os.path.join(self.tmp.name, "src_" + str(len(family))),
                        {f"{packer.slugify(family)}/T{len(family)}.ttf": size})
        path = os.path.join(self.tmp.name, f"p{len(family)}.zip")
        meta = packer.pack_family(path, list(src.values()), family=family)
        with open(path, "rb") as fh:
            blob = fh.read()
        return {"name": packer.asset_name(family, meta),
                "blob": blob, "family": family}

    def test_new_machine_downloads_and_writes_fonts(self):
        pkg = self._package("思源黑体 SC")
        api = FakeScanApi([])
        st = {"deviceId": "dev2", "repo": "", "families": {}, "pendingDeletes": []}
        summary = syncer.run_restore(FakeRestoreGh([pkg]), api, self.cfg, st,
                                     log=lambda *_: None)
        self.assertEqual(summary["downloaded"], 1)
        self.assertEqual(summary["failed"], [])
        written = os.path.join(self.lib, "思源黑体 SC", "T7.ttf")
        self.assertTrue(os.path.exists(written), "字体文件必须落到字体库里")
        self.assertIn("/fonts/scan", api.posts, "写完必须让主程序重扫，否则界面看不到")
        rec = st["families"]["思源黑体 SC"]
        self.assertEqual(len(rec["hash"]), 64, "恢复时要记完整哈希，下次同步才不会重传")
        self.assertTrue(rec.get("fromRemote"))

    def test_family_already_local_is_skipped(self):
        pkg = self._package("Arial")
        api = FakeScanApi([{"path": os.path.join(self.lib, "Arial", "a.ttf"),
                            "family": "Arial"}])
        st = {"deviceId": "dev1", "repo": "", "families": {}, "pendingDeletes": []}
        summary = syncer.run_restore(FakeRestoreGh([pkg]), api, self.cfg, st,
                                     log=lambda *_: None)
        self.assertEqual(summary["planned"], 0)
        self.assertEqual(summary["downloaded"], 0)
        self.assertEqual(summary["skipped"], 1)

    def test_overwrite_setting_pulls_it_again(self):
        pkg = self._package("Arial")
        api = FakeScanApi([{"path": os.path.join(self.lib, "Arial", "a.ttf"),
                            "family": "Arial"}])
        st = {"deviceId": "dev1", "repo": "", "families": {}, "pendingDeletes": []}
        summary = syncer.run_restore(FakeRestoreGh([pkg]), api, self.cfg, st,
                                     overwrite=True, log=lambda *_: None)
        self.assertEqual(summary["downloaded"], 1)

    def test_corrupted_download_is_refused(self):
        pkg = self._package("Broken")
        pkg["blob"] = b"not the real zip content"
        api = FakeScanApi([])
        st = {"deviceId": "dev2", "repo": "", "families": {}, "pendingDeletes": []}
        summary = syncer.run_restore(FakeRestoreGh([pkg]), api, self.cfg, st,
                                     log=lambda *_: None)
        self.assertEqual(summary["downloaded"], 0)
        self.assertEqual(len(summary["failed"]), 1, "哈希对不上就不能往字体库里写")
        self.assertFalse(os.path.exists(os.path.join(self.lib, "Broken")))

    def test_dry_run_touches_nothing(self):
        pkg = self._package("Preview")
        api = FakeScanApi([])
        st = {"deviceId": "dev2", "repo": "", "families": {}, "pendingDeletes": []}
        summary = syncer.run_restore(FakeRestoreGh([pkg]), api, self.cfg, st,
                                     dry_run=True, log=lambda *_: None)
        self.assertEqual(summary["planned"], 1)
        self.assertEqual(summary["downloaded"], 0)
        self.assertFalse(os.path.exists(os.path.join(self.lib, "Preview")))
        self.assertIn("Preview", syncer.list_remote(FakeRestoreGh([pkg]), "me", "fonts",
                                                    log=lambda *_: None)[0]["slug"])

    def test_only_one_family(self):
        a, b = self._package("A"), self._package("BB")
        api = FakeScanApi([])
        st = {"deviceId": "dev2", "repo": "", "families": {}, "pendingDeletes": []}
        summary = syncer.run_restore(FakeRestoreGh([a, b]), api, self.cfg, st,
                                     only="BB", log=lambda *_: None)
        self.assertEqual(summary["downloaded"], 1)
        self.assertTrue(os.path.isdir(os.path.join(self.lib, "BB")))
        self.assertFalse(os.path.isdir(os.path.join(self.lib, "A")))

    def test_latest_version_wins(self):
        old = self._package("Dup")
        new = self._package("Dup", size=2048)
        rows = syncer.list_remote(FakeRestoreGh([old, new]), "me", "fonts",
                                  log=lambda *_: None)
        self.assertEqual(len(rows), 1, "同一族有多个版本时只留最新的")
        self.assertEqual(rows[0]["size"], len(new["blob"]))

    def test_public_repo_refuses_restore(self):
        pkg = self._package("X")
        api = FakeScanApi([])
        st = {"deviceId": "dev2", "repo": "", "families": {}, "pendingDeletes": []}
        with self.assertRaises(RuntimeError):
            syncer.run_restore(FakeRestoreGh([pkg], private=False), api, self.cfg, st,
                               log=lambda *_: None)


if __name__ == "__main__":
    unittest.main()
