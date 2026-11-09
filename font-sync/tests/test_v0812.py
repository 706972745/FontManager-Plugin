"""0.8.12 回归：GitHub 设备流报错说人话 + 预演结果带上分母。

用户 2026-11-09 报的两件事：

1. **「连不上 GitHub 的登录验证服务：HTTP Error 400: Bad Request」**
   真相不是网络 —— `POST /login/device/code` 真的回了 400，是 OAuth App
   没勾 `Enable Device Flow`。旧代码 `_post_form` 直接 `raise`，把 GitHub 的
   错误体丢了，用户只看到 urllib 的 `HTTP Error 400: Bad Request`。
   现在必须翻成「去勾 Enable Device Flow」并给出 kind，面板据此把 Client ID
   输入框和「改用令牌校验」一并露出来。

2. **「4000 多个族只显示出 278 个」**
   真相是默认 `excludeCopyright=["unknown"]` 挡掉了 3786 个族，预演只报
   「278 个族待传」，没有分母也没有跳过原因。现在 build_plan 必须给出
   全库族数 + 各项跳过数，describe_skips() 拼成一句话给 toast / 面板。
"""

import json
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

import store  # noqa: E402
import syncer  # noqa: E402
import verify  # noqa: E402

from test_core import FakeApi, make_tree  # noqa: E402


class DeviceFlowErrorTests(unittest.TestCase):
    """GitHub 的 4xx 必须翻译成「下一步点哪里」，不能是裸 HTTPError。"""

    def test_device_flow_disabled_is_explained(self):
        out = verify._explain_failure(400, json.dumps({"error": "device_flow_disabled"}), "Ov23li")
        self.assertEqual(out["kind"], "device_flow_disabled")
        self.assertIn("Enable Device Flow", out["error"])
        self.assertIn("400", out["error"], "要保留 HTTP 400 的事实，用户才好搜")

    def test_missing_client_id_is_explained(self):
        # 实测：瞎填的 client_id GitHub 回 404 {"error":"Not Found"}
        out = verify._explain_failure(404, json.dumps({"error": "Not Found"}), "abc")
        self.assertEqual(out["kind"], "bad_client_id")
        self.assertIn("不存在", out["error"])
        self.assertIn("secret", out["error"], "最常见的误操作是把 secret 当 ID 粘进来")

    def test_unknown_http_error_keeps_github_text(self):
        out = verify._explain_failure(422, json.dumps(
            {"error": "invalid_scope", "error_description": "scope 不支持"}), "x")
        self.assertEqual(out["kind"], "http")
        self.assertIn("scope 不支持", out["error"])

    def test_non_json_body_does_not_crash(self):
        out = verify._explain_failure(500, "<html>oops</html>", "x")
        self.assertEqual(out["kind"], "http")
        self.assertIn("500", out["error"])

    def test_probe_without_client_id_asks_for_one(self):
        orig = verify.client_id
        verify.client_id = lambda: ""
        try:
            out = verify.probe(None)
        finally:
            verify.client_id = orig
        self.assertFalse(out["ok"])
        self.assertTrue(out["needClientId"])

    def test_status_reports_method(self):
        st = verify.status()
        self.assertIn("method", st)
        self.assertIn("hasClientId", st)


class TokenFallbackTests(unittest.TestCase):
    """没有 OAuth App 时的降级路径：没令牌要明确说「先存令牌」。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_secret = store.SECRET_PATH
        store.SECRET_PATH = os.path.join(self._tmp.name, "secret.bin")
        store._reset_cache()

    def tearDown(self):
        store.SECRET_PATH = self._orig_secret
        store._reset_cache()
        self._tmp.cleanup()

    def test_without_token_is_rejected(self):
        out = verify.verify_by_token()
        self.assertFalse(out["ok"])
        self.assertIn("令牌", out["error"])


class PreviewBreakdownTests(unittest.TestCase):
    """预演必须能解释「为什么只传这么点」。"""

    def _faces(self, root):
        files = make_tree(root, {
            "已知A/A.ttf": 1024,
            "未知B/B.ttf": 1024,
            "未知C/C.ttf": 1024,
            "未知D/D.ttf": 1024,
        })
        return [
            {"family": "已知A", "path": files["已知A/A.ttf"], "source": "managed",
             "copyrightClass": "free_commercial"},
            {"family": "未知B", "path": files["未知B/B.ttf"], "source": "managed",
             "copyrightClass": "unknown"},
            {"family": "未知C", "path": files["未知C/C.ttf"], "source": "managed",
             "copyrightClass": None},
            {"family": "未知D", "path": files["未知D/D.ttf"], "source": "system",
             "copyrightClass": "unknown"},
        ]

    def test_skipped_license_keeps_denominator(self):
        with tempfile.TemporaryDirectory() as d:
            group1 = {}
            faces = self._faces(d)
            cfg = {"sync": {"excludeCopyright": ["unknown"],
                            "maxFamilyBytes": 8 * 1024 * 1024}}
            logs = []
            todo, skipped = syncer.build_plan(FakeApi(faces), cfg, group1, log=logs.append)
            self.assertEqual([e["family"] for e in todo], ["已知A"])
            self.assertEqual(skipped["localFamilies"], 3, "系统字体不算库里的族")
            self.assertEqual(len(skipped["license"]), 2)
            self.assertEqual(skipped["systemFaces"], 1)
            self.assertTrue(any("全库 3 个族" in line for line in logs))
            text = syncer.describe_skips(skipped)
            self.assertIn("2 族版权未知", text)

    def test_no_skips_is_empty_text(self):
        self.assertEqual(syncer.describe_skips({}), "")


if __name__ == "__main__":
    unittest.main()
