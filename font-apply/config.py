# -*- coding: utf-8 -*-
"""插件自己的配置：目前只有一个 `doubleClickMode`。

存在插件目录下的 `config.json`，**缺文件 / 坏 JSON / 未知值一律按默认值**。
这条规则是刻意的：配置读不出来就崩插件，用户只会看到「右键菜单点了没反应」，
连一句能查的话都没有。所以这里所有异常都吞掉，落回默认。
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

# auto = 双击直接套用到最近在用的设计软件；pick = 双击后先点一下目标窗口
MODES = ("auto", "pick")
# 默认 pick：与 1.9.10 的第一版行为一致（双击 → 面板上出现「点一下要应用到的窗口」）。
# auto 是后来加的加速选项，用户要在插件设置里主动选。
DEFAULT_MODE = "pick"


def load():
    """读整份配置（永远返回一个带全部默认值的 dict）。"""
    data = {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            data = raw
    except (OSError, ValueError):
        # 文件不存在 / 不是 JSON / 读不动：全部当默认，不打扰用户
        data = {}
    mode = str(data.get("doubleClickMode") or "").strip().lower()
    return {"doubleClickMode": mode if mode in MODES else DEFAULT_MODE}


def double_click_mode():
    return load()["doubleClickMode"]


def save(patch):
    """把 patch 合进现有配置并落盘。只认 `doubleClickMode` 的合法值。

    返回落盘后的完整配置。写失败抛 OSError —— 这一处要往上抛，
    因为面板需要告诉用户「没保存成功」，不能装作成功了。
    """
    data = load()
    if isinstance(patch, dict):
        mode = str(patch.get("doubleClickMode") or "").strip().lower()
        if mode in MODES:
            data["doubleClickMode"] = mode
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    # 先写临时文件再替换：写到一半断电也不会留下半个 JSON
    os.replace(tmp, CONFIG_PATH)
    return data
