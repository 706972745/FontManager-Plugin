"""GitHub REST 客户端（零依赖，只用标准库）。

设计要点都来自 `软件迭代/GitHub同步实现文档.md`，这里只写代码需要的部分：

- **重试**：5xx 与网络错误退避重试 3 次；403 且 `X-RateLimit-Remaining: 0` 时
  按 `X-RateLimit-Reset` 睡到重置（有上限，不会睡一小时）。
- **302**：下载资产时 GitHub 会跳到 objects.githubusercontent.com，跳转后**不能再带
  Authorization**（会 400）。这里手动处理，不交给 urllib 自动跟随。
- **分页**：`Link: rel="next"` 才是权威，别自己拼 page 参数。
- **代理**：不写死任何代理，走 urllib 默认（读 HTTPS_PROXY / HTTP_PROXY 环境变量）。
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
UA = "FontManager-Plugin-font-sync/0.1"


class GhError(RuntimeError):
    def __init__(self, status, message, body=None):
        super().__init__(f"GitHub {status}: {message}")
        self.status = status
        self.body = body


class Gh:
    def __init__(self, token, timeout=60):
        self.token = token
        self.timeout = timeout

    # ── 底层 ────────────────────────────────────────────────────────────
    def _open(self, req, timeout=None):
        return urllib.request.urlopen(req, timeout=timeout or self.timeout)

    def request(
        self,
        method,
        url,
        json_body=None,
        data=None,
        accept="application/vnd.github+json",
        auth=True,
        allow=(200, 201, 204),
        timeout=None,
    ):
        """发一个请求，返回 (status, 解析后的 JSON 或 None)。"""
        if url.startswith("/"):
            url = API + url
        attempt = 0
        while True:
            attempt += 1
            headers = {"Accept": accept, "User-Agent": UA,
                       "X-GitHub-Api-Version": "2022-11-28"}
            if auth and self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            payload = data
            if json_body is not None:
                payload = json.dumps(json_body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=payload, headers=headers, method=method)
            try:
                with self._open(req, timeout) as resp:
                    status = resp.status
                    raw = resp.read()
                    if status not in allow:
                        raise GhError(status, "unexpected status", raw[:400])
                    if not raw:
                        return status, None
                    try:
                        return status, json.loads(raw.decode("utf-8"))
                    except Exception:
                        return status, None
            except urllib.error.HTTPError as e:
                body = e.read()[:2000]
                text = _error_text(body)
                # 速率限制：睡到重置（最多 15 分钟）
                if e.code in (403, 429) and e.headers.get("X-RateLimit-Remaining") == "0":
                    reset = int(e.headers.get("X-RateLimit-Reset") or 0)
                    wait = max(1, min(reset - int(time.time()) + 2, 900))
                    time.sleep(wait)
                    if attempt <= 3:
                        continue
                if e.code >= 500 and attempt <= 3:
                    time.sleep(2 ** attempt)
                    continue
                raise GhError(e.code, text, body) from None
            except urllib.error.URLError as e:
                if attempt <= 3:
                    time.sleep(2 ** attempt)
                    continue
                raise GhError(0, f"连不上 GitHub：{e.reason}") from None

    # ── 账号与仓库 ──────────────────────────────────────────────────────
    def whoami(self):
        _, data = self.request("GET", "/user")
        return data or {}

    def token_scopes(self):
        """classic token 的权限范围藏在响应头里；fine-grained 没有这个头。"""
        req = urllib.request.Request(
            API + "/user",
            headers={"Authorization": f"Bearer {self.token}", "User-Agent": UA,
                     "Accept": "application/vnd.github+json"},
        )
        try:
            with self._open(req) as resp:
                return (resp.headers.get("X-OAuth-Scopes") or "").replace(" ", "").split(",")
        except Exception:
            return []

    def get_repo(self, owner, name):
        """返回仓库 JSON；不存在返回 None。"""
        try:
            _, data = self.request("GET", f"/repos/{owner}/{name}")
            return data
        except GhError as e:
            if e.status == 404:
                return None
            raise

    def create_private_repo(self, name, description=""):
        _, data = self.request(
            "POST", "/user/repos",
            json_body={"name": name, "private": True, "description": description,
                       "auto_init": True, "has_issues": False, "has_wiki": False,
                       "has_projects": False},
        )
        return data

    def repo_state(self, owner, name):
        """给设置面板用的一份体检：存在与否、是否私有、空仓、默认分支、能否匿名读。"""
        repo = self.get_repo(owner, name)
        if repo is None:
            return {"exists": False, "private": None, "empty": None,
                    "defaultBranch": "", "anonymousReadable": None}
        branch = repo.get("default_branch") or "main"
        return {
            "exists": True,
            "private": bool(repo.get("private")),
            "empty": not self.branch_sha(owner, name, branch),
            "defaultBranch": branch,
            "anonymousReadable": self.repo_is_anonymous_readable(owner, name),
        }

    def branch_sha(self, owner, name, branch):
        """分支的最新提交；空仓（还没有任何 commit）返回 None。"""
        try:
            _, ref = self.request("GET", f"/repos/{owner}/{name}/git/ref/heads/{branch}",
                                  allow=(200, 404, 409))
            return ((ref or {}).get("object") or {}).get("sha")
        except GhError as e:
            if e.status in (404, 409):
                return None
            raise

    def init_empty_repo(self, owner, name, branch="main", readme=None):
        """空仓库先放一个 README 建出首个提交 —— 否则 Git Data API 没有 base tree 可用。"""
        body = readme or (
            "# 字体库同步\n\n"
            "这个仓库由 FontManager 的「字体库同步」插件使用，用来存放按字族打包的字体 zip。\n\n"
            "- 内容在 **Releases** 的 `lib-0001`、`lib-0002`… 分桶里，不在 git 历史里；\n"
            "- `ops/` 下是同步记账（每台机器一个目录）。\n\n"
            "⚠️ 请保持这个仓库 **私有**。公开会导致字体被公开分发。\n"
        )
        import base64 as _b64

        self.request(
            "PUT", f"/repos/{owner}/{name}/contents/README.md",
            json_body={"message": "init: font-sync repo",
                       "content": _b64.b64encode(body.encode("utf-8")).decode("ascii"),
                       "branch": branch},
            allow=(201, 200, 422),
        )
        return self.branch_sha(owner, name, branch)

    def repo_is_anonymous_readable(self, owner, name):
        """匿名能不能读：这是「仓库到底是不是私有」的唯一可信判据。

        `private: true` 字段不够（fork / 转移 / 组织策略都可能变），必须**不带令牌**探一次。
        返回 True = 公网可读 = 绝对不能往里放字体。
        """
        req = urllib.request.Request(
            f"{API}/repos/{owner}/{name}",
            headers={"Accept": "application/vnd.github+json", "User-Agent": UA},
        )
        try:
            with self._open(req, 20) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as e:
            return e.code != 404  # 403/451 之类也视作「不公开」，但不要当成私有来信任
        except Exception:
            return False

    # ── release（分桶放 asset） ─────────────────────────────────────────
    def list_releases_simple(self, owner, name):
        out, page = [], 1
        while True:
            _, data = self.request("GET", f"/repos/{owner}/{name}/releases?per_page=100&page={page}")
            data = data or []
            out.extend(data)
            if len(data) < 100:
                return out
            page += 1
            if page > 50:  # 5000 个 release 还没找完，肯定是配置错了
                return out

    def find_or_create_bucket(self, owner, name, prefix="lib", max_assets=888):
        """找到第一个还没装满的桶（release tag = `lib-0001`…），没有就建空的。

        一个 release 上限 1000 个 asset，取 888 留余量（文档 §3）。
        返回 (release_id, tag)。
        """
        releases = {r.get("tag_name"): r for r in self.list_releases_simple(owner, name)}
        idx = 1
        while idx <= 200:
            tag = f"{prefix}-{idx:04d}"
            rel = releases.get(tag)
            if rel is None:
                rel = self.create_release(owner, name, tag)
                return rel["id"], tag
            if len(self.list_assets(owner, name, rel["id"])) < max_assets:
                return rel["id"], tag
            idx += 1
        raise GhError(0, "桶用尽了（>200 个 release），先清理旧同步")

    def create_release(self, owner, name, tag, body=None):
        _, data = self.request(
            "POST", f"/repos/{owner}/{name}/releases",
            json_body={"tag_name": tag, "name": tag, "body": body or
                       "FontManager 字体库同步分桶。asset 是各字族的 zip，不要手动改。",
                       "draft": False, "prerelease": True},
        )
        return data

    def list_assets(self, owner, name, release_id):
        out, page = [], 1
        while True:
            _, data = self.request(
                "GET", f"/repos/{owner}/{name}/releases/{release_id}/assets?per_page=100&page={page}")
            data = data or []
            out.extend(data)
            if len(data) < 100:
                return out
            page += 1

    def upload_asset(self, owner, name, release_id, filename, blob, timeout=600):
        url = (f"{UPLOADS}/repos/{owner}/{name}/releases/{release_id}/assets?"
               + urllib.parse.urlencode({"name": filename}))
        try:
            status, data = self.request(
                "POST", url, data=blob, accept="application/vnd.github+json",
                allow=(201, 422), timeout=timeout)
        except GhError as e:
            # 同名 asset 已存在：GitHub 回 422，当成功（幂等重传）
            if e.status == 422:
                return None
            raise
        if status == 422:
            return None
        return data

    def delete_asset(self, owner, name, asset_id):
        try:
            self.request("DELETE", f"/repos/{owner}/{name}/releases/assets/{asset_id}",
                         allow=(204, 404))
        except GhError as e:
            if e.status != 404:
                raise

    def download_asset(self, owner, name, asset_id, dest_path, chunk=1 << 20):
        """下载 asset：先拿 302，再**不带令牌**跟随。"""
        req = urllib.request.Request(
            f"{API}/repos/{owner}/{name}/releases/assets/{asset_id}",
            headers={"Accept": "application/octet-stream", "User-Agent": UA,
                     "Authorization": f"Bearer {self.token}"},
        )
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(req, timeout=self.timeout) as resp:
            location = resp.headers.get("Location")
        if not location:
            raise GhError(0, "下载没有拿到 302 跳转")
        req2 = urllib.request.Request(location, headers={"User-Agent": UA})
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        size = 0
        with urllib.request.urlopen(req2, timeout=self.timeout) as r, open(dest_path, "wb") as fh:
            while True:
                block = r.read(chunk)
                if not block:
                    break
                fh.write(block)
                size += len(block)
        return size

    # ── Git Data API（一次提交写多个文件，天然原子） ─────────────────────
    def commit_files(self, owner, name, branch, files, message, retries=6):
        """files: {路径: bytes}。force:false 当 CAS：非快进就被拒，重试即可。

        空仓库（还没有任何提交）会先补一个 README 建出首个提交 ——
        Git Data API 需要 base tree，空仓直接调用会 404。
        """
        last = None
        inited = False
        for attempt in range(retries):
            try:
                _, ref = self.request("GET", f"/repos/{owner}/{name}/git/ref/heads/{branch}")
                head = (ref or {}).get("object", {}).get("sha")
                if not head:
                    raise GhError(0, f"分支 {branch} 不存在")
                _, base_commit = self.request("GET", f"/repos/{owner}/{name}/git/commits/{head}")
                base_tree = base_commit["tree"]["sha"]

                tree = []
                for path, blob in files.items():
                    _, b = self.request("POST", f"/repos/{owner}/{name}/git/blobs",
                                        json_body={"content": blob.decode("utf-8"),
                                                   "encoding": "utf-8"})
                    tree.append({"path": path, "mode": "100644", "type": "blob",
                                 "sha": b["sha"]})
                _, new_tree = self.request("POST", f"/repos/{owner}/{name}/git/trees",
                                           json_body={"base_tree": base_tree, "tree": tree})
                _, commit = self.request(
                    "POST", f"/repos/{owner}/{name}/git/commits",
                    json_body={"message": message, "tree": new_tree["sha"], "parents": [head]})
                self.request("PATCH", f"/repos/{owner}/{name}/git/refs/heads/{branch}",
                             json_body={"sha": commit["sha"], "force": False})
                return commit["sha"]
            except GhError as e:
                last = e
                # 空仓：没有任何提交 → 补 README 建首个提交，然后重来一次
                if e.status in (404, 409) and not inited:
                    inited = True
                    try:
                        self.init_empty_repo(owner, name, branch)
                        continue
                    except Exception:
                        pass
                # 409 / 422 = 竞争失败（别人先提交了），重新读 head 再来
                if e.status in (409, 422, 404):
                    time.sleep(0.4 * (attempt + 1))
                    continue
                raise
        raise last or GhError(0, "提交失败")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _error_text(body):
    try:
        data = json.loads(body.decode("utf-8", "replace"))
        msg = data.get("message") or ""
        errs = data.get("errors")
        if errs:
            msg += " " + json.dumps(errs, ensure_ascii=False)[:300]
        return msg or "unknown"
    except Exception:
        return body.decode("utf-8", "replace")[:200] or "unknown"
