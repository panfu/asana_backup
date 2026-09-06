"""后台清理线程 —— bridge CleanupExpiredAsanaBackups 的移植（OSS → 本地文件）。

- 完成超过 ZIP_TTL_HOURS（3 小时）的任务：删除 ZIP 文件与记录
- 终态超过 JOB_RECORD_TTL_HOURS 的失败/陈旧记录：清残留工作目录与记录
- 残留 token 的任务（如进程中断）：尽力撤销后抹除
- 超龄会话与孤儿工作目录一并清理
"""

from __future__ import annotations

import logging
import shutil
import threading
from datetime import timedelta
from pathlib import Path

from . import oauth
from .config import settings
from .crypto import TokenCipher
from .store import Store, utcnow

log = logging.getLogger(__name__)


def _remove(path: Path | None) -> None:
    if path and path.exists():
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except OSError as e:
            log.warning("cleanup failed for %s: %s", path, e)


def sweep_once(store: Store, cipher: TokenCipher) -> dict[str, int]:
    stats = {"expired_zips": 0, "stale_jobs": 0, "revoked_tokens": 0, "sessions": 0, "workdirs": 0}
    now = utcnow()

    # 1. 完成但已过期：删 ZIP 文件 + 记录
    for job in store.expired_completed_jobs(now):
        zip_path = Path(job["zip_path"]) if job.get("zip_path") else None
        if zip_path and zip_path.exists():
            _remove(zip_path)
            stats["expired_zips"] += 1
        store.delete_job(job["id"])
        log.info("expired backup removed: job=%s", job["id"])

    # 2. 超龄终态任务：清残留目录 + 记录
    for job in store.stale_jobs(now - timedelta(hours=settings.job_record_ttl_hours)):
        _remove(settings.data_dir / "work" / job["id"])
        if job.get("zip_path"):
            _remove(Path(job["zip_path"]))
        store.delete_job(job["id"])
        stats["stale_jobs"] += 1

    # 3. 进程中断等遗留的 token：尽力撤销后抹除
    for job in store.jobs_with_live_tokens():
        try:
            access = cipher.decrypt(job["token_enc"]) if job.get("token_enc") else None
            refresh = cipher.decrypt(job["refresh_enc"]) if job.get("refresh_enc") else None
            if oauth.revoke_token(access, refresh):
                stats["revoked_tokens"] += 1
        except Exception as e:  # noqa: BLE001
            log.warning("leftover token revoke failed: job=%s (%s)", job["id"], e)
        store.wipe_job_tokens(job["id"])

    # 4. 超龄会话
    stats["sessions"] = store.purge_stale_sessions(now - timedelta(hours=settings.job_record_ttl_hours))

    # 5. 孤儿工作目录（无对应任务记录）
    work_root = settings.data_dir / "work"
    if work_root.is_dir():
        for child in work_root.iterdir():
            if child.is_dir() and not store.get_job(child.name):
                _remove(child)
                stats["workdirs"] += 1

    return stats


def start_cleanup_thread(store: Store, cipher: TokenCipher) -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()

    def _loop() -> None:
        while not stop.is_set():
            try:
                sweep_once(store, cipher)
            except Exception as e:  # noqa: BLE001 — 清理线程永不退出
                log.error("cleanup sweep failed: %s", e)
            stop.wait(settings.cleanup_interval_seconds)

    thread = threading.Thread(target=_loop, name="asana-backup-cleanup", daemon=True)
    thread.start()
    return thread, stop
