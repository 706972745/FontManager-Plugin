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
import re
import tempfile
import time

import packer
import store

OPS_PREFIX = "ops"
LIB_TAG_PREFIX = "lib-"


class LocalApi:
    """主程序的本地 API（受限令牌）。只用到 `GET /fonts`。

    **必须绕开系统代理**：本地地址走 `HTTPS_PROXY`/`HTTP_PROXY` 会被代理当成外网请求，
    代理连不上 127.0.0.1 就回 502（实测：「upstream connect failed」）。
    这里用空 ProxyHandler 的 opener，逐请求生效，不动全局环境。
    """

    def __init__(self, base, token, timeout=30):
        self.base = (base or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        self._opener = None

    def _open(self, req):
        if self._opener is None:
            import urllib.request

            self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return self._opener.open(req, timeout=self.timeout)

    def get(self, path):
        return self._request("GET", path)

    def post(self, path, body=None):
        return self._request("POST", path, body)

    def _request(self, method, path, body=None):
        import urllib.error
        import urllib.request

        url = self.base + path
        payload = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "FontManager-Plugin-font-sync/0.1",
        }
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with self._open(req) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            text = e.read()[:300].decode("utf-8", "replace")
            raise RuntimeError(f"本地 API {e.code}：{text}") from None
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


def group_by_family(faces, library_only=True):
    """{family: [ {path, size, mtime, copyrightClass, ...} ]}，只留有文件的。

    `library_only` 过滤掉**系统字体**（`C:\\Windows\\Fonts` 那 300 多个，
    `source = system`）：那些是 Windows 自带、不可再分发的，传上去既没用又惹版权。
    只同步 `source = managed`（字体库里的）。老版本 API 没有 `source` 字段时放行。
    """
    groups = {}
    for f in faces:
        src = (f.get("source") or "").strip().lower()
        if library_only and src and src != "managed":
            continue
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
    led = _ledger(state, faces)  # 多库切换兼容：按当前字体库取账目
    system_faces = sum(1 for f in faces if (f.get("source") or "").lower() == "system")
    groups = group_by_family(faces)
    if system_faces:
        log(f"跳过 {system_faces} 个系统字体（Windows 自带，不可再分发、也不属于字体库）")
    if only_family:
        groups = {k: v for k, v in groups.items() if k == only_family}
        if not groups:
            raise RuntimeError(f"本地清单里没有这个字族：{only_family}")

    known = _ledger(state, faces).get("families") or {}
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

    # 上次同步过、这次本地已经没有的族（只在整个库全量扫的时候算；
    # 单族同步不知道「别的族还在不在」，绝不能拿它推断删除）
    skipped["localFamilies"] = len(groups)
    skipped["gone"] = (sorted(f for f in known if f not in groups)
                       if only_family is None else [])

    todo.sort(key=lambda e: e["bytes"])
    log(f"计划：{len(todo)} 个族要打包（共 {sum(e['bytes'] for e in todo)/1048576:.1f} MB），"
        f"跳过 {skipped['unchanged']} 个未变化")
    if skipped["license"]:
        log(f"按版权设置跳过 {len(skipped['license'])} 个族（{'/'.join(sorted(excluded))}）")
    if skipped["tooBig"]:
        for family, size in skipped["tooBig"][:20]:
            log(f"  过大跳过：{family}（{size/1048576:.1f} MB）")
    return todo, skipped


def resolve_target(gh, cfg):
    """算出「要往哪个账号的哪个仓库同步」：(login, owner, name)。不联网。"""
    login = ""
    try:
        login = (gh.whoami() or {}).get("login") or ""
    except Exception:
        login = ""
    owner = (cfg.get("owner") or login).strip()
    name = (cfg.get("repoName") or "").strip()
    return login, owner, name


