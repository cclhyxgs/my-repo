# -*- coding: utf-8 -*-
"""DB 引擎 / 会话工厂（能力 5）。

DSN 唯一来源 = `server.settings.resolve_database_url()`（env `MBULL_DATABASE_URL`
优先，否则开发态 SQLite 文件兜底）。切换 DSN（测试用临时 SQLite）时自动重建引擎。
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from server import settings
from server.db.models import Base

_engine = None
_session_factory = None
_url: str | None = None
_schema_ready = False


def _connect_kwargs(url: str) -> dict:
    # SQLite 在 TestClient 多线程下会因线程归属报错，需放开 check_same_thread
    if url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return {}


def get_engine():
    """返回当前 DSN 对应的 Engine；DSN 变化时自动重建。"""
    global _engine, _session_factory, _url, _schema_ready
    url = settings.resolve_database_url()
    if _engine is None or _url != url:
        if _engine is not None:
            _engine.dispose()
        _engine = create_engine(url, future=True, **_connect_kwargs(url))
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
        _url = url
        _schema_ready = False
    return _engine


def get_session_factory() -> sessionmaker:
    get_engine()
    return _session_factory


def session_scope() -> Session:
    """新建会话（调用方负责 commit/rollback；建议用 with/finally 关闭）。

    首次取会话时自动建表（骨架期用 create_all；正式迁移后续换 Alembic），
    保证任何端点都不会因"表不存在"而失败。
    """
    global _schema_ready
    get_engine()
    if not _schema_ready:
        init_schema()
        _schema_ready = True
    return get_session_factory()()


def init_schema() -> None:
    """建表（幂等）。"""
    Base.metadata.create_all(get_engine())


def dispose() -> None:
    """释放引擎并清空缓存（测试切换 DSN / 进程退出用）。"""
    global _engine, _session_factory, _url, _schema_ready
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
    _url = None
    _schema_ready = False
