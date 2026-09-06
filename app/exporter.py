"""导出任务运行器 —— ~/ar/bridge AsanaBackupService 的移植。

执行模型（对应 bridge 的 Job 链）：
    run_export          → 逐项目列出任务 → 逐任务导出 → 打包 ZIP → 完成
    export_task         → 单任务导出（详情/附件/评论/子任务任一步失败都降级，md 必落盘）
    finalize            → 工作目录打包 zip（内容根 Asana/），校验 md 数量 > 0

差异（均为需求要求）：完成即撤销 OAuth token 并抹除密文；ZIP 3 小时过期。
"""

from __future__ import annotations

import json
import logging
import secrets
import shutil
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import oauth
from . import store as store_mod
from .asana_client import AsanaApiError, AsanaClient
from .config import settings
from .crypto import TokenCipher
from .store import Store
from .vault import (
    build_markdown,
    is_external_attachment,
    safe_attachment_filename,
    sanitize,
    task_markdown_filename,
)

log = logging.getLogger(__name__)

EXPORT_OPTIONS = {
    "include_completed": True,
    "include_comments": True,
    "include_subtasks": True,
    "include_attachments": True,
}


def normalize_options(raw: dict[str, Any] | None) -> dict[str, bool]:
    opts = {**EXPORT_OPTIONS}
    for key in EXPORT_OPTIONS:
        if raw and key in raw:
            opts[key] = bool(raw[key])
    return opts


class TokenProvider:
    """任务期间持有 token：过期自动刷新并回写密文（bridge freshAccessToken 的移植）。

    OAuth 模式下 refresh_token 会被 Asana 轮换，新值必须存回；开发模式 PAT
    永不过期（expires_at 为空则跳过刷新）。
    """

    def __init__(self, store: Store, cipher: TokenCipher, job: dict[str, Any]) -> None:
        self._store = store
        self._cipher = cipher
        self._job_id = job["id"]
        self._access: str | None = cipher.decrypt(job["token_enc"]) if job.get("token_enc") else None
        self._refresh: str | None = cipher.decrypt(job["refresh_enc"]) if job.get("refresh_enc") else None
        self._expires_at = store_mod.parse_iso(job.get("token_expires_at"))

    def access_token(self) -> str:
        if self._access is None:
            raise RuntimeError("token 已被清除，无法继续任务")
        if self._expires_at is None or self._expires_at > store_mod.utcnow() + timedelta(minutes=5):
            return self._access
        if not self._refresh:
            raise RuntimeError("Asana 授权已过期且无 refresh_token，请重新连接 Asana")
        data = oauth.refresh_access_token(self._refresh)
        self._access = data["access_token"]
        if data.get("refresh_token"):
            self._refresh = data["refresh_token"]  # Asana 会轮换 refresh_token
        self._expires_at = store_mod.utcnow() + timedelta(seconds=int(data.get("expires_in") or 3600))
        self._store.update_job(
            self._job_id,
            token_enc=self._cipher.encrypt(self._access),
            refresh_enc=self._cipher.encrypt(self._refresh) if self._refresh else None,
            token_expires_at=store_mod.iso(self._expires_at),
        )
        return self._access

    def revoke_and_wipe(self) -> None:
        """终态收尾：撤销授权（尽力而为）+ 抹掉任务里的 token 密文。"""
        try:
            oauth.revoke_token(self._access, self._refresh)
        finally:
            self._access = None
            self._refresh = None
            self._store.wipe_job_tokens(self._job_id)


def _fetch_subtasks(client: AsanaClient, task_gid: str, cap: list[int]) -> list[dict[str, Any]]:
    """递归拉取子任务；深度与数量都设上限，避免异常项目拖垮导出。"""
    if cap[0] >= settings.max_subtasks_per_task:
        return []

    def _walk(gid: str, depth: int) -> list[dict[str, Any]]:
        if depth > settings.max_subtask_depth or cap[0] >= settings.max_subtasks_per_task:
            return []
        items = client.get_task_subtasks(gid)
        out: list[dict[str, Any]] = []
        for st in items:
            if cap[0] >= settings.max_subtasks_per_task:
                break
            cap[0] += 1
            children = _walk(st["gid"], depth + 1)
            if children:
                st = {**st, "children": children}
            out.append(st)
        return out

    return _walk(task_gid, 1)