def check_repo(gh, owner, name):
    """设置面板用：一次性把「这个库能不能用」说清楚。"""
    if not owner or not name:
        return {"ok": False, "message": "先填账号和仓库名"}
    try:
        st = gh.repo_state(owner, name)
    except Exception as e:
        return {"ok": False, "message": f"查不到 {owner}/{name}：{e}"}
    if not st["exists"]:
        return {"ok": True, "willCreate": True,
                "message": f"{owner}/{name} 还不存在 —— 保存后第一次同步会自动建成私有仓"}
    if not st["private"]:
        return {"ok": False, "message": f"{owner}/{name} 是**公开**仓库，同步已被禁止（先改成私有）"}
    if st["anonymousReadable"]:
        return {"ok": False, "message": f"{owner}/{name} 能匿名读取，同步已被禁止（检查仓库可见性）"}
    if st["empty"]:
        return {"ok": True, "willInit": True,
                "message": f"{owner}/{name} 是空仓库 —— 首次同步会自动放一个 README 建出首个提交"}
    return {"ok": True, "message": f"{owner}/{name} 可用（私有，默认分支 {st['defaultBranch']}）"}


def ensure_repo(gh, cfg, state, log=print, create=True):
    """找到/建好私有仓库，并把「匿名可读」当场否掉。

    这一步是硬闸：仓库只要是公开可读的，就**拒绝同步**——
    宁可不备份，也不能把商用字体摆到公网上。
    """
    login, owner, name = resolve_target(gh, cfg)
    if not login:
        raise RuntimeError("令牌无效或没网：拿不到 GitHub 账号（先在设置里点「测试连接」）")
    if not name:
        raise RuntimeError("还没设置仓库名（设置面板里填一个，或从「最近的库」里选）")

    st = gh.repo_state(owner, name)
    if not st["exists"]:
        if not create:
            raise RuntimeError(f"{owner}/{name} 不存在")
        log(f"仓库 {owner}/{name} 不存在，创建私有仓…")
        gh.create_private_repo(name, "FontManager 字体库同步（私有，勿设为公开）")
        st = gh.repo_state(owner, name)
        if not st["exists"]:
            raise RuntimeError(f"建仓失败：{owner}/{name} 还是没有（令牌有没有 repo 权限？）")
    if not st["private"]:
        raise RuntimeError(f"{owner}/{name} 不是私有仓库，已中止（改成私有再来）")
    if st["anonymousReadable"]:
        raise RuntimeError(f"{owner}/{name} 匿名可读，已中止（这是最危险的状态，先改私有）")

    branch = st["defaultBranch"] or "main"
    if st["empty"]:
        log("仓库还是空的，先放一个 README 建出首个提交…")
        gh.init_empty_repo(owner, name, branch)
        st = gh.repo_state(owner, name)
        branch = st["defaultBranch"] or branch

    target = f"{owner}/{name}"
    prev = (state.get("repo") or "").strip()
    if prev and prev.lower() != target.lower():
        # 换库防呆：families 里的指纹/assetId 是**旧库**的。留着它们的话，
        # build_plan 会把每个族都判成「未变化」直接跳过 —— 新库永远是空的，
        # 日志却显示「跳过 N 个未变化」，看着像成功。所以换库必须清空重来。
        had = sum(len(led.get("families") or {}) for led in _iter_ledgers(state))
        log(f"目标库 {prev} → {target}：清掉属于旧仓库的 {had} 条同步记录，按新仓库重新全量")
        for led in _iter_ledgers(state):
            # 远端仓库都换了，所有库的账都指向旧仓库，全部作废
            led["families"] = {}
            led["pendingDeletes"] = []
            led.pop("lastGone", None)
        state["families"] = {}
        # 旧库的待删 asset id 在新库里指向别的东西，绝不能带过去
        state["pendingDeletes"] = []
    state["repo"] = target
    store.remember_repo(owner, name)
    return owner, name, branch


