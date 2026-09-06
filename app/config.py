"""环境变量配置。所有运行时行为都从这里读，.env 由部署方填写。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# 与 Asana Developer Console 中勾选的 scope 保持一致，否则回调会报 forbidden_scopes。
# 全部为只读 scope，这是「只申请读取所需权限」承诺的落地。
DEFAULT_SCOPES = "tasks:read projects:read attachments:read users:read workspaces:read custom_fields:read"

ZIP_TTL_HOURS_DEFAULT = 3


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "") or default)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "") or default)
    except ValueError:
        return default


@dataclass
class Settings:
    # OAuth App（未配置时若设置了 dev token 则进入开发模式）
    asana_client_id: str = field(default_factory=lambda: os.environ.get("ASANA_CLIENT_ID", ""))
    asana_client_secret: str = field(default_factory=lambda: os.environ.get("ASANA_CLIENT_SECRET", ""))
    asana_redirect_uri: str = field(default_factory=lambda: os.environ.get("ASANA_REDIRECT_URI", ""))
    asana_scopes: str = field(default_factory=lambda: os.environ.get("ASANA_SCOPES", DEFAULT_SCOPES))

    # 开发模式固定 Token（来自 ~/ar/bridge 的 PAT），仅在 OAuth 未配置时生效
    asana_dev_token: str = field(default_factory=lambda: os.environ.get("ASANA_DEV_TOKEN", ""))

    # API 行为（沿用 bridge 验证过的参数：Free 档 ~150 req/min，按 2 req/s 限流）
    asana_base_url: str = field(
        default_factory=lambda: os.environ.get("ASANA_BASE_URL", "https://app.asana.com/api/1.0")
    )
    asana_timeout: float = field(default_factory=lambda: _env_float("ASANA_TIMEOUT", 30.0))
    asana_retry_times: int = field(default_factory=lambda: _env_int("ASANA_RETRY_TIMES", 3))
    asana_rate_limit: float = field(default_factory=lambda: _env_float("ASANA_RATE_LIMIT", 2.0))

    # 会话/token 加密密钥（Fernet key）
    secret_key: str = field(default_factory=lambda: os.environ.get("SECRET_KEY", ""))

    base_url: str = field(default_factory=lambda: os.environ.get("BASE_URL", "http://localhost:8600"))

    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data"))))

    # ZIP 保存时限（小时），到期后文件与记录一并清理
    zip_ttl_hours: float = field(default_factory=lambda: _env_float("ZIP_TTL_HOURS", ZIP_TTL_HOURS_DEFAULT))

    # 终态任务/会话记录的最长保留（小时）
    job_record_ttl_hours: float = field(default_factory=lambda: _env_float("JOB_RECORD_TTL_HOURS", 24.0))

    # 导出安全上限，防止异常项目把任务拖垮
    max_tasks_per_export: int = field(default_factory=lambda: _env_int("MAX_TASKS_PER_EXPORT", 5000))
    max_subtasks_per_task: int = field(default_factory=lambda: _env_int("MAX_SUBTASKS_PER_TASK", 50))
    max_subtask_depth: int = field(default_factory=lambda: _env_int("MAX_SUBTASK_DEPTH", 3))
    max_attachment_mb: int = field(default_factory=lambda: _env_int("MAX_ATTACHMENT_MB", 200))

    # 清理线程间隔（秒）
    cleanup_interval_seconds: float = field(default_factory=lambda: _env_float("CLEANUP_INTERVAL_SECONDS", 60.0))

    @property
    def oauth_configured(self) -> bool:
        return bool(self.asana_client_id and self.asana_client_secret and self.asana_redirect_uri)

    @property
    def dev_mode(self) -> bool:
        """OAuth 未配置但提供了开发 Token：跳过 OAuth，直接用 PAT 连接。"""
        return not self.oauth_configured and bool(self.asana_dev_token)

    @property
    def connectable(self) -> bool:
        return self.oauth_configured or self.dev_mode

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "zips").mkdir(exist_ok=True)
        (self.data_dir / "work").mkdir(exist_ok=True)


settings = Settings()
