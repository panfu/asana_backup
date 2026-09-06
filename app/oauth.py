"""Asana OAuth 2.0（Authorization Code Grant）—— 移植 bridge 的 AsanaOAuthService。

用户视角只有一步：点「连接 Asana」→ Asana 官方页点「允许」→ 自动跳回。
token 的换取、加密存储、过期刷新、任务结束后的撤销都在这里完成。
"""

from __future__ import annotations

import logging
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import settings

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://app.asana.com/-/oauth_authorize"
TOKEN_URL = "https://app.asana.com/-/oauth_token"
REVOKE_URL = "https://app.asana.com/-/oauth_revoke"


def new_state() -> str:
    return secrets.token_urlsafe(32)


def authorization_url(state: str) -> str:
    params = {
        "client_id": settings.asana_client_id,
        "redirect_uri": settings.asana_redirect_uri,
        "response_type": "code",
        "scope": settings.asana_scopes,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code_for_tokens(code: str) -> dict[str, Any]:
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": settings.asana_client_id,
            "client_secret": settings.asana_client_secret,
            "redirect_uri": settings.asana_redirect_uri,
            "code": code,
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        log.error("Asana token exchange failed: %s", resp.text[:500])
        try:
            err = resp.json()
            message = err.get("error_description") or err.get("error") or resp.text[:200]
        except Exception:
            message = resp.text[:200]
        raise RuntimeError(f"Asana 授权失败：{message}")
    return resp.json()


def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": settings.asana_client_id,
            "client_secret": settings.asana_client_secret,
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError("Asana 授权刷新失败，请重新连接 Asana")
    return resp.json()


def revoke_token(access_token: str | None, refresh_token: str | None) -> bool:
    """任务结束后撤销授权（尽力而为：失败不影响导出结果，token 记录仍会被删除）。"""
    if not settings.oauth_configured:
        return False
    candidates: list[dict[str, str]] = []
    if refresh_token:
        candidates.append({"refresh_token": refresh_token})
    if access_token:
        candidates.append({"token": access_token})
    for payload in candidates:
        try:
            resp = httpx.post(
                REVOKE_URL,
                data={
                    "client_id": settings.asana_client_id,
                    "client_secret": settings.asana_client_secret,
                    **payload,
                },
                timeout=30,
            )
            if resp.status_code < 400:
                log.info("Asana token revoked")
                return True
            log.warning("Asana revoke returned %s: %s", resp.status_code, resp.text[:200])
        except httpx.HTTPError as e:
            log.warning("Asana revoke request failed: %s", e)
    return False
