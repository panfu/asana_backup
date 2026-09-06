"""exporter.py 的单元测试：单任务导出降级 / ZIP 打包 / 全流程生命周期。"""

from __future__ import annotations

import json
import zipfile

import pytest

from app import exporter
from app.crypto import TokenCipher
from app.exporter import (
    TokenProvider,
    build_zip,
    export_task,
    normalize_options,
    run_export,
)
from app.store import Store


class FakeClient:
    """内存版 Asana 客户端：行为对齐真实 API 的关键点。"""

    def __init__(self, *, fail_detail_for=None):
        self.fail_detail_for = set(fail_detail_for or [])
        self.tasks = {
            "t1": {
                "gid": "t1",
                "name": "Task One",
                "completed": False,
                "notes": "note with https://app.asana.com/app/asana/-/get_asset?asset_id=a1",
                "permalink_url": "https://app.asana.com/0/p1/t1",
                "created_by": {"name": "A", "email": "a@x.com"},
                "assignee": {"name": "B"},
                "due_on": "2026-09-10",
                "created_at": "2026-09-01",
                "modified_at": "2026-09-02",
                "completed_at": None,
                "projects": [{"gid": "p1", "name": "Proj"}],
                "memberships": [{"section": {"name": "Sec"}}],
                "custom_fields": [{"name": "Prio", "display_value": "High"}],
            },
            "t2": {
                "gid": "t2",
                "name": "Task Two",
                "completed": True,
                "notes": "",
                "permalink_url": "u",
                "projects": [{"gid": "p1", "name": "Proj"}],
                "memberships": [{"section": {"name": "Sec"}}],
            },
        }

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def get_workspaces(self):
        return [{"gid": "w1", "name": "artextile.com.hk", "is_organization": True}]

    def get_projects(self, workspace_gid):
        return [
            {
                "gid": "p1",
                "name": "IT",
                "notes": "项目备注",
                "due_on": None,
                "created_at": "2026-01-01",
                "modified_at": "2026-09-01",
            }
        ]

    def get_all_project_tasks(self, project_gid):
        return [
            {"gid": "t1", "name": "Task One", "completed": False},
            {"gid": "t2", "name": "Task Two", "completed": True},
        ]

    def get_task_for_backup(self, gid):
        if gid in self.fail_detail_for:
            raise RuntimeError("boom")
        return dict(self.tasks[gid])

    def get_task_subtasks(self, task_gid):
        return [
            {
                "gid": "s1",
                "name": "Sub",
                "completed": True,
                "notes": "n",
                "assignee": {"name": "C"},
                "due_on": "2026-09-11",
                "permalink_url": "pu",
            },
        ]

    def get_task_attachments(self, task_gid):
        return [
            {"gid": "a1", "name": "doc.pdf", "resource_subtype": "upload", "host": None},
            {"gid": "a2", "name": "https://figma.com/x", "resource_subtype": "external", "host": "figma"},
        ]

    def get_fresh_attachment_download_url(self, attachment_gid):
        return f"https://files/{attachment_gid}" if attachment_gid == "a1" else None

    def get_task_comments(self, task_gid):
        return [{"gid": "c1", "type": "comment", "text": "hi", "created_by": {"name": "D"}, "created_at": "2026-09-03"}]

    def download_file(self, url, max_bytes):
        return b"%" * 120 if url.endswith("a1") else None


OPTS_ALL = {
    "include_completed": True,
    "include_comments": True,
    "include_subtasks": True,
    "include_attachments": True,
}


class TestNormalizeOptions:
    def test_defaults(self):
        assert normalize_options(None) == OPTS_ALL
        assert normalize_options({"include_comments": False})["include_comments"] is False
        assert normalize_options({"include_comments": False})["include_subtasks"] is True


class TestExportTask:
    def test_full_export_writes_files(self, data_dir):
        base = data_dir / "work"
        export_task(FakeClient(), "t1", "Task One", base, OPTS_ALL)

        md_path = base / "Proj" / "Sec" / "t1 Task One.md"
        assert md_path.is_file()
        md = md_path.read_text()
        assert "![](../attachments/t1_doc.pdf)" in md
        assert "[📎 Figma](https://figma.com/x)" in md
        assert "## 🌿 Subtasks" in md
        assert "### D · 2026-09-03" in md
        att_path = base / "Proj" / "attachments" / "t1_doc.pdf"
        assert att_path.is_file() and att_path.stat().st_size == 120

    def test_detail_failure_writes_stub(self, data_dir):
        base = data_dir / "work"
        export_task(FakeClient(fail_detail_for={"t2"}), "t2", "Fallback", base, OPTS_ALL)
        # bridge 行为：详情失败时项目/分区未知 → Unknown/Uncategorized，但 md 必落盘
        stub = base / "Unknown" / "Uncategorized" / "t2 Fallback.md"
        assert stub.is_file()
        assert "Fallback" in stub.read_text()

    def test_options_off(self, data_dir):
        base = data_dir / "work"
        opts = {k: False for k in OPTS_ALL}
        export_task(FakeClient(), "t1", "Task One", base, opts)
        md = (base / "Proj" / "Sec" / "t1 Task One.md").read_text()
        assert "Attachments" not in md and "Comments" not in md and "Subtasks" not in md
        assert not (base / "Proj" / "attachments").exists()


