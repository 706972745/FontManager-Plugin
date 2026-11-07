"""同步编排：取本地清单 → 分组 → 打包 → 上传 → 记账。

三个「不做什么」比「做什么」更重要：

1. **不重新打包没变的字族**：先用「路径+大小+修改时间」的指纹比一遍，
   指纹一样就整族跳过 —— 否则每次同步都要把 6 GB 重新压一遍。
2. **不重传内容一样的字族**：指纹变了才打包，打完比 sha256；
   哈希一样（只改了 mtime、或者只是挪了文件）就不上传。
3. **不删刚被替换的 asset**：旧 asset 记进 `pendingDeletes`，
   过了 `deleteAfterDays` 天再物理删 —— 给另一台机器用旧版覆盖新版留反悔窗口。
"""

import hashlib
import json
import os
import tempfile
import time

import packer
import store

OPS_PREFIX = "ops"


class LocalApi:
    """主程序的本地 API（受限令牌）。只用到 `GET /fonts`。"""

    def __init__(self, base, token, timeout=30):
        self.base = (base or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout

    def get(self, path):
        import urllib.error
        import urllib.parse
        import urllib.request

        url = self.base + path
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "FontManager-Plugin-font-sync/0.1",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode("utf-8", "replace")
            raise RuntimeError(f"本地 API {e.code}：{body}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"连不上本地 API（{url}）：{e.reason}") from None

    def faces(self, page=2000):
        out, offset = [], 0
        while True:
            data = self.get(f"/fonts?limit={page}&offset={offset}")
            batch = data.get("faces") or []
            out.extend(batch)
            total = int(data.get("total") or 0)
            offset += len(batch)
            if not batch or offset >= total:
                return out


def group_by_family(faces):
    """{family: [ {path, size, mtime, copyrightClass, ...} ]}，只留有文件的。"""
    groups = {}
    for f in faces:
        path = f.get("path") or ""
        family = (f.get("family") or "").strip()
        if not path or not family:
            continue
        groups.setdefault(family, []).append(f)
    return groups


def fingerprint(items):
    """指纹 = 排序后的「路径|大小|mtime」。只 stat，不读内容。"""
    parts = []
    for f in sorted(items, key=lambda x: (x["path"] or "").lower()):
        try:
            st = os.stat(f["path"])
        except OSError:
            continue
        parts.append(f'{f["path"].lower()}|{st.st_size}|{int(st.st_mtime)}')
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:32]


def build_plan(api, cfg, state, only_family=None, log=print):
    sync_cfg = cfg.get("sync") or {}
    excluded = {c.lower() for c in (sync_cfg.get("excludeCopyright") or [])}
    max_bytes = int(sync_cfg.get("maxFamilyBytes") or 400 * 1024 * 1024)

    faces = api.faces()
    groups = group_by_family(faces)
    if only_family:
        groups = {k: v for k, v in groups.items() if k == only_family}
        if not groups:
            raise RuntimeError(f"本地清单里没有这个字族：{only_family}")

    known = state.get("families") or {}
    todo, skipped = [], {"unchanged": 0, "license": [], "tooBig": [], "noFile": []}

    for family, items in groups.items():
        classes = {(f.get("copyrightClass") or "unknown").lower() for f in items}
        if excluded and classes and classes <= excluded:
            skipped["license"].append(family)
            continue
        fp = fingerprint(items)
        prev = known.get(family) or {}
        if prev.get("fingerprint") == fp and prev.get("hash"):
            skipped["unchanged"] += 1
            continue
        total = 0
        paths = []
        for f in items:
            try:
                total += os.path.getsize(f["path"])
                paths.append(f["path"])
            except OSError:
                skipped["noFile"].append(f["path"])
        if not paths:
            continue
        if total > max_bytes:
            skipped["tooBig"].append((family, total))
            continue
        todo.append({"family": family, "paths": paths, "bytes": total, "fingerprint": fp})

    todo.sort(key=lambda e: e["bytes"])
    log(f"计划：{len(todo)} 个族要打包（共 {sum(e['bytes'] for e in todo)/1048576:.1f} MB），"
        f"跳过 {skipped['unchanged']} 个未变化")
    if skipped["license"]:
        log(f"按版权设置跳过 {len(skipped['license'])} 个族（{'/'.join(sorted(excluded))}）")
    if skipped["tooBig"]:
        for family, size in skipped["tooBig"][:20]:
            log(f"  过大跳过：{family}（{size/1048576:.1f} MB）")
    return todo, skipped


def ensure_repo(gh, cfg, state, log=print):
    """找到/建好私有仓库，并把「匿名可读」当场否掉。

    这一步是硬闸：仓库只要是公开可读的，就**拒绝同步**——
    宁可不备份，也不能把商用字体摆到公网上。
    """
    me = gh.whoami()
    login = me.get("login") or ""
    owner = (cfg.get("owner") or login).strip()
    name = (cfg.get("repoName") or "").strip()
    if not name:
        raise RuntimeError("还没设置仓库名（设置面板里填）")

    repo = gh.get_repo(owner, name)
    if repo is None:
        log(f"仓库 {owner}/{name} 不存在，创建私有仓…")
        repo = gh.create_private_repo(name, "FontManager 字体库同步（私有）")
        repo = gh.get_repo(owner, name) or repo
    if not repo.get("private"):
        raise RuntimeError(f"{owner}/{name} 不是私有仓库，已中止（改成私有再来）")
    if gh.repo_is_anonymous_readable(owner, name):
        raise RuntimeError(f"{owner}/{name} 匿名可读，已中止（这是最危险的状态，先改私有）")
    state["repo"] = f"{owner}/{name}"
    return owner, name


def _ops_record(device_id, entries, removed):
    return {
        "device": device_id,
        "at": int(time.time() * 1000),
        "families": [
            {"family": e["family"], "asset": e["asset"], "hash": e["hash"],
             "size": e["size"], "fonts": e["fonts"], "bucket": e["bucket"]}
            for e in entries
        ],
        "removed": removed,
    }


def run_sync(gh, api, cfg, state, only_family=None, dry_run=False, log=print, progress=None):
    """跑一次同步。返回汇总字典。"""
    sync_cfg = cfg.get("sync") or {}
    delete_after = int(sync_cfg.get("deleteAfterDays") or 7) * 86400
    batch = max(1, int(sync_cfg.get("batchFamilies") or 100))

    todo, skipped = build_plan(api, cfg, state, only_family, log)
    summary = {"planned": len(todo), "uploaded": 0, "failed": [], "deleted": 0,
               "skipped": skipped, "bytes": 0}
    if dry_run:
        state["lastPlan"] = {"at": int(time.time()), "families":
                             [{"family": e["family"], "bytes": e["bytes"]} for e in todo]}
        store.save_state(state)
        log("预演结束：以上是本次会打包上传的内容，什么都没传")
        return summary

    owner, name = ensure_repo(gh, cfg, state, log)
    release_id, bucket_tag = (None, None)
    entries, done = [], 0
    tmpdir = tempfile.mkdtemp(prefix="font-sync-")

    try:
        for item in todo:
            family = item["family"]
            try:
                meta = packer.pack_family(os.path.join(tmpdir, "f.zip"), item["paths"])
                prev = (state["families"].get(family) or {})
                if prev.get("hash") == meta["hash"] and prev.get("asset"):
                    log(f"  内容未变（只动了时间戳）：{family}")
                    state["families"][family] = dict(prev, fingerprint=item["fingerprint"])
                    continue
                if release_id is None or len(entries) >= 888:
                    release_id, bucket_tag = gh.find_or_create_bucket(owner, name)
                asset = packer.asset_name(family, meta)
                with open(os.path.join(tmpdir, "f.zip"), "rb") as fh:
                    blob = fh.read()
                asset_obj = gh.upload_asset(owner, name, release_id, asset, blob)
                asset_id = (asset_obj or {}).get("id")
                if asset_id is None:
                    # 422：同名 asset 已存在（上次传了但没记账），按成功处理
                    log(f"  同名 asset 已存在，按成功处理：{asset}")
                else:
                    old = prev.get("assetId")
                    if old:
                        state["pendingDeletes"].append(
                            {"assetId": old, "name": prev.get("asset"),
                             "deleteAfter": int(time.time()) + delete_after})
                    state["families"][family] = {
                        "fingerprint": item["fingerprint"], "hash": meta["hash"],
                        "asset": asset, "assetId": asset_id, "bucket": bucket_tag,
                        "size": meta["size"], "fonts": meta["fonts"],
                        "docs": meta["docs"], "at": int(time.time() * 1000),
                    }
                entries.append({"family": family, "asset": asset, "hash": meta["hash"],
                                "size": meta["size"], "fonts": meta["fonts"],
                                "bucket": bucket_tag})
                summary["uploaded"] += 1
                summary["bytes"] += meta["size"]
                done += 1
                log(f"  ↑ {family}（{meta['fonts']} 字面 {meta['size']/1048576:.2f} MB）→ {bucket_tag}")
                if progress:
                    progress(done, len(todo))
                if done % 20 == 0:
                    store.save_state(state)
            except Exception as e:  # 单族失败不拖垮整轮
                summary["failed"].append({"family": family, "error": str(e)})
                log(f"  ✗ {family}：{e}")
    finally:
        try:
            os.remove(os.path.join(tmpdir, "f.zip"))
        except OSError:
            pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass

    if entries:
        record = _ops_record(state["deviceId"], entries, [])
        path = f"{OPS_PREFIX}/{state['deviceId']}/{int(time.time()*1000)}.json"
        try:
            gh.commit_files(owner, name, gh_default_branch(gh, owner, name),
                            {path: json.dumps(record, ensure_ascii=False, indent=1).encode("utf-8")},
                            f"sync: {len(entries)} families from {state['deviceId']}")
            log(f"已提交记账文件 {path}")
        except Exception as e:
            log(f"记账提交失败（不影响 asset，下次会重记）：{e}")

    summary["deleted"] = flush_pending_deletes(gh, state, owner, name, log)
    state["lastSyncAt"] = int(time.time() * 1000)
    store.save_state(state)
    log(f"本轮结束：上传 {summary['uploaded']} 个族 / {summary['bytes']/1048576:.1f} MB，"
        f"失败 {len(summary['failed'])}，清掉旧 asset {summary['deleted']}")
    return summary


def gh_default_branch(gh, owner, name):
    repo = gh.get_repo(owner, name) or {}
    return repo.get("default_branch") or "main"


def flush_pending_deletes(gh, state, owner, name, log=print):
    """把到期的旧 asset 真的删掉。7 天窗口就是在这里生效的。"""
    keep, deleted = [], 0
    for item in state.get("pendingDeletes") or []:
        if int(item.get("deleteAfter") or 0) > int(time.time()):
            keep.append(item)
            continue
        try:
            gh.delete_asset(owner, name, item["assetId"])
            deleted += 1
            log(f"  已删除过期 asset：{item.get('name')}")
        except Exception as e:
            log(f"  删除 {item.get('name')} 失败：{e}")
            keep.append(item)
    state["pendingDeletes"] = keep
    return deleted
