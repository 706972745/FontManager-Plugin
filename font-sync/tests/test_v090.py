# -*- coding: utf-8 -*-
"""0.9.0：元数据（标签 / 评分 / 收藏 / 备注）随字体一起同步。

重点不是「能跑」，而是**多台机器都往同一份写时不能互相覆盖**：
合并必须可交换、幂等，写回只写差异。
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import meta  # noqa: E402


class FakeApi:
    """只实现 get('/curation') 与 put(...)，把调用记下来。"""

    def __init__(self, curation=None):
        self.curation = curation or {}
        self.puts = []

    def get(self, path):
        assert path == "/curation", path
        return self.curation

    def put(self, path, body):
        self.puts.append((path, body))
        return {"ok": True}


class FakeGh:
    def __init__(self, remote=None):
        self.remote = remote
        self.commits = []

    def get_file_json(self, owner, name, path, branch="main"):
        assert path == meta.CURATION_PATH
        return self.remote

    def commit_files(self, owner, name, branch, files, message):
        self.commits.append({"files": files, "message": message})
        self.remote = json.loads(next(iter(files.values())).decode("utf-8"))
        return "sha"


class NormalizeTests(unittest.TestCase):
    def test_drops_junk_and_keeps_shape(self):
        raw = {
            "tags": {"A": ["x", " ", "x", "y"], "B": "not-a-list", "C": []},
            "favorites": ["A", "", None, "B"],
            "ratings": {"A": 4, "B": 9, "C": "x", "D": 0},
            "notes": {"A": "  hi  ", "B": "", "C": None},
            "collections": {"分组": ["A"]},  # 分组不参与同步
        }
        out = meta.normalize(raw)
        self.assertEqual(out["tags"], {"A": ["x", "y"]})
        self.assertEqual(out["favorites"], ["A", "B"])
        self.assertEqual(out["ratings"], {"A": 4})
        self.assertEqual(out["notes"], {"A": "hi"})
        self.assertEqual(set(out), {"tags", "favorites", "ratings", "notes"})

    def test_empty_and_bad_input(self):
        self.assertEqual(meta.normalize(None), meta.EMPTY)
        self.assertEqual(meta.normalize("nope"), meta.EMPTY)


class MergeTests(unittest.TestCase):
    A = {
        "tags": {"族1": ["衬线", "标题"]},
        "favorites": ["族1"],
        "ratings": {"族1": 3},
        "notes": {"族1": "短"},
    }
    B = {
        "tags": {"族1": ["标题", "免费"], "族2": ["无衬线"]},
        "favorites": ["族2"],
        "ratings": {"族1": 5, "族2": 4},
        "notes": {"族1": "更长的备注", "族2": "备注"},
    }

    def test_union_and_max(self):
        out = meta.merge(self.A, self.B)
        self.assertEqual(out["tags"]["族1"], ["免费", "标题", "衬线"])
        self.assertEqual(out["favorites"], ["族1", "族2"])
        self.assertEqual(out["ratings"], {"族1": 5, "族2": 4})
        self.assertEqual(out["notes"]["族1"], "更长的备注")

    def test_commutative(self):
        self.assertEqual(meta.merge(self.A, self.B), meta.merge(self.B, self.A))

    def test_idempotent(self):
        once = meta.merge(self.A, self.B)
        self.assertEqual(meta.merge(once, self.B), once)
        self.assertEqual(meta.merge(once, once), once)

    def test_empty_side_is_identity(self):
        self.assertEqual(meta.merge(self.A, meta.EMPTY), meta.normalize(self.A))
        self.assertEqual(meta.merge(meta.EMPTY, self.B), meta.normalize(self.B))


class DiffAndApplyTests(unittest.TestCase):
    def test_diff_only_reports_changes(self):
        local = {"tags": {"A": ["x"]}, "favorites": ["A"], "ratings": {"A": 5}, "notes": {"A": "n"}}
        merged = {"tags": {"A": ["x"]}, "favorites": ["A", "B"], "ratings": {"A": 5}, "notes": {"A": "n2"}}
        out = meta.diff(local, merged)
        self.assertEqual(out["tags"], {})
        self.assertEqual(out["favorites"], ["B"])
        self.assertEqual(out["ratings"], {})
        self.assertEqual(out["notes"], {"A": "n2"})

    def test_apply_writes_only_diff_and_respects_only(self):
        api = FakeApi()
        local = {"tags": {"A": ["x"], "B": []}, "favorites": [], "ratings": {}, "notes": {}}
        merged = {"tags": {"A": ["x", "y"], "B": ["z"]}, "favorites": ["A"], "ratings": {"A": 4}, "notes": {"A": "hi"}}
        done = meta.apply(api, local, merged, log=lambda *_: None)
        self.assertEqual(done, 5)  # A 标签 / B 标签 / A 收藏 / A 评分 / A 备注
        paths = [p for p, _ in api.puts]
        self.assertIn("/curation/tags", paths)
        self.assertIn("/curation/favorite", paths)
        self.assertIn("/curation/rating", paths)
        self.assertIn("/curation/note", paths)

        api2 = FakeApi()
        meta.apply(api2, local, merged, only={"B"}, log=lambda *_: None)
        fams = {body["family"] for _, body in api2.puts}
        self.assertEqual(fams, {"B"})

    def test_apply_swallows_single_failure(self):
        class Flaky(FakeApi):
            def put(self, path, body):
                if body["family"] == "A":
                    raise RuntimeError("boom")
                return super().put(path, body)

        api = Flaky()
        local = meta.EMPTY
        merged = {"tags": {}, "favorites": ["A", "B"], "ratings": {}, "notes": {}}
        done = meta.apply(api, local, merged, log=lambda *_: None)
        self.assertEqual(done, 1)  # A 失败，B 成功，整轮不中断


class PushPullTests(unittest.TestCase):
    def test_push_skips_when_nothing_changed(self):
        remote = meta.normalize(PushPullTests.LOCAL)
        gh = FakeGh(remote)
        api = FakeApi(PushPullTests.LOCAL)
        meta.push(gh, api, "o", "r", "main", log=lambda *_: None)
        self.assertEqual(gh.commits, [])

    def test_push_merges_and_commits(self):
        gh = FakeGh({"favorites": ["远端族"]})
        api = FakeApi(PushPullTests.LOCAL)
        merged = meta.push(gh, api, "o", "r", "main", log=lambda *_: None)
        self.assertEqual(len(gh.commits), 1)
        self.assertIn(meta.CURATION_PATH, gh.commits[0]["files"])
        self.assertIn("远端族", merged["favorites"])
        self.assertIn("本地族", merged["favorites"])

    def test_pull_writes_remote_into_local(self):
        gh = FakeGh({"tags": {"族1": ["云端"]}, "favorites": ["族1"], "ratings": {"族1": 5}, "notes": {}})
        api = FakeApi({"tags": {"族1": ["本地"]}})
        done = meta.pull(gh, api, "o", "r", "main", log=lambda *_: None)
        self.assertEqual(done, 3)  # 标签（合并后不同）/ 收藏 / 评分
        tags_call = [b for p, b in api.puts if p == "/curation/tags"][0]
        self.assertEqual(tags_call["tags"], ["云端", "本地"])

    def test_pull_noop_without_remote(self):
        gh = FakeGh(None)
        api = FakeApi(PushPullTests.LOCAL)
        self.assertEqual(meta.pull(gh, api, "o", "r", "main", log=lambda *_: None), 0)
        self.assertEqual(api.puts, [])

    LOCAL = {
        "tags": {"族1": ["本地"]},
        "favorites": ["本地族"],
        "ratings": {},
        "notes": {},
    }


if __name__ == "__main__":
    unittest.main()