def _safe_to_remove(local_families, known_families, gone):
    """批量删除前的保险丝。

    最怕的场景：主程序正在扫描 / 库没挂上，本地清单只回来一小部分，
    这时「其他族都不见了」会被当成用户删除，一口气把远端全删了。
    所以：上次记过 20 个以上、而这次本地只读到不足一半时，这一轮不删。
    返回 (能不能删, 原因)。
    """
    if not gone:
        return True, ""
    if known_families >= 20 and local_families < known_families * 0.5:
        return False, (f"本地只读到 {local_families} 个族，上次同步过 {known_families} 个 —— "
                       f"像是扫描没跑完或字体库没挂上，这轮不删任何远端内容")
    return True, ""


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


def _find_asset_id(gh, owner, name, release_id, asset_name):
    """撞名（422）时把已存在 asset 的 id 取回来。取不到就返回 None，不抛异常。

    比较用归一化 slug：GitHub 会把名字里的 `!` 改写成 `.`（FS-1），我们记的名字
    和 GitHub 存的名字可能差一个字符——`Far_Out!__x.zip` vs `Far_Out.__x.zip`。
    拆出 (slug, hash12) 后按 slug_key 比，hash12 是内容寻址的，必须严格相等。
    """
    try:
        want_slug, want_digest = packer.parse_asset_name(asset_name)
        for a in gh.list_assets(owner, name, release_id) or []:
            got_slug, got_digest = packer.parse_asset_name(a.get("name"))
            if got_slug is None:
                continue
            if got_digest == want_digest and packer.slug_key(got_slug) == packer.slug_key(want_slug):
                return a.get("id")
    except Exception:
        pass
    return None


def _report(progress, phase, **fields):
    """把进度报给界面。回调出错绝不能影响同步本身。"""
    if not progress:
        return
    try:
        progress(phase, **fields)
    except Exception:
        pass


def _library_key(faces):
    """当前字体库的标识：managed 字体路径的公共根目录。多库切换各记各的账。"""
    dirs = sorted({os.path.dirname(os.path.abspath(f.get("path") or "")).lower()
                   for f in (faces or [])
                   if (f.get("source") or "").lower() == "managed" and f.get("path")})
    if not dirs:
        return "empty"
    try:
        return os.path.commonpath(dirs).lower()
    except ValueError:
        return "mixed"


def _ledger(state, faces):
    """取「当前字体库」的同步账目（families / pendingDeletes / lastGone）。

    - 新结构（state["libraries"] 按 <库根> 分键）：切换/回切字体库互不删除、不重传；
    - 旧扁平结构（没有 libraries 键）：原样返回 state —— 兼容老 state 与既有测试。
    """
    libs = state.get("libraries")
    if not isinstance(libs, dict):
        return state
    led = libs.setdefault(_library_key(faces), {})
    led.setdefault("families", {})
    led.setdefault("pendingDeletes", [])
    return led


def _iter_ledgers(state):
    """所有账目：兼容扁平结构时先给 state 本身，再给每个库的账。"""
    yield state
    libs = state.get("libraries")
    if isinstance(libs, dict):
        for led in libs.values():
            if isinstance(led, dict):
                yield led


