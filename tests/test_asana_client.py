"""asana_client.py 的单元测试：翻页 / 重试 / 限流 / 评论过滤 / 附件下载规则。"""

from __future__ import annotations

import httpx
import pytest

from app.asana_client import AsanaApiError, AsanaClient


def make_client(handler, rate_limit=1000.0, retry=3) -> AsanaClient:
    client = AsanaClient("token")
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    client._rate_limit = rate_limit
    client._retry_times = retry
    return client


class TestPagination:
    def test_get_all_project_tasks_follows_offset(self):
        pages = [
            {"data": [{"gid": "1"}, {"gid": "2"}], "next_page": {"offset": "abc"}},
            {"data": [{"gid": "3"}], "next_page": None},
        ]
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, json=pages[len(calls) - 1])

        client = make_client(handler)
        tasks = client.get_all_project_tasks("p1")
        assert [t["gid"] for t in tasks] == ["1", "2", "3"]
        assert "offset=abc" in calls[1]
        assert "project=p1" in calls[0]


class TestRetry:
    def test_retries_429_then_succeeds(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda *_: None)
        seen = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["n"] += 1
            if seen["n"] < 3:
                return httpx.Response(429, json={"errors": [{"message": "rate limited"}]})
            return httpx.Response(200, json={"data": {"gid": "me"}})

        client = make_client(handler, retry=3)
        assert client.get_me()["gid"] == "me"
        assert seen["n"] == 3

    def test_does_not_retry_400(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda *_: None)
        seen = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["n"] += 1
            return httpx.Response(400, json={"errors": [{"message": "bad request"}]})

        client = make_client(handler, retry=3)
        with pytest.raises(AsanaApiError) as exc:
            client.get_me()
        assert "bad request" in str(exc.value)
        assert seen["n"] == 1  # 未重试

    def test_error_message_extraction(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"errors": [{"message": "forbidden_scopes"}]})

        client = make_client(handler)
        with pytest.raises(AsanaApiError, match="forbidden_scopes"):
            client.get_workspaces()


class TestComments:
    def test_filters_system_stories(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "/stories" in str(request.url)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"gid": "1", "type": "system", "text": "assigned to Fred"},
                        {"gid": "2", "type": "comment", "text": "真实评论"},
                    ]
                },
            )

        client = make_client(handler)
        comments = client.get_task_comments("t1")
        assert len(comments) == 1 and comments[0]["gid"] == "2"


class TestDownloadFile:
    def test_returns_bytes_when_ok(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 100)

        client = make_client(handler)
        assert client.download_file("https://files.example/a", 1024) == b"x" * 100

    def test_drops_tiny_error_page(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"expired")  # <50 字节视为错误页

        client = make_client(handler)
        assert client.download_file("https://files.example/a", 1024) is None

    def test_enforces_size_cap(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 1000)

        client = make_client(handler)
        assert client.download_file("https://files.example/a", 200) is None


class TestRateLimiter:
    def test_sliding_window_waits(self, monkeypatch):
        client = AsanaClient("t")
        client._rate_limit = 2
        sleeps: list[float] = []
        monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

        client._rate_limit_wait()
        client._rate_limit_wait()
        client._rate_limit_wait()  # 第三次应触发等待

        assert len(sleeps) == 1 and 0 < sleeps[0] <= 1