class TestBuildZip:
    def test_zip_structure_and_stats(self, data_dir):
        work = data_dir / "work" / "j1"
        (work / "Proj" / "Sec").mkdir(parents=True)
        (work / "Proj" / "attachments").mkdir(parents=True)
        (work / "Proj" / "Sec" / "t1 x.md").write_text("# x")
        (work / "Proj" / "attachments" / "t1 f.bin").write_bytes(b"123" * 20)

        zip_path = data_dir / "zips" / "j1.zip"
        stats = build_zip(work, zip_path)
        assert stats == {"files": 2, "markdown": 1, "attachments": 1, "size_bytes": zip_path.stat().st_size}

        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        assert "Asana/Proj/Sec/t1 x.md" in names
        assert "Asana/Proj/attachments/t1 f.bin" in names
        assert all(n.startswith("Asana/") for n in names)

    def test_empty_workdir_raises(self, data_dir):
        work = data_dir / "work" / "j2"
        work.mkdir(parents=True)
        with pytest.raises(RuntimeError, match="No markdown files"):
            build_zip(work, data_dir / "zips" / "j2.zip")


class TestRunExport:
    def _make_store(self, tmp_path, cipher):
        store = Store(tmp_path / "db.sqlite3")
        session = store.create_session()
        store.update_session(
            session["id"],
            token_enc=cipher.encrypt("tok"),
            refresh_enc=cipher.encrypt("ref"),
            token_expires_at=None,
        )
        job = store.create_job(
            session_id=session["id"],
            projects=[{"gid": "p1", "name": "Proj", "status": "pending", "total": 0, "processed": 0}],
            options=OPTS_ALL,
            token_enc=cipher.encrypt("tok"),
            refresh_enc=cipher.encrypt("ref"),
            token_expires_at=None,
        )
        return store, session, job

    def test_full_lifecycle(self, tmp_path, data_dir, monkeypatch):
        cipher = TokenCipher("test-secret")
        store, session, job = self._make_store(tmp_path, cipher)
        revoked = []
        monkeypatch.setattr(exporter, "AsanaClient", lambda token: FakeClient())
        monkeypatch.setattr(exporter.oauth, "revoke_token", lambda a, r: revoked.append((a, r)) or True)

        run_export(store, cipher, job["id"])

        fresh = store.get_job(job["id"])
        assert fresh["status"] == "completed"
        assert fresh["token_enc"] is None and fresh["refresh_enc"] is None  # token 已抹除
        assert revoked == [("tok", "ref")]  # 已撤销
        stats = json.loads(fresh["stats_json"])
        assert stats["markdown"] == 2
        assert fresh["expires_at"] is not None
        assert (data_dir / "work" / job["id"]).exists() is False  # 工作目录已清理
        assert store.get_job(job["id"])["download_token"]

    def test_failure_marks_failed_and_cleans(self, tmp_path, data_dir, monkeypatch):
        class ExplodingClient(FakeClient):
            def get_all_project_tasks(self, project_gid):
                raise RuntimeError("asana down")

        cipher = TokenCipher("test-secret")
        store, session, job = self._make_store(tmp_path, cipher)
        monkeypatch.setattr(exporter, "AsanaClient", lambda token: ExplodingClient())
        monkeypatch.setattr(exporter.oauth, "revoke_token", lambda a, r: True)

        run_export(store, cipher, job["id"])
        fresh = store.get_job(job["id"])
        assert fresh["status"] == "failed"
        assert "asana down" in fresh["error"]
        assert fresh["token_enc"] is None  # 失败同样撤销并抹除
        assert not (data_dir / "zips" / f"{job['id']}.zip").exists()


class TestTokenProvider:
    def test_returns_token_when_no_expiry(self, tmp_path):
        cipher = TokenCipher("s")
        store = Store(tmp_path / "db.sqlite3")
        session = store.create_session()
        job = store.create_job(
            session_id=session["id"],
            projects=[],
            options={},
            token_enc=cipher.encrypt("tok"),
            refresh_enc=None,
            token_expires_at=None,
        )
        provider = TokenProvider(store, cipher, job)
        assert provider.access_token() == "tok"

    def test_expires_immediately_without_refresh(self, tmp_path):
        from app import store as s

        cipher = TokenCipher("s")
        store = Store(tmp_path / "db.sqlite3")
        session = store.create_session()
        job = store.create_job(
            session_id=session["id"],
            projects=[],
            options={},
            token_enc=cipher.encrypt("tok"),
            refresh_enc=None,
            token_expires_at=s.iso(s.utcnow()),  # 已过期
        )
        provider = TokenProvider(store, cipher, job)
        with pytest.raises(RuntimeError, match="重新连接"):
            provider.access_token()
