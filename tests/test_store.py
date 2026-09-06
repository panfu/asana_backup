"""store.py 的单元测试：会话与任务生命周期。"""

from __future__ import annotations

from datetime import timedelta

from app import store as s
from app.store import Store


def make_store(tmp_path):
    return Store(tmp_path / "test.db")


class TestSessions:
    def test_create_get_update_delete(self, tmp_path):
        store = make_store(tmp_path)
        session = store.create_session()
        sid = session["id"]
        assert store.get_session(sid)["id"] == sid
        assert store.get_session("nope") is None
        assert store.get_session(None) is None

        store.update_session(sid, asana_name="Fred", token_enc="enc")
        assert store.get_session(sid)["asana_name"] == "Fred"

        store.delete_session(sid)
        assert store.get_session(sid) is None

    def test_purge_stale_sessions(self, tmp_path):
        store = make_store(tmp_path)
        session = store.create_session()
        store.update_session(session["id"], created_at=s.iso(s.utcnow() - timedelta(hours=48)))
        assert store.purge_stale_sessions(s.utcnow() - timedelta(hours=24)) == 1
        assert store.get_session(session["id"]) is None


class TestJobs:
    def _job(self, store, **kw):
        defaults = dict(
            session_id="sess",
            projects=[{"gid": "p1", "name": "IT", "status": "pending", "total": 0, "processed": 0}],
            options={"include_comments": True},
            token_enc="enc",
            refresh_enc=None,
            token_expires_at=None,
        )
        defaults.update(kw)
        return store.create_job(**defaults)

    def test_create_and_get(self, tmp_path):
        store = make_store(tmp_path)
        job = self._job(store)
        assert job["status"] == s.JOB_PENDING
        assert store.get_job(job["id"])["id"] == job["id"]
        assert store.get_session_active_job("sess")["id"] == job["id"]

    def test_new_job_replaces_pending(self, tmp_path):
        store = make_store(tmp_path)
        j1 = self._job(store)
        j2 = self._job(store)
        assert store.get_job(j1["id"]) is None
        assert store.get_job(j2["id"])["status"] == s.JOB_PENDING

    def test_update_job_project_recalc_totals(self, tmp_path):
        store = make_store(tmp_path)
        job = store.create_job(
            session_id="sess",
            projects=[
                {"gid": "p1", "name": "A", "status": "pending", "total": 0, "processed": 0},
                {"gid": "p2", "name": "B", "status": "pending", "total": 0, "processed": 0},
            ],
            options={},
            token_enc="e",
            refresh_enc=None,
            token_expires_at=None,
        )
        store.update_job_project(job["id"], "p1", {"total": 10, "processed": 3})
        fresh = store.get_job(job["id"])
        assert fresh["total_tasks"] == 10 and fresh["processed_tasks"] == 3
        projects = __import__("json").loads(fresh["projects_json"])
        assert projects[0]["processed"] == 3 and projects[1]["total"] == 0

    def test_wipe_tokens(self, tmp_path):
        store = make_store(tmp_path)
        job = self._job(store)
        assert store.get_job(job["id"])["token_enc"] == "enc"
        store.wipe_job_tokens(job["id"])
        fresh = store.get_job(job["id"])
        assert fresh["token_enc"] is None and fresh["refresh_enc"] is None
        assert list(store.jobs_with_live_tokens()) == []

    def test_expired_completed_jobs(self, tmp_path):
        store = make_store(tmp_path)
        job = self._job(store)
        past = (s.utcnow() - timedelta(hours=1)).isoformat()
        store.update_job(job["id"], status=s.JOB_COMPLETED, expires_at=past)
        rows = store.expired_completed_jobs(s.utcnow())
        assert [r["id"] for r in rows] == [job["id"]]

    def test_stale_jobs(self, tmp_path):
        store = make_store(tmp_path)
        job = self._job(store)
        store.update_job(job["id"], created_at=s.iso(s.utcnow() - timedelta(hours=48)))
        assert [r["id"] for r in store.stale_jobs(s.utcnow() - timedelta(hours=24))] == [job["id"]]