def run_sync(gh, api, cfg, state, only_family=None, dry_run=False, log=print,
             progress=None, max_families=None):
    """跑一次同步。返回汇总字典。

    `max_families`：本轮最多传几个族（按体积从小到大）。首次全量可能几十 GB，
    一口气跑完容易中途断网、也看不到阶段性成果；分批传，剩下的下一轮继续。
    """
    sync_cfg = cfg.get("sync") or {}
    # 多库切换兼容：按当前字体库取账目（多调一次 /fonts 只为算库标识）
    led = _ledger(state, api.faces())
    delete_after = int(sync_cfg.get("deleteAfterDays") or 7) * 86400
    batch = max(1, int(sync_cfg.get("batchFamilies") or 100))

    _report(progress, "scan")
    todo, skipped = build_plan(api, cfg, state, only_family, log)
    if max_families and len(todo) > max_families:
        rest = len(todo) - max_families
        todo = todo[:max_families]
        log(f"本轮只传前 {max_families} 个族（还有 {rest} 个下轮继续）")
    total_bytes = sum(e["bytes"] for e in todo)
    _report(progress, "plan", total=len(todo), bytesTotal=total_bytes)
    summary = {"planned": len(todo), "uploaded": 0, "failed": [], "deleted": 0,
               "removed": [], "skipped": skipped, "bytes": 0}
    gone = list(skipped.get("gone") or [])
    delete_removed = bool(sync_cfg.get("deleteRemovedFamilies", True))
    fused = False
    if gone:
        if not delete_removed:
            log(f"本地少了 {len(gone)} 个族，但设置里关掉了「同步删除」，远端保持不动")
            gone = []
        else:
            ok, why = _safe_to_remove(skipped.get("localFamilies") or 0,
                                      len(led.get("families") or {}), gone)
            if not ok:
                # 保险丝触发：账**不能清**（本地清单不可信，清了会把真删除也抹掉），
                # 但同一批缺席每轮都念叨一遍就是噪音 —— 按「缺席集合」去重提示。
                fp = hashlib.sha256(",".join(gone).encode("utf-8")).hexdigest()[:16]
                prev = led.get("lastGone") or {}
                if prev.get("key") == fp:
                    log(f"⚠️ 与上一轮完全相同：仍有 {len(gone)} 个族缺席"
                        f"（保险丝生效中，本轮不删任何远端内容）")
                else:
                    log(f"⚠️ 跳过删除：{why}")
                led["lastGone"] = {"key": fp, "at": int(time.time()), "count": len(gone)}
                gone = []
                fused = True
            else:
                log(f"本地少了 {len(gone)} 个族，本轮会一并删掉远端备份（可在设置里关掉）")
    if not fused and not gone and led.get("lastGone"):
        # 清单恢复正常（或没有缺席了）：解除去重记忆，下次再触发时重新完整提示
        led.pop("lastGone", None)
    if dry_run:
        state["lastPlan"] = {"at": int(time.time()),
                             "families": [{"family": e["family"], "bytes": e["bytes"]}
                                          for e in todo],
                             "removed": gone}
        store.save_state(state)
        if gone:
            log("预演：以下族在本地已删除，正式同步时远端备份也会删掉 —— "
                + "、".join(gone[:10]) + ("" if len(gone) <= 10 else f" 等 {len(gone)} 个"))
        log("预演结束：以上是本次会打包上传的内容，什么都没传")
        _report(progress, "done", total=len(todo), done=0, bytesTotal=total_bytes,
                message=f"预演完成：{len(todo)} 个族待传")
        return summary

    _report(progress, "connect", total=len(todo), bytesTotal=total_bytes)
    owner, name, branch = ensure_repo(gh, cfg, state, log)
    release_id, bucket_tag = (None, None)
    entries, done = [], 0
    tmpdir = tempfile.mkdtemp(prefix="font-sync-")

    try:
        for index, item in enumerate(todo, start=1):
            family = item["family"]
            _report(progress, "pack", done=done, total=len(todo), family=family,
                    index=index, bytes=summary["bytes"], bytesTotal=total_bytes)
            try:
                meta = packer.pack_family(os.path.join(tmpdir, "f.zip"), item["paths"],
                                          family=family)
                prev = (led["families"].get(family) or {})
                if prev.get("hash") == meta["hash"] and prev.get("asset"):
                    log(f"  内容未变（只动了时间戳）：{family}")
                    led["families"][family] = dict(prev, fingerprint=item["fingerprint"])
                    continue
                if release_id is None or len(entries) >= 888:
                    release_id, bucket_tag = gh.find_or_create_bucket(owner, name)
                asset = packer.asset_name(family, meta)
                with open(os.path.join(tmpdir, "f.zip"), "rb") as fh:
                    blob = fh.read()
                asset_obj = gh.upload_asset(owner, name, release_id, asset, blob)
                asset_id = (asset_obj or {}).get("id")
                if asset_id is None:
                    # 422：同名 asset 已存在（上次传了但没记上账）。必须把它的 id
                    # 找回来再记账 —— 否则这一族永远进不了 state，每次同步都会
                    # 重新打包、重新撞 422；本地删掉它时也找不到要删哪个包。
                    log(f"  同名 asset 已存在，按成功处理：{asset}")
                    asset_id = _find_asset_id(gh, owner, name, release_id, asset)
                    if asset_id is None:
                        log(f"  （没能取回 {asset} 的 id，先记账，删除时可能需要手工处理）")
                # 无论新传还是撞名都要记账
                old = prev.get("assetId")
                if old and old != asset_id:
                    led["pendingDeletes"].append(
                        {"assetId": old, "name": prev.get("asset"),
                         "deleteAfter": int(time.time()) + delete_after})
                led["families"][family] = {
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
                _report(progress, "upload", done=done, total=len(todo), family=family,
                        index=index, bytes=summary["bytes"], bytesTotal=total_bytes)
                if done % 20 == 0:
                    store.save_state(state)
            except Exception as e:  # 单族失败不拖垮整轮
                summary["failed"].append({"family": family, "error": str(e)})
                log(f"  ✗ {family}：{e}")
                _report(progress, "failed", done=done, total=len(todo), family=family,
                        index=index, bytes=summary["bytes"], bytesTotal=total_bytes,
                        message=str(e))
    finally:
        try:
            os.remove(os.path.join(tmpdir, "f.zip"))
        except OSError:
            pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass

    for position, family in enumerate(gone, start=1):
        _report(progress, "delete", done=position, total=len(gone), family=family,
                index=position, bytes=summary["bytes"], bytesTotal=total_bytes)
        info = led["families"].get(family) or {}
        asset_id = info.get("assetId")
        if not asset_id:
            # 只有记账没有 asset（上次传失败或旧格式），本地记录清掉即可
            led["families"].pop(family, None)
            summary["removed"].append({"family": family, "asset": "", "hash": ""})
            continue
        try:
            gh.delete_asset(owner, name, asset_id)
            led["families"].pop(family, None)
            summary["removed"].append({"family": family, "asset": info.get("asset") or "",
                                       "hash": info.get("hash") or ""})
            log(f"  − 远端已删（本地不存在了）：{family}")
        except Exception as e:
            log(f"  ✗ 删除远端 {family} 失败（下一轮再试）：{e}")

    if entries or summary["removed"]:
        record = _ops_record(state["deviceId"], entries, summary["removed"])
        path = f"{OPS_PREFIX}/{state['deviceId']}/{int(time.time()*1000)}.json"
        try:
            gh.commit_files(owner, name, branch,
                            {path: json.dumps(record, ensure_ascii=False, indent=1).encode("utf-8")},
                            f"sync: {len(entries)} families, -{len(summary['removed'])} "
                            f"from {state['deviceId']}")
            log(f"已提交记账文件 {path}")
        except Exception as e:
            log(f"记账提交失败（不影响 asset，下次会重记）：{e}")

    summary["deleted"] = flush_pending_deletes(gh, state, owner, name, log)
    state["lastSyncAt"] = int(time.time() * 1000)
    store.save_state(state)
    log(f"本轮结束：上传 {summary['uploaded']} 个族 / {summary['bytes']/1048576:.1f} MB，"
        f"删除远端 {len(summary['removed'])} 个族，失败 {len(summary['failed'])}，"
        f"清掉旧 asset {summary['deleted']}")
    _report(progress, "done", done=len(todo), total=len(todo),
            bytes=summary["bytes"], bytesTotal=total_bytes,
            message=(f"完成：上传 {summary['uploaded']} 个族"
                     + (f"，删除 {len(summary['removed'])} 个" if summary["removed"] else "")
                     + (f"，失败 {len(summary['failed'])} 个" if summary["failed"] else "")))
    return summary


def flush_pending_deletes(gh, state, owner, name, log=print):
    """把到期的旧 asset 真的删掉。7 天窗口就是在这里生效的。
    多库分账后每个库有自己的待删列表，这里全部一起处理（asset 都在同一个仓库）。"""
    deleted = 0
    for led in _iter_ledgers(state):
        keep, done = [], 0
        for item in led.get("pendingDeletes") or []:
            if int(item.get("deleteAfter") or 0) > int(time.time()):
                keep.append(item)
                continue
            try:
                gh.delete_asset(owner, name, item["assetId"])
                done += 1
                log(f"  已删除过期 asset：{item.get('name')}")
            except Exception as e:
                log(f"  删除 {item.get('name')} 失败：{e}")
                keep.append(item)
        led["pendingDeletes"] = keep
        deleted += done
    return deleted


# ───────────────────────── 下载 / 恢复到本机 ─────────────────────────
def list_remote(gh, owner, name, log=print):
    """列出远端所有字体包（只认 `lib-0001` 这类分桶里的 `<slug>__<hash>.zip`）。

    同一个族可能同时存在多个版本（旧版还在 7 天反悔窗口里），
    这里按 asset 创建时间取最新的那一个 —— 恢复时永远拿最新的。
    """
    rows = []
    for rel in gh.list_releases_simple(owner, name):
        tag = rel.get("tag_name") or ""
        if not tag.startswith(LIB_TAG_PREFIX):
            continue
        for a in gh.list_assets(owner, name, rel["id"]) or []:
            slug, digest = packer.parse_asset_name(a.get("name"))
            if not slug:
                continue
            rows.append({
                "asset": a.get("name"), "slug": slug, "hash12": digest,
                "assetId": a.get("id"), "releaseId": rel["id"], "tag": tag,
                "size": int(a.get("size") or 0),
                "createdAt": a.get("created_at") or "",
            })
    rows.sort(key=lambda r: (r["createdAt"], r["assetId"] or 0))
    latest = {}
    for r in rows:
        # 去重键用归一化 slug：老包的 `!` 被 GitHub 改写成了 `.`（FS-1），
        # `Far_Out.` 和 `Far_Out` 必须认成同一个族，否则恢复清单里出现重复项
        latest[packer.slug_key(r["slug"])] = r
    out = list(latest.values())
    out.sort(key=lambda r: r["slug"].lower())
    log(f"远端一共 {len(rows)} 个字体包，去重后 {len(out)} 个族")
    return out


def read_app_library_dir():
    """从主程序自己的配置里读「当前字体库在哪」。

    为什么要读它：**新装的机器字体库是空的**，`GET /fonts` 一个路径都没有，
    没法从字体路径推断库根。只读一个字段，不写。
    """
    base = os.environ.get("APPDATA") or ""
    if not base:
        return ""
    for name in ("FontManager", "ZFontManager"):
        path = os.path.join(base, name, "state.json")
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("libraryDirEnabled") is False or data.get("library_dir_enabled") is False:
            continue
        for key in ("libraryDir", "library_dir"):
            val = (data.get(key) or "").strip()
            if val and os.path.isdir(val):
                return val
        for key in ("libraryDirs", "library_dirs"):
            for val in data.get(key) or []:
                if isinstance(val, str) and os.path.isdir(val):
                    return val
    return ""


def guess_library_dir(faces):
    """从字体路径推断库根：所有字体所在目录的公共祖先。

    只有一个族、或者字体散在各处时这个推断不可靠，宁可不给 —— 调用方会退回
    主程序配置或让用户手填。写错目标目录等于往人家硬盘里乱放文件。
    """
    dirs = sorted({os.path.dirname(f.get("path") or "") for f in faces if f.get("path")})
    dirs = [d for d in dirs if d]
    if len(dirs) < 2:
        return ""
    try:
        common = os.path.commonpath(dirs)
    except (ValueError, TypeError):
        return ""
    if not common or common.rstrip("\\/") == "":
        return ""
    if len(common.rstrip("\\/")) <= 3:  # 只剩 `E:` 这种盘根，太顶层，不敢用
        return ""
    return common


def library_dir_for_restore(cfg, faces, log=print):
    """决定「下载回来的字体放哪」：设置里填的 > 主程序当前库 > 路径推断。"""
    explicit = (cfg.get("restoreDir") or "").strip()
    if explicit:
        if not os.path.isdir(explicit):
            try:
                os.makedirs(explicit, exist_ok=True)
            except OSError as e:
                raise RuntimeError(f"设置的恢复目录建不出来：{explicit}（{e}）") from None
        log(f"恢复目录（设置里指定）：{explicit}")
        return explicit
    app = read_app_library_dir()
    if app:
        log(f"恢复目录（主程序当前字体库）：{app}")
        return app
    guess = guess_library_dir(faces)
    if guess:
        log(f"恢复目录（按字体路径推断）：{guess}")
        return guess
    raise RuntimeError("找不到字体库目录：设置面板 →「恢复」页里手动填一个")


def _safe_dirname(family):
    """字族名 → 目录名。和 packer.slugify 不同：这里**保留空格**（目录名更可读）。"""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", (family or "").strip())
    name = name.strip(" .")
    return (name or "family")[:80]


def _norm(family):
    return packer.slug_key(family)


def plan_restore(remote, local_families, overwrite=False, only=None):
    """比对远端与本地，分出「要下载」和「本地已有」。

    `only` 可以是单个族名，也可以是**族名列表**（面板「补缺」一次勾选多个用）。
    匹配键是归一化 slug（`slug_key`）：老包被 GitHub 改写过名字（`!`→`.`）也能对上。
    """
    local_map = {}
    for fam in local_families:
        local_map[_norm(fam)] = fam
    items, skipped = [], []
    if isinstance(only, str):
        want = {_norm(only)} if only else set()
    else:
        want = {_norm(x) for x in (only or []) if x}
    for row in remote:
        key = packer.slug_key(row["slug"])
        if want and key not in want:
            continue
        if key in local_map and not overwrite:
            skipped.append({"family": local_map[key], "reason": "本地已有这个字族"})
            continue
        items.append(row)
    return items, skipped


def run_restore(gh, api, cfg, state, dry_run=False, overwrite=False, only=None,
                log=print, progress=None):
    """从 GitHub 把字体包下载回本机字体库，然后让主程序重扫。"""
    _report(progress, "restore-scan")
    faces = api.faces()
    # 只认**字体库**里的族算「本机已有」：C://Windows//Fonts 的系统字体不算数 ——
    # 否则远端备份里同名的库字体会被系统字体占位跳过，用户永远拿不回来
    # （2026-11-08 真 bug：新库里只有系统字体时，恢复把所有包都标成「本地已有」）
    local_families = list(group_by_family(faces, library_only=True).keys())
    led = _ledger(state, faces)
    log(f"本机字体库现有 {len(local_families)} 个字族（系统字体不计），{len(faces)} 个字体文件")

    owner, name, _branch = ensure_repo(gh, cfg, state, log, create=False)
    _report(progress, "restore-plan")
    remote = list_remote(gh, owner, name, log)
    items, skipped = plan_restore(remote, local_families, overwrite, only)
    need_bytes = sum(r["size"] for r in items)
    log(f"计划：下载 {len(items)} 个族（{need_bytes/1048576:.1f} MB），"
        f"跳过 {len(skipped)} 个（本机已有）")
    _report(progress, "restore-plan", total=len(items), bytesTotal=need_bytes,
            message=f"{len(items)} 个族待下载")

    target_dir = ""
    if not dry_run:
        target_dir = library_dir_for_restore(cfg, faces, log)

    summary = {"planned": len(items), "downloaded": 0, "bytes": 0, "failed": [],
               "conflicts": [], "skipped": len(skipped), "rescan": ""}
    if dry_run:
        for row in items[:50]:
            log(f"  会下载：{row['slug']}（{row['size']/1048576:.2f} MB，{row['tag']}）")
        state["lastRestorePlan"] = {
            "at": int(time.time()),
            "target": target_dir or "",
            "families": [{"slug": r["slug"], "bytes": r["size"]} for r in items],
        }
        store.save_state(state)
        _report(progress, "done", done=0, total=len(items), bytesTotal=need_bytes,
                message=f"预演完成：{len(items)} 个族可从远端恢复")
        return summary

    tmpdir = tempfile.mkdtemp(prefix="font-restore-")
    try:
        for index, row in enumerate(items, start=1):
            _report(progress, "download", done=summary["downloaded"], total=len(items),
                    index=index, family=row["slug"], bytes=summary["bytes"],
                    bytesTotal=need_bytes)
            tmp_zip = os.path.join(tmpdir, f"{index:05d}.zip")
            try:
                size = gh.download_asset(owner, name, row["assetId"], tmp_zip)
                digest = packer.sha256_path(tmp_zip)
                if not digest.startswith(row["hash12"]):
                    raise RuntimeError(f"下载内容校验失败（sha256={digest[:12]}，"
                                       f"期望 {row['hash12']}）")
                manifest = packer.read_manifest(tmp_zip) or {}
                family = (manifest.get("family") or "").strip() or row["slug"]
                dest = os.path.join(target_dir, _safe_dirname(family))
                _report(progress, "unpack", done=summary["downloaded"], total=len(items),
                        index=index, family=family, bytes=summary["bytes"],
                        bytesTotal=need_bytes)
                res = packer.unpack_family(tmp_zip, dest, skip_existing=not overwrite)
                summary["downloaded"] += 1
                summary["bytes"] += size
                if res["conflicts"]:
                    summary["conflicts"].extend(
                        {"family": family, "path": p} for p in res["conflicts"])
                    log(f"  ! {family}：{len(res['conflicts'])} 个文件本机已有且内容不同，"
                        f"未覆盖（要覆盖请勾「覆盖本机同名文件」）")
                led["families"][family] = {
                    "hash": digest, "asset": row["asset"], "assetId": row["assetId"],
                    "bucket": row["tag"], "size": size,
                    "fonts": len(res["fonts"]) + len(res["skipped"]),
                    "docs": len(res["docs"]), "at": int(time.time() * 1000),
                    "fingerprint": "", "fromRemote": True,
                }
                log(f"  ↓ {family}：{len(res['fonts'])} 个文件写入 {dest}")
                _report(progress, "download", done=summary["downloaded"], total=len(items),
                        index=index, family=family, bytes=summary["bytes"],
                        bytesTotal=need_bytes)
            except Exception as e:
                summary["failed"].append({"family": row["slug"], "error": str(e)})
                log(f"  ✗ {row['slug']}：{e}")
            finally:
                try:
                    os.remove(tmp_zip)
                except OSError:
                    pass
    finally:
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass

    if summary["downloaded"]:
        _report(progress, "rescan", done=summary["downloaded"], total=len(items),
                bytes=summary["bytes"], bytesTotal=need_bytes, message="让主程序重新扫描字体库…")
        try:
            api.post("/fonts/scan")
            after = api.faces()
            summary["rescan"] = f"重扫完成，本机现在 {len(group_by_family(after))} 个字族"
            log(summary["rescan"])
        except Exception as e:
            summary["rescan"] = f"重扫失败（字体已经写进库，手动点一次「重新扫描」即可）：{e}"
            log(summary["rescan"])

    state["lastRestoreAt"] = int(time.time() * 1000)
    store.save_state(state)
    log(f"恢复结束：下载 {summary['downloaded']} 个族 / {summary['bytes']/1048576:.1f} MB，"
        f"失败 {len(summary['failed'])}，文件冲突 {len(summary['conflicts'])}")
    _report(progress, "done", done=summary["downloaded"], total=len(items),
            bytes=summary["bytes"], bytesTotal=need_bytes,
            message=(f"恢复完成：{summary['downloaded']} 个族"
                     + (f"，{len(summary['failed'])} 个失败" if summary["failed"] else "")
                     + (f"，{len(summary['conflicts'])} 个文件冲突未覆盖"
                        if summary["conflicts"] else "")))
    return summary
