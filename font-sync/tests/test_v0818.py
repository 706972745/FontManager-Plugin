"""0.8.18：快照缺字体的联动钩子 `snapshot-missing`（主程序 1.8.3 起会发）。

主程序导入快照后发现「本机没有」的字体，把缺失字族广播给所有声明了
`hooks: ["snapshot-missing"]` 的插件。本插件只负责把用户送到
「同步与恢复 → 补缺」那个页签，**不自动下载** —— 静默拉几百个族既慢，
也可能拷来用户根本不想要的东西。

这里锁住三件事：
1. 收到 hook 一定打开 `sync` 页签（而不是默认的 account 页）；
2. `families` 里的脏数据（空串 / 非字符串）被过滤掉，不会拼进提示文案；
3. `missing` 缺失或非法时退回用族数，提示里不会出现 None / 0。
"""

import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import main as plugin_main  # noqa: E402


class SnapshotMissingHook(unittest.TestCase):
    def call(self, payload):
        with mock.patch.object(plugin_main, "open_settings_panel") as opened, mock.patch.object(
            plugin_main, "toast"
        ) as toasted, mock.patch.object(plugin_main, "log") as logged:
            plugin_main.handle_invoke("snapshot-missing", payload)
        return opened, toasted, logged

    def test_opens_sync_tab(self):
        opened, _, _ = self.call({"families": ["思源宋体"], "missing": 3, "total": 320})
        opened.assert_called_once_with("sync")

    def test_filters_dirty_family_names(self):
        _, toasted, _ = self.call(
            {"families": ["思源宋体", "", None, 123, "霞鹜文楷"], "missing": 7}
        )
        msg = toasted.call_args[0][0]
        self.assertIn("7", msg)
        self.assertIn("2 个族", msg)  # 脏数据被滤掉后只剩两个
        self.assertNotIn("None", msg)

    def test_missing_falls_back_to_family_count(self):
        _, toasted, _ = self.call({"families": ["A", "B", "C"]})  # 没给 missing
        msg = toasted.call_args[0][0]
        self.assertIn("3 个字体", msg)
        self.assertIn("3 个族", msg)

    def test_missing_non_positive_falls_back(self):
        _, toasted, _ = self.call({"families": ["A"], "missing": 0})
        self.assertIn("1 个字体", toasted.call_args[0][0])

    def test_empty_payload_still_opens_panel(self):
        opened, toasted, _ = self.call({})
        opened.assert_called_once_with("sync")
        self.assertIn("补缺", toasted.call_args[0][0])

    def test_unknown_hook_is_ignored(self):
        with mock.patch.object(plugin_main, "open_settings_panel") as opened, mock.patch.object(
            plugin_main, "toast"
        ) as toasted, mock.patch.object(plugin_main, "log") as logged:
            plugin_main.handle_invoke("no-such-hook", {"families": ["A"]})
        opened.assert_not_called()
        toasted.assert_not_called()
        logged.assert_called_once()


if __name__ == "__main__":
    unittest.main()
