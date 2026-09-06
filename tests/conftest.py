"""测试公共夹具：在导入 app 之前固定环境变量（config 在 import 时读 env）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests")
os.environ.setdefault("ASANA_DEV_TOKEN", "2/1234567890/1234567890:deadbeef")
os.environ.setdefault("ASANA_CLIENT_ID", "")
os.environ.setdefault("ASANA_CLIENT_SECRET", "")
os.environ.setdefault("BASE_URL", "http://localhost:8600")


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """把数据目录指到临时目录（settings 单例属性可变）。"""
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    (tmp_path / "work").mkdir(exist_ok=True)
    (tmp_path / "zips").mkdir(exist_ok=True)
    return tmp_path
