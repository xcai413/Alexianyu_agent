"""全局配置(从 .env / 环境变量加载,Pydantic Settings)。"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet
from dotenv import set_key
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    """应用配置,自动从 .env 与环境变量加载。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="XIANYU_",
        case_sensitive=False,
        extra="ignore",
    )

    # === 运行时 ===
    env: Literal["development", "production"] = "development"
    data_dir: Path = Field(default=Path("./data"))
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    tz: str = "Asia/Shanghai"

    # === 数据库 ===
    # database_url 非空时优先于 db_path;用于 MySQL/PostgreSQL 以及显式 SQLite URL。
    # 支持 SQLAlchemy async URL,例如:
    #   sqlite+aiosqlite:///./data/xianyu.db
    #   mysql+asyncmy://user:pass@127.0.0.1:3306/xianyu
    #   postgresql+asyncpg://user:pass@127.0.0.1:5432/xianyu
    database_url: str = ""
    db_path: Path | None = None  # database_url 留空时使用 data_dir/xianyu.db

    # === 安全 ===
    fernet_key: str = ""  # 首次启动自动生成

    # === HTTP API(可选)===
    http_host: str = "127.0.0.1"
    http_port: int = 8089

    # === MCP Server ===
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8090

    # === 闲鱼协议 ===
    ws_url: str = ""
    mtop_url: str = ""

    # === AI Provider(可选)===
    ai_base_url: str = ""
    ai_api_key: str = ""
    ai_model: str = ""
    reply_mode: Literal["rule", "rule_then_ai", "ai"] = "rule"
    automation_mode: Literal["observe", "active"] = "observe"

    # === guardrails ===
    guardrail_max_msg_per_hour: int = 60
    guardrail_max_order_amount: float = 9999.0
    guardrail_quiet_hours: str = ""  # 留空=不启用夜间静默
    guardrail_fail_threshold: int = 5

    @field_validator("data_dir", mode="after")
    @classmethod
    def _ensure_data_dir(cls, v: Path) -> Path:
        v.mkdir(parents=True, exist_ok=True)
        return v

    @property
    def resolved_db_path(self) -> Path:
        return self.db_path or (self.data_dir / "xianyu.db")

    @staticmethod
    def _normalize_async_database_url(value: str) -> str:
        """Normalize bare SQLAlchemy URLs to the async drivers supported by this project."""
        url = make_url(value)
        if url.drivername == "sqlite":
            url = url.set(drivername="sqlite+aiosqlite")
        elif url.drivername == "mysql":
            url = url.set(drivername="mysql+asyncmy")
        elif url.drivername == "postgresql":
            url = url.set(drivername="postgresql+asyncpg")
        return url.render_as_string(hide_password=False)

    @property
    def db_url(self) -> str:
        if self.database_url.strip():
            return self._normalize_async_database_url(self.database_url.strip())
        return f"sqlite+aiosqlite:///{self.resolved_db_path.as_posix()}"

    @property
    def sync_db_url(self) -> str:
        """Return a synchronous URL for compatibility with legacy diagnostics/tools."""
        url = make_url(self.db_url)
        if url.drivername == "sqlite+aiosqlite":
            url = url.set(drivername="sqlite")
        elif url.drivername == "mysql+asyncmy":
            url = url.set(drivername="mysql+pymysql")
        elif url.drivername == "postgresql+asyncpg":
            url = url.set(drivername="postgresql+psycopg")
        return url.render_as_string(hide_password=False)

    @property
    def database_backend(self) -> str:
        """Return SQLAlchemy backend name: sqlite, mysql, or postgresql."""
        return make_url(self.db_url).get_backend_name()

    @property
    def runtime_dir(self) -> Path:
        return self.data_dir / "runtime"

    @property
    def daemon_lock_path(self) -> Path:
        return self.runtime_dir / "daemon.lock"

    @property
    def service_pause_path(self) -> Path:
        return self.runtime_dir / "service.paused"

    def account_lock_path(self, account_id: str) -> Path:
        """Return the per-account WS lock path after removing unsafe path characters."""
        safe = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in account_id
        )[:32]
        digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:12]
        return self.runtime_dir / "accounts" / f"{safe}-{digest}.lock"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def daemon_log_path(self) -> Path:
        return self.log_dir / "xianyu-agent.log"

    @property
    def fernet(self) -> Fernet:
        if not self.fernet_key:
            msg = "FERNET_KEY 未配置"
            raise ValueError(msg)
        return Fernet(self.fernet_key.encode())

    @staticmethod
    def generate_fernet_key() -> str:
        return Fernet.generate_key().decode()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局单例配置。"""
    return Settings()


def reset_settings_cache() -> None:
    """测试用:清掉 lru_cache。"""
    get_settings.cache_clear()


def ensure_fernet_key() -> str:
    """确保 FERNET_KEY 已配置:缺失则生成并持久化到 .env。

    兑现 README / .env.example 中"首次启动自动生成"的承诺。
    返回当前生效的 key。
    """
    s = get_settings()
    if s.fernet_key:
        return s.fernet_key
    key = Settings.generate_fernet_key()
    env_path = Path(".env")
    if env_path.exists():
        set_key(str(env_path), "XIANYU_FERNET_KEY", key)
    else:
        env_path.write_text(f"XIANYU_FERNET_KEY={key}\n", encoding="utf-8")
    reset_settings_cache()
    return get_settings().fernet_key