def export_task(
    client: AsanaClient,
    task_gid: str,
    fallback_name: str,
    base_dir: Path,
    options: dict[str, bool],
) -> None:
    """导出单个任务 —— bridge exportTask 的移植（含子任务/外链附件扩展）。

    健壮性原则：任务详情/评论/附件任何一步失败都不阻断 Markdown 落盘
    （降级为占位内容），用户拿到的 zip 中每个任务必有 md 文件。
    """
    # 1. 任务详情（失败则用列表数据降级，仍写 md）
    task: dict[str, Any] = {}
    try:
        task = client.get_task_for_backup(task_gid)
    except (AsanaApiError, Exception) as e:  # noqa: BLE001 — bridge 同款：单任务失败不阻断
        log.warning("task detail fetch failed, writing stub: %s (%s)", task_gid, e)
    if not task:
        task = {"gid": task_gid, "name": fallback_name or task_gid, "notes": ""}

    project_name = sanitize((((task.get("projects") or [{}])[0]) or {}).get("name") or "Unknown")
    section_name = sanitize(
        ((((task.get("memberships") or [{}])[0]) or {}).get("section") or {}).get("name") or "Uncategorized"
    )
    # 附件按项目集中：{Project}/attachments/。项目文件夹自包含——单独解压、
    # 整体移动、多次导出合并都不影响 md 内的相对引用（../attachments/）
    section_dir = base_dir / project_name / section_name
    attachments_dir = base_dir / project_name / "attachments"
    section_dir.mkdir(parents=True, exist_ok=True)

    # 2. 附件：托管在 Asana 的才下载；外链（Drive/Figma 等）只保留原链接
    attachment_map: dict[str, str] = {}
    external_attachments: list[dict[str, Any]] = []
    if options["include_attachments"]:
        try:
            for att in client.get_task_attachments(task_gid):
                try:
                    if is_external_attachment(att):
                        external_attachments.append(att)
                        continue
                    fresh_url = client.get_fresh_attachment_download_url(att["gid"])
                    if not fresh_url:
                        continue
                    unique_name = f"{task_gid}_{safe_attachment_filename(att, att['gid'])}"
                    data = client.download_file(fresh_url, settings.max_attachment_mb * 1024 * 1024)
                    if data is not None:  # None = 下载失败或 <50 字节的过期错误页
                        attachments_dir.mkdir(parents=True, exist_ok=True)
                        (attachments_dir / unique_name).write_bytes(data)
                        attachment_map[att["gid"]] = unique_name
                except Exception as e:  # noqa: BLE001
                    log.warning("attachment failed: %s (%s)", att.get("gid"), e)
        except Exception as e:  # noqa: BLE001
            log.warning("attachment list fetch failed: %s (%s)", task_gid, e)

    # 3. 评论（失败降级为空，不阻断 md）
    comments: list[dict[str, Any]] = []
    if options["include_comments"]:
        try:
            comments = client.get_task_comments(task_gid)
        except Exception as e:  # noqa: BLE001
            log.warning("comments fetch failed: %s (%s)", task_gid, e)

    # 4. 子任务（失败降级为空，不阻断 md）
    subtasks: list[dict[str, Any]] = []
    if options["include_subtasks"]:
        try:
            subtasks = _fetch_subtasks(client, task_gid, cap=[0])
        except Exception as e:  # noqa: BLE001
            log.warning("subtasks fetch failed: %s (%s)", task_gid, e)

    # 5. 写 Markdown（任何情况下必须落盘）
    markdown = build_markdown(task, comments, attachment_map, external_attachments, subtasks, task_gid)
    filename = task_markdown_filename(task, task_gid, fallback_name)
    (section_dir / filename).write_text(markdown, encoding="utf-8")


