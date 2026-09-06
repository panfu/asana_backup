"""FastAPI 入口：四步向导的全部路由。

流程：OAuth 授权 → 创建导出任务 → 拉取数据与附件 → 生成 Markdown Vault
→ 打包 ZIP → 临时下载链接（3 小时）→ 自动清理。

token 生命周期与产品承诺一致：连接后仅在会话内加密保存用于列出
Workspace/项目；一旦发起导出，token 即转移到任务、任务结束即撤销并抹除。
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import oauth
from .asana_client import AsanaApiError, AsanaClient
from .cleanup import start_cleanup_thread
from .config import settings
from .crypto import TokenCipher
from .exporter import normalize_options, run_export
from .store import Store, iso, parse_iso, utcnow

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
SESSION_COOKIE = "ab_sid"
MAX_PROJECTS_PER_EXPORT = 50


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    if not settings.connectable:
        log.warning(
            "Asana OAuth 未配置（ASANA_CLIENT_ID/SECRET/REDIRECT_URI），且无 ASANA_DEV_TOKEN —— 页面可访问但无法连接"
        )
    app.state.store = Store(settings.data_dir / "asana_backup.db")
    app.state.cipher = TokenCipher(settings.secret_key)
    _, app.state.cleanup_stop = start_cleanup_thread(app.state.store, app.state.cipher)
    yield
    app.state.cleanup_stop.set()
    app.state.store.close()


app = FastAPI(title="Asana → Obsidian Vault", lifespan=lifespan)


# ---------- helpers ----------


def get_store(request: Request) -> Store:
    return request.app.state.store


def current_session(request: Request) -> dict[str, Any] | None:
    return get_store(request).get_session(request.cookies.get(SESSION_COOKIE))


def require_session(request: Request) -> dict[str, Any]:
    session = current_session(request)
    if not session:
        raise HTTPException(401, "请先连接 Asana")
    return session


def set_session_cookie(response: Response, sid: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        sid,
        max_age=int(timedelta(hours=24).total_seconds()),
        httponly=True,
        samesite="lax",
        secure=settings.base_url.startswith("https://"),
        path="/",
    )


def job_public_view(job: dict[str, Any]) -> dict[str, Any]:
    projects = [
        {
            "gid": p["gid"],
            "name": p.get("name"),
            "status": p.get("status"),
            "total": p.get("total", 0),
            "processed": p.get("processed", 0),
        }
        for p in (json_loads(job.get("projects_json")) or [])
    ]
    total = job.get("total_tasks") or 0
    processed = job.get("processed_tasks") or 0
    view: dict[str, Any] = {
        "id": job["id"],
        "status": job["status"],
        "projects": projects,
        "total_tasks": total,
        "processed_tasks": processed,
        "progress": int(processed * 100 / total) if total else 0,
        "created_at": job.get("created_at"),
    }
    if job["status"] == "completed":
        view["expires_at"] = job.get("expires_at")
        view["stats"] = json_loads(job.get("stats_json"))
        view["download_url"] = f"/api/export/{job['id']}/download?token={job.get('download_token')}"
    elif job["status"] == "failed":
        view["error"] = job.get("error")
    return view


def json_loads(raw: str | None) -> Any:
    import json

    return json.loads(raw) if raw else None


# ---------- 页面 ----------


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"ok": "true"}


# ---------- 会话 / OAuth ----------


@app.get("/api/session")
def api_session(request: Request) -> dict[str, Any]:
    session = current_session(request)
    store = get_store(request)
    job = store.get_session_active_job(request.cookies.get(SESSION_COOKIE)) if session else None
    payload: dict[str, Any] = {
        "configured": settings.connectable,
        "dev_mode": settings.dev_mode,
        "connected": bool(session and session.get("token_enc")),
        "identity": None,
        "job": None,
    }
    if session:
        payload["identity"] = {
            "name": session.get("asana_name"),
            "email": session.get("asana_email"),
        }
        payload["job"] = job_public_view(job) if job else None
    return payload


@app.get("/api/connect", include_in_schema=False)
def api_connect(request: Request):
    store = get_store(request)
    if settings.dev_mode:
        # 开发模式：跳过 OAuth，直接用固定 PAT 建立会话（仅本地联调用）
        session = store.create_session()
        cipher: TokenCipher = request.app.state.cipher
        store.update_session(
            session["id"],
            token_enc=cipher.encrypt(settings.asana_dev_token),
            asana_name="Dev Token",
            asana_email="",
        )
        resp = RedirectResponse("/?connected=1", status_code=302)
        set_session_cookie(resp, session["id"])
        return resp

    if not settings.oauth_configured:
        return JSONResponse(
            {"error": "Asana OAuth 未配置，请设置 ASANA_CLIENT_ID / ASANA_CLIENT_SECRET / ASANA_REDIRECT_URI"},
            503,
        )

    session = current_session(request) or store.create_session()
    state = oauth.new_state()
    store.update_session(session["id"], oauth_state=state)
    resp = RedirectResponse(oauth.authorization_url(state), status_code=302)
    set_session_cookie(resp, session["id"])
    return resp


@app.get("/oauth/callback", include_in_schema=False)
def oauth_callback(request: Request):
    store = get_store(request)
    session = current_session(request)

    def fail(message: str):
        return RedirectResponse(f"/?error={message}", status_code=302)

    if not session or not session.get("oauth_state"):
        return fail("session_lost")
    if request.query_params.get("error"):
        return fail("cancelled")
    state = request.query_params.get("state", "")
    if state != session.get("oauth_state"):
        return fail("state_mismatch")
    code = request.query_params.get("code")
    if not code:
        return fail("no_code")

    try:
        token_data = oauth.exchange_code_for_tokens(code)
    except Exception as e:  # noqa: BLE001
        log.error("OAuth callback failed: %s", e)
        return fail("exchange_failed")

    cipher: TokenCipher = request.app.state.cipher
    access = token_data["access_token"]
    refresh = token_data.get("refresh_token")
    expires_at = utcnow() + timedelta(seconds=int(token_data.get("expires_in") or 3600))

    identity: dict[str, Any] = {}
    try:
        with AsanaClient(access) as client:
            identity = client.get_me()
    except Exception as e:  # noqa: BLE001
        log.warning("get_me failed after OAuth: %s", e)

    store.update_session(
        session["id"],
        oauth_state=None,
        asana_gid=identity.get("gid"),
        asana_name=identity.get("name"),
        asana_email=identity.get("email"),
        token_enc=cipher.encrypt(access),
        refresh_enc=cipher.encrypt(refresh) if refresh else None,
        token_expires_at=iso(expires_at),
    )
    return RedirectResponse("/?connected=1", status_code=302)


@app.post("/api/disconnect")
def api_disconnect(request: Request):
    session = require_session(request)
    store = get_store(request)
    cipher: TokenCipher = request.app.state.cipher
    try:
        access = cipher.decrypt(session["token_enc"]) if session.get("token_enc") else None
        refresh = cipher.decrypt(session["refresh_enc"]) if session.get("refresh_enc") else None
        oauth.revoke_token(access, refresh)
    except Exception as e:  # noqa: BLE001
        log.warning("disconnect revoke failed: %s", e)
    store.delete_session(session["id"])
    return {"ok": True}


# ---------- 数据选择 ----------


@app.get("/api/workspaces")
def api_workspaces(request: Request):
    session = require_session(request)
    if not session.get("token_enc"):
        raise HTTPException(401, "授权已用于导出任务并被撤销，请重新连接 Asana")
    token = _fresh_session_token(request, session)
    try:
        with AsanaClient(token) as client:
            return {"workspaces": client.get_workspaces()}
    except AsanaApiError as e:
        raise HTTPException(502, f"Asana API 失败：{e}") from e


@app.get("/api/projects")
def api_projects(request: Request, workspace: str):
    session = require_session(request)
    if not session.get("token_enc"):
        raise HTTPException(401, "授权已用于导出任务并被撤销，请重新连接 Asana")
    if not workspace:
        raise HTTPException(422, "workspace 参数必填")
    token = _fresh_session_token(request, session)
    try:
        with AsanaClient(token) as client:
            projects = client.get_projects(workspace)
        return {
            "projects": [
                {"gid": p.get("gid"), "name": p.get("name"), "notes": (p.get("notes") or "")[:200]} for p in projects
            ]
        }
    except AsanaApiError as e:
        raise HTTPException(502, f"Asana API 失败：{e}") from e


def _fresh_session_token(request: Request, session: dict[str, Any]) -> str:
    """会话期 token 过期则刷新（bridge freshAccessToken 的移植）。"""
    cipher: TokenCipher = request.app.state.cipher
    access = cipher.decrypt(session["token_enc"])
    expires_at = parse_iso(session.get("token_expires_at"))
    if expires_at is None or expires_at > utcnow() + timedelta(minutes=5):
        return access
    refresh = cipher.decrypt(session["refresh_enc"]) if session.get("refresh_enc") else None
    if not refresh:
        raise HTTPException(401, "Asana 授权已过期，请重新连接")
    data = oauth.refresh_access_token(refresh)
    new_access = data["access_token"]
    new_refresh = data.get("refresh_token") or refresh  # Asana 会轮换 refresh_token
    new_expires = iso(utcnow() + timedelta(seconds=int(data.get("expires_in") or 3600)))
    get_store(request).update_session(
        session["id"],
        token_enc=cipher.encrypt(new_access),
        refresh_enc=cipher.encrypt(new_refresh),
        token_expires_at=new_expires,
    )
    return new_access


# ---------- 导出任务 ----------


class ExportRequest(BaseModel):
    projects: list[dict[str, str]] = Field(min_length=1, max_length=MAX_PROJECTS_PER_EXPORT)
    options: dict[str, bool] | None = None


@app.post("/api/export")
def api_export(request: Request, body: ExportRequest):
    session = require_session(request)
    if not session.get("token_enc"):
        raise HTTPException(401, "授权已用于导出任务并被撤销，请重新连接 Asana")

    projects = [
        {"gid": p["gid"], "name": p.get("name") or p["gid"], "status": "pending", "total": 0, "processed": 0}
        for p in body.projects
        if p.get("gid")
    ]
    if not projects:
        raise HTTPException(422, "至少选择一个项目")

    store = get_store(request)
    cipher: TokenCipher = request.app.state.cipher
    job = store.create_job(
        session_id=session["id"],
        projects=projects,
        options=normalize_options(body.options),
        token_enc=session["token_enc"],
        refresh_enc=session.get("refresh_enc"),
        token_expires_at=session.get("token_expires_at"),
    )
    # token 只在本次导出任务期间持有：会话立即清空，任务终态即撤销
    store.update_session(session["id"], token_enc=None, refresh_enc=None, token_expires_at=None)

    threading.Thread(
        target=run_export, args=(store, cipher, job["id"]), name=f"export-{job['id']}", daemon=True
    ).start()
    return {"job": job_public_view(job)}


@app.get("/api/export/{job_id}")
def api_export_status(request: Request, job_id: str):
    session = require_session(request)
    job = get_store(request).get_job(job_id)
    if not job or job["session_id"] != session["id"]:
        raise HTTPException(404, "任务不存在")
    view = job_public_view(job)
    if job["status"] == "completed":
        expired = parse_iso(job.get("expires_at"))
        view["expired"] = bool(expired and expired < utcnow())
    return view


@app.get("/api/export/{job_id}/download", include_in_schema=False)
def api_export_download(request: Request, job_id: str, token: str):
    """临时下载链接：capability token 校验（3 小时后随文件一并销毁）。"""
    job = get_store(request).get_job(job_id)
    if not job or job["status"] != "completed":
        raise HTTPException(404, "文件不存在或任务未完成")
    expired = parse_iso(job.get("expires_at"))
    if expired and expired < utcnow():
        raise HTTPException(410, "下载链接已过期（ZIP 已自动销毁）")
    if not token or token != job.get("download_token"):
        raise HTTPException(403, "下载令牌无效")
    zip_path = Path(job["zip_path"])
    if not zip_path.is_file():
        raise HTTPException(410, "文件已清理")
    return FileResponse(zip_path, media_type="application/zip", filename="Obsidian-Vault.zip")
