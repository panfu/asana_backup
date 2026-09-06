"""Asana API 客户端 —— 逐条移植 ~/ar/bridge 的 AsanaService（已验证可用）：

- 滑动窗口限流（Free 档 ~150 req/min，按 2 req/s）
- 429/5xx 指数退避重试（2s/4s/6s，最多 3 次），其余 4xx 不重试
- next_page.offset 翻页
- opt_fields 与 bridge 完全一致（多一个子任务接口，bridge 未用到）
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)


class AsanaApiError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# bridge getTaskForBackup 的字段清单（原样保留）
TASK_BACKUP_FIELDS = ",".join(
    [
        "gid",
        "name",
        "completed",
        "notes",
        "permalink_url",
        "created_by.name",
        "created_by.email",
        "assignee.name",
        "assignee.gid",
        "due_on",
        "created_at",
        "modified_at",
        "completed_at",
        "projects.name",
        "projects.gid",
        "memberships.section.name",
        "custom_fields.name",
        "custom_fields.display_value",
        "custom_fields.text_value",
        "custom_fields.number_value",
        "tags.name",
    ]
)

# 子任务以列表接口一次取渲染所需的字段，避免每个子任务再查一次详情
SUBTASK_FIELDS = ",".join(
    [
        "gid",
        "name",
        "completed",
        "notes",
        "permalink_url",
        "assignee.name",
        "due_on",
        "created_at",
    ]
)


class AsanaClient:
    def __init__(self, access_token: str) -> None:
        self._token = access_token
        self._base_url = settings.asana_base_url.rstrip("/")
        self._rate_limit = settings.asana_rate_limit
        self._retry_times = settings.asana_retry_times
        self._timeout = settings.asana_timeout
        self._request_timestamps: list[float] = []
        self._http = httpx.Client(
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            timeout=self._timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> AsanaClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------- 请求核心（bridge makeRequest 的移植） ----------

    def _rate_limit_wait(self) -> None:
        now = time.monotonic()
        self._request_timestamps = [ts for ts in self._request_timestamps if ts > now - 1]
        if len(self._request_timestamps) >= self._rate_limit:
            oldest = self._request_timestamps[0]
            sleep_time = 1 - (now - oldest)
            if sleep_time > 0:
                time.sleep(sleep_time)
        self._request_timestamps.append(time.monotonic())

    def _request(self, method: str, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        attempt = 0
        last_exc: Exception | None = None
        while attempt < self._retry_times:
            try:
                self._rate_limit_wait()
                resp = self._http.request(method, f"{self._base_url}{endpoint}", params=params)
                if resp.status_code >= 400:
                    try:
                        message = resp.json().get("errors", [{}])[0].get("message", f"HTTP {resp.status_code}")
                    except Exception:
                        message = f"HTTP {resp.status_code}"
                    raise AsanaApiError(f"Asana API error: {message}", resp.status_code)
                return resp.json()
            except AsanaApiError as e:
                last_exc = e
                retryable = e.status_code in (429,) or (e.status_code or 0) >= 500
                attempt += 1
                if not retryable or attempt >= self._retry_times:
                    log.error("Asana request failed: %s %s (%s)", method, endpoint, e)
                    raise
                time.sleep(attempt * 2)  # 指数退避：2s, 4s
            except httpx.HTTPError as e:
                last_exc = e
                attempt += 1
                if attempt >= self._retry_times:
                    log.error("Asana request transport failed: %s %s (%s)", method, endpoint, e)
                    raise AsanaApiError(f"Asana transport error: {e}") from e
                time.sleep(attempt * 2)
        raise last_exc or AsanaApiError("unreachable")

    def _paginate(self, endpoint: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        offset: str | None = None
        while True:
            query = {**params, "limit": 100}
            if offset:
                query["offset"] = offset
            data = self._request("GET", endpoint, query)
            yield from data.get("data") or []
            next_page = data.get("next_page") or {}
            offset = next_page.get("offset")
            if not offset:
                return

    # ---------- 业务方法（对齐 bridge） ----------

    def get_me(self) -> dict[str, Any]:
        return self._request("GET", "/users/me", {"opt_fields": "gid,name,email"}).get("data") or {}

    def get_workspaces(self) -> list[dict[str, Any]]:
        return list(self._paginate("/workspaces", {"opt_fields": "gid,name,is_organization"}))

    def get_projects(self, workspace_gid: str) -> list[dict[str, Any]]:
        return list(
            self._paginate(
                "/projects",
                {
                    "workspace": workspace_gid,
                    "archived": "false",
                    "opt_fields": "gid,name,notes,due_on,created_at,modified_at,memberships.section.name",
                },
            )
        )

    def get_all_project_tasks(self, project_gid: str) -> list[dict[str, Any]]:
        """拉取项目全部任务（自动翻页），仅取概要字段 —— bridge 同款。"""
        return list(self._paginate("/tasks", {"project": project_gid, "opt_fields": "gid,name,completed"}))

    def get_task_for_backup(self, gid: str) -> dict[str, Any]:
        return self._request("GET", f"/tasks/{gid}", {"opt_fields": TASK_BACKUP_FIELDS}).get("data") or {}

    def get_task_subtasks(self, task_gid: str) -> list[dict[str, Any]]:
        return list(self._paginate(f"/tasks/{task_gid}/subtasks", {"opt_fields": SUBTASK_FIELDS}))

    def get_task_attachments(self, task_gid: str) -> list[dict[str, Any]]:
        return list(
            self._paginate(
                f"/tasks/{task_gid}/attachments",
                {
                    "opt_fields": (
                        "gid,name,resource_subtype,host,created_at,created_by.name,download_url,file_type,size"
                    )
                },
            )
        )

    def get_fresh_attachment_download_url(self, attachment_gid: str) -> str | None:
        """download_url 含 e= 过期参数（约 30 分钟），每次下载前必须重新取。"""
        data = self._request("GET", f"/attachments/{attachment_gid}", {"opt_fields": "download_url"}).get("data") or {}
        return data.get("download_url")

    def get_task_comments(self, task_gid: str) -> list[dict[str, Any]]:
        """任务评论（stories 中 type=comment 的才导出，过滤系统事件）。"""
        stories = self._paginate(
            f"/tasks/{task_gid}/stories",
            {"opt_fields": "gid,type,text,created_at,created_by.name,created_by.email"},
        )
        return [s for s in stories if s.get("type") == "comment"]

    def download_file(self, url: str, max_bytes: int) -> bytes | None:
        """下载附件内容；bridge 验证过的经验：<50 字节视为过期错误页，丢弃。"""
        try:
            with self._http.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return None
                buf = bytearray()
                for chunk in resp.iter_bytes(65536):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        log.warning("attachment exceeds size cap, truncated: %s", url)
                        return None
                data = bytes(buf)
                return data if len(data) >= 50 else None
        except httpx.HTTPError as e:
            log.warning("attachment download failed: %s (%s)", url, e)
            return None
