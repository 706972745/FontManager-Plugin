"""字体库的「元数据」：标签 / 评分 / 收藏 / 备注。

字体包只搬字节。换一台电脑，你写的标签、打的分、收藏、备注不会跟着走 ——
重装系统或换机器之后，几万个族全变成「未知」，等于白整理。

这里把 `GET /curation` 的结果收成一份小 JSON（`ops/curation.json`），
跟字体包一起提交到同一个仓库；恢复时再合并回本机。

**合并口径**（多台机器都往同一份写，必须可交换、不丢数据）：

| 字段 | 合并方式 |
|---|---|
| tags | 并集（去重、排序） |
| favorites | 并集 |
| ratings | 取较大值（0 = 未评分） |
| notes | 两边都有取更长的（信息更多）；一边为空取另一边 |

这四条都满足「交换律 + 幂等」：A 合并 B 和 B 合并 A 结果一样，重复合并也不会变。
所以哪台机器先提交都不会覆盖别人 —— 这是「多机一致」在没有 tombstones 时能做到的最好版本。

`collections`（分组）**不同步**：主程序的本地 API 还没有写分组的端点，
硬塞进去只会造成「云端有、本机写不进去」的假象。等主程序补了端点再加。
"""

import json

# 仓库里的路径。放 ops/ 下面，跟记账文件同一个目录，不污染字体包。
CURATION_PATH = "ops/curation.json"

EMPTY = {"tags": {}, "favorites": [], "ratings": {}, "notes": {}}


def normalize(raw):
    """把 `GET /curation`（或仓库里那份）洗干净。

    一份脏数据不该毁掉整轮合并：类型不对的整条丢掉，字符串去空白，
    评分只认 1~5，备注只留非空。返回的 dict 永远四个键齐全。
    """
    raw = raw if isinstance(raw, dict) else {}

    tags = {}
    for fam, arr in (raw.get("tags") or {}).items():
        if not isinstance(arr, list):
            continue
        clean = sorted({str(t).strip() for t in arr if t is not None and str(t).strip()})
        if clean:
            tags[str(fam)] = clean

    favorites = sorted({str(f).strip() for f in (raw.get("favorites") or [])
                       if f is not None and str(f).strip()})

    ratings = {}
    for fam, v in (raw.get("ratings") or {}).items():
        try:
            stars = int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= stars <= 5:
            ratings[str(fam)] = stars

    notes = {}
    for fam, v in (raw.get("notes") or {}).items():
        text = str(v or "").strip()
        if text:
            notes[str(fam)] = text

    return {"tags": tags, "favorites": favorites, "ratings": ratings, "notes": notes}


def merge(a, b):
    """合并两份元数据（可交换、幂等）。见模块注释里的表。"""
    a, b = normalize(a), normalize(b)

    tags = {}
    for fam in set(a["tags"]) | set(b["tags"]):
        merged = sorted(set(a["tags"].get(fam, [])) | set(b["tags"].get(fam, [])))
        if merged:
            tags[fam] = merged

    ratings = {}
    for fam in set(a["ratings"]) | set(b["ratings"]):
        ratings[fam] = max(a["ratings"].get(fam, 0), b["ratings"].get(fam, 0))

    notes = {}
    for fam in set(a["notes"]) | set(b["notes"]):
        left, right = a["notes"].get(fam, ""), b["notes"].get(fam, "")
        notes[fam] = left if len(left) >= len(right) else right

    return {
        "tags": tags,
        "favorites": sorted(set(a["favorites"]) | set(b["favorites"])),
        "ratings": ratings,
        "notes": notes,
    }


def diff(local, merged):
    """合并后**真正需要写回本机**的那部分（只写差异，不把整库 PUT 一遍）。"""
    local, merged = normalize(local), normalize(merged)
    out = {"tags": {}, "favorites": [], "ratings": {}, "notes": {}}

    for fam, tags in merged["tags"].items():
        if local["tags"].get(fam) != tags:
            out["tags"][fam] = tags

    have = set(local["favorites"])
    out["favorites"] = [f for f in merged["favorites"] if f not in have]

    for fam, stars in merged["ratings"].items():
        if local["ratings"].get(fam) != stars:
            out["ratings"][fam] = stars

    for fam, note in merged["notes"].items():
        if local["notes"].get(fam) != note:
            out["notes"][fam] = note

    return out


def apply(api, local, merged, only=None, log=print):
    """把差异写回本机。`only` 给了就只处理这些族（恢复单族时用）。

    每条都单独 try：一个族写失败不该让整轮停下来 —— 标签写不进去，
    至少字体已经到位了。返回写成功的条数。
    """
    todo = diff(local, merged)
    allowed = set(only) if only is not None else None
    done = 0

    for fam, tags in todo["tags"].items():
        if allowed is not None and fam not in allowed:
            continue
        try:
            api.put("/curation/tags", {"family": fam, "tags": tags})
            done += 1
        except Exception as e:  # noqa: BLE001
            log(f"  ! 标签写回失败 {fam}：{e}")

    for fam in todo["favorites"]:
        if allowed is not None and fam not in allowed:
            continue
        try:
            api.put("/curation/favorite", {"family": fam, "favorite": True})
            done += 1
        except Exception as e:  # noqa: BLE001
            log(f"  ! 收藏写回失败 {fam}：{e}")

    for fam, stars in todo["ratings"].items():
        if allowed is not None and fam not in allowed:
            continue
        try:
            api.put("/curation/rating", {"family": fam, "stars": stars})
            done += 1
        except Exception as e:  # noqa: BLE001
            log(f"  ! 评分写回失败 {fam}：{e}")

    for fam, note in todo["notes"].items():
        if allowed is not None and fam not in allowed:
            continue
        try:
            api.put("/curation/note", {"family": fam, "note": note})
            done += 1
        except Exception as e:  # noqa: BLE001
            log(f"  ! 备注写回失败 {fam}：{e}")

    return done


def summary(meta):
    """一行人类可读的概况，给日志用。"""
    meta = normalize(meta)
    return (f"{len(meta['tags'])} 个族有标签、{len(meta['ratings'])} 个有评分、"
            f"{len(meta['favorites'])} 个收藏、{len(meta['notes'])} 条备注")


def push(gh, api, owner, name, branch, log=print):
    """把本机元数据合并进仓库里那份，有变化才提交。返回合并后的元数据。"""
    local = normalize(api.get("/curation"))
    remote = normalize(gh.get_file_json(owner, name, CURATION_PATH, branch) or {})
    merged = merge(local, remote)
    if merged == remote:
        log("元数据没有变化，跳过")
        return merged
    body = json.dumps(merged, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")
    gh.commit_files(owner, name, branch, {CURATION_PATH: body},
                    "meta: 标签 / 评分 / 收藏 / 备注")
    log(f"元数据已上传：{summary(merged)}")
    return merged


def pull(gh, api, owner, name, branch, only=None, log=print):
    """把仓库里的元数据合并回本机。返回写回的条数。"""
    remote = normalize(gh.get_file_json(owner, name, CURATION_PATH, branch) or {})
    if not any(remote.values()):
        return 0
    local = normalize(api.get("/curation"))
    merged = merge(local, remote)
    done = apply(api, local, merged, only=only, log=log)
    if done:
        log(f"元数据已写回本机：{done} 项（{summary(merged)}）")
    return done
