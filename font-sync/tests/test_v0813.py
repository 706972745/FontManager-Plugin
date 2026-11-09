"""0.8.13 回归：同步规则「勾一下就生效」+ 版权未知默认不挡。

用户 2026-11-09 报：「这两个同步规则没法取消」「点击同步后，还是会回到默认」。
根因：勾选框只在「保存为当前库」里跟着写 config，勾掉后直接点「立即同步」，
同步结束面板按服务端状态 render() 一次，勾选就弹回默认。现在四个控件
change 即落盘（`POST /api/rules` → `save_rules_only`），且默认值只认
`store.DEFAULT_CONFIG`（把「不同步版权未知」改成默认关闭）。
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

import panel  # noqa: E402
import store  # noqa: E402


class RuleDefaultsTests(unittest.TestCase):
    def test_default_keeps_unknown_copyright(self):
        """默认不挡版权未知 —— 用户 2026-11-09 定：私有仓是备份不是分发。"""
        self.assertEqual(store.DEFAULT_CONFIG["sync"]["excludeCopyright"], [])

    def test_patch_without_fields_uses_defaults(self):
        p = panel._rules_patch({})
        self.assertEqual(p["excludeCopyright"], [], "勾选框默认不勾")
        self.assertTrue(p["deleteRemovedFamilies"], "删除同步默认开")
        self.assertEqual(p["deleteAfterDays"], 7)
        self.assertEqual(p["maxFamilyBytes"], 85 * 1024 * 1024)

    def test_tick_exclude_unknown(self):
        self.assertEqual(panel._rules_patch({"excludeUnknown": True})["excludeCopyright"],
                         ["unknown"])

    def test_untick_exclude_unknown_wins_over_stored_value(self):
        # 前端取消勾选后传的就是 false，必须真的清空，不能回落到默认值
        self.assertEqual(panel._rules_patch({"excludeUnknown": False})["excludeCopyright"], [])

    def test_delete_after_days_zero_is_kept(self):
        """0 天是合法值（旧包立刻删）——以前 `or 7` 会把 0 吃掉变成 7。"""
        self.assertEqual(panel._rules_patch({"deleteAfterDays": "0"})["deleteAfterDays"], 0)

    def test_max_mb_falls_back_on_garbage(self):
        self.assertEqual(panel._rules_patch({"maxFamilyMB": "abc"})["maxFamilyBytes"],
                         85 * 1024 * 1024)


class SaveRulesOnlyTests(unittest.TestCase):
    """save_rules_only 只碰 sync，绝不改账号 / 仓库名。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_cfg = store.CONFIG_PATH
        self._orig_secret = store.SECRET_PATH
        store.CONFIG_PATH = os.path.join(self._tmp.name, "config.json")
        store.SECRET_PATH = os.path.join(self._tmp.name, "secret.bin")
        store._reset_cache()
        store.save_config({"account": "IIEOzo", "repoName": "fontmanager-fonts",
                           "owner": ""})

    def tearDown(self):
        store.CONFIG_PATH = self._orig_cfg
        store.SECRET_PATH = self._orig_secret
        store._reset_cache()
        self._tmp.cleanup()

    def test_rules_saved_and_scope_untouched(self):
        panel.save_rules_only({"excludeUnknown": True, "deleteRemoved": False,
                               "maxFamilyMB": "200", "deleteAfterDays": "30"})
        cfg = json.load(open(store.CONFIG_PATH, encoding="utf-8"))
        self.assertEqual(cfg["sync"]["excludeCopyright"], ["unknown"])
        self.assertFalse(cfg["sync"]["deleteRemovedFamilies"])
        self.assertEqual(cfg["sync"]["deleteAfterDays"], 30)
        self.assertEqual(cfg["sync"]["maxFamilyBytes"], 200 * 1024 * 1024)
        self.assertEqual(cfg["repoName"], "fontmanager-fonts", "仓库名不许被规则保存冲掉")
        self.assertEqual(cfg["account"], "IIEOzo", "账号不许被规则保存冲掉")

    def test_unticking_persists(self):
        panel.save_rules_only({"excludeUnknown": True})
        panel.save_rules_only({"excludeUnknown": False})
        cfg = json.load(open(store.CONFIG_PATH, encoding="utf-8"))
        self.assertEqual(cfg["sync"]["excludeCopyright"], [])


if __name__ == "__main__":
    unittest.main()
