"""API 集成测试：开发模式连接 → 选项目 → 导出 → 下载 全流程（FakeClient）。"""

from __future__ import annotations

import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from test_exporter import FakeClient

from app import exporter, main
from app.config import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(main, "AsanaClient", lambda token: FakeClient())
    monkeypatch.setattr(exporter, "AsanaClient", lambda token: FakeClient())
    with TestClient(main.app) as c:
        yield c


def connect(c: TestClient) -> None:
    resp = c.get("/api/connect", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/?connected=1"
    assert "ab_sid" in resp.cookies


class TestBasics:
    def test_index_serves_wizard(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Obsidian" in resp.text
        assert "3 小时" in resp.text  # 隐私承诺文案

    def test_healthz(self, client):
        assert client.get("/healthz").json() == {"ok": "true"}

    def test_session_before_connect(self, client):
        data = client.get("/api/session").json()
        assert data == {
            "configured": True,
            "dev_mode": True,
            "connected": False,
            "identity": None,
            "job": None,
        }


class TestSelection:
    def test_requires_session(self, client):
        assert client.get("/api/workspaces").status_code == 401

    def test_workspaces_and_projects(self, client):
        connect(client)
        ws = client.get("/api/workspaces").json()["workspaces"]
        assert ws[0]["gid"] == "w1"
        projects = client.get("/api/projects", params={"workspace": "w1"}).json()["projects"]
        assert projects[0]["name"] == "IT"


class TestExportFlow:
    def test_full_flow(self, client):
        connect(client)

        resp = client.post(
            "/api/export",
            json={
                "projects": [{"gid": "p1", "name": "Proj"}],
                "options": {
                    "include_completed": True,
                    "include_comments": True,
                    "include_subtasks": True,
                    "include_attachments": True,
                },
            },
        )
        assert resp.status_code == 200
        job = resp.json()["job"]
        assert job["status"] in ("pending", "processing")

        # token 应已从会话转移：会话显示未连接（隐私承诺：仅任务期间持有）
        session = client.get("/api/session").json()
        assert session["connected"] is False

        # 轮询至完成
        for _ in range(50):
            job = client.get(f"/api/export/{job['id']}").json()
            if job["status"] in ("completed", "failed"):
                break
            time.sleep(0.1)
        assert job["status"] == "completed", job
        assert job["progress"] == 100
        assert job["stats"]["markdown"] == 2
        assert job["download_url"]

        # 下载（capability token 校验）
        resp = client.get(job["download_url"])
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/zip")
        assert "Obsidian-Vault.zip" in resp.headers["content-disposition"]

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            names = zf.namelist()
        assert "Asana/Proj/Sec/t1 Task One.md" in names
        assert "Asana/Proj/attachments/t1_doc.pdf" in names

        md = zipfile.ZipFile(io.BytesIO(resp.content)).read("Asana/Proj/Sec/t1 Task One.md").decode()
        assert "## 🌿 Subtasks" in md
        assert "### D · 2026-09-03" in md

    def test_download_rejects_wrong_token(self, client):
        connect(client)
        job = client.post("/api/export", json={"projects": [{"gid": "p1", "name": "Proj"}]}).json()["job"]
        for _ in range(50):
            job = client.get(f"/api/export/{job['id']}").json()
            if job["status"] in ("completed", "failed"):
                break
            time.sleep(0.1)
        assert job["status"] == "completed"
        assert client.get(f"/api/export/{job['id']}/download?token=wrong").status_code == 403

    def test_export_requires_connection(self, client):
        connect(client)
        first = client.post("/api/export", json={"projects": [{"gid": "p1", "name": "P"}]}).json()["job"]
        # 等第一个任务终态，避免后台线程与 teardown 竞争
        for _ in range(50):
            first = client.get(f"/api/export/{first['id']}").json()
            if first["status"] in ("completed", "failed"):
                break
            time.sleep(0.1)
        resp = client.post("/api/export", json={"projects": [{"gid": "p1", "name": "P"}]})
        assert resp.status_code == 401
        assert "重新连接" in resp.json()["detail"]

    def test_export_validates_projects(self, client):
        connect(client)
        resp = client.post("/api/export", json={"projects": []})
        assert resp.status_code == 422


class TestDisconnect:
    def test_disconnect_clears_session(self, client, monkeypatch):
        connect(client)
        monkeypatch.setattr(main.oauth, "revoke_token", lambda a, r: True)
        assert client.post("/api/disconnect").status_code == 200
        assert client.get("/api/session").json()["connected"] is False