def build_zip(work_dir: Path, zip_path: Path) -> dict[str, int]:
    """工作目录流式打包（内容根 Asana/）—— bridge buildWorkDirZip 的移植。"""
    file_count = 0
    md_count = 0
    attachment_count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(work_dir.rglob("*")):
            if not path.is_file():
                continue
            relative = "Asana/" + path.relative_to(work_dir).as_posix()
            zf.write(path, relative)
            file_count += 1
            if path.suffix == ".md":
                md_count += 1
            elif "/attachments/" in f"/{path.relative_to(work_dir).as_posix()}":
                attachment_count += 1
    if md_count == 0:
        raise RuntimeError(
            "No markdown files were generated — task export failed for every task "
            "(check task detail/comments fetch errors in logs)"
        )
    return {
        "files": file_count,
        "markdown": md_count,
        "attachments": attachment_count,
        "size_bytes": zip_path.stat().st_size,
    }


def _remove_dir(path: Path | None) -> None:
    if path and path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def run_export(store: Store, cipher: TokenCipher, job_id: str) -> None:
    """后台线程入口：拉取数据与附件 → 生成 Markdown Vault → 打包 ZIP。"""
    job = store.get_job(job_id)
    if not job:
        return
    work_dir = settings.data_dir / "work" / job_id
    zip_path = settings.data_dir / "zips" / f"{job_id}.zip"
    provider = TokenProvider(store, cipher, job)
    options = json.loads(job["options_json"])
    projects = json.loads(job["projects_json"])

    try:
        store.update_job(job_id, status=store_mod.JOB_PROCESSING)
        client = AsanaClient(provider.access_token())

        def renew_client() -> AsanaClient:
            """401（token 中途过期）时刷新并重建客户端。"""
            client.close()
            return AsanaClient(provider.access_token())

        try:
            task_budget = settings.max_tasks_per_export
            for project in projects:
                gid = project["gid"]
                store.update_job_project(job_id, gid, {"status": store_mod.JOB_PROCESSING})
                tasks = client.get_all_project_tasks(gid)
                if not options["include_completed"]:
                    tasks = [t for t in tasks if not t.get("completed")]
                if len(tasks) > task_budget:
                    tasks = tasks[:task_budget]
                task_budget -= len(tasks)
                store.update_job_project(job_id, gid, {"total": len(tasks)})

                done = 0
                for task in tasks:
                    try:
                        export_task(client, task["gid"], task.get("name", ""), work_dir, options)
                    except AsanaApiError as e:
                        if e.status_code == 401:
                            client = renew_client()
                            try:
                                export_task(client, task["gid"], task.get("name", ""), work_dir, options)
                            except Exception as e2:  # noqa: BLE001
                                log.warning("task export failed after token refresh: %s (%s)", task.get("gid"), e2)
                        else:
                            log.warning("task export failed: %s (%s)", task.get("gid"), e)
                    except Exception as e:  # noqa: BLE001 — 单任务失败不阻断整体，但留痕
                        log.warning(
                            "task export failed: job=%s project=%s task=%s (%s)",
                            job_id,
                            gid,
                            task.get("gid"),
                            e,
                        )
                    done += 1
                    if done % 3 == 0 or done == len(tasks):
                        store.update_job_project(job_id, gid, {"processed": done})
                store.update_job_project(job_id, gid, {"status": store_mod.JOB_COMPLETED})
        finally:
            client.close()

        stats = build_zip(work_dir, zip_path)
        store.update_job(
            job_id,
            status=store_mod.JOB_COMPLETED,
            zip_path=str(zip_path),
            download_token=secrets.token_urlsafe(24),
            expires_at=store_mod.iso(store_mod.utcnow() + timedelta(hours=settings.zip_ttl_hours)),
            stats_json=json.dumps(stats, ensure_ascii=False),
            completed_at=store_mod.iso(store_mod.utcnow()),
        )
        log.info("export completed: job=%s stats=%s", job_id, stats)
    except Exception as e:  # noqa: BLE001
        log.error("export failed: job=%s (%s)", job_id, e)
        store.update_job(job_id, status=store_mod.JOB_FAILED, error=str(e)[:500])
        _remove_dir(work_dir)
        zip_path.unlink(missing_ok=True)
    else:
        _remove_dir(work_dir)
    finally:
        # 隐私承诺：任务终态立即撤销授权并抹除 token（源数据目录已删，仅剩 ZIP 待 3h 过期）
        provider.revoke_and_wipe()
