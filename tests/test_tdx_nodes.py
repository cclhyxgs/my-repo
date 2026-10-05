# -*- coding: utf-8 -*-
"""通达信节点池（engine/data_sources/tdx_nodes.py）单元测试。

覆盖 A+B+C 三项自愈能力的关键分支：
- 持久化优先 / 缺失或损坏时回落种子（永不返回空池）
- 重扫成功后落盘并切换池；重扫为空时**保留现有池**（不因一次失败清空）
- 过期池触发后台重扫、新鲜池不触发；失败上报走节流
- 探测判据：TCP 可连 ≠ 支持 K 线命令

约束：全程不触网（探测/重扫全部 monkeypatch）、不写用户数据目录（CONFIG_DIR → tmp_path）。
"""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault(
    "QUANT_SYSTEM_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".pytest_tdx_nodes_tmp"),
)

import pytest

from engine.data_sources import tdx_nodes

_POOL_FILE = "tdx_servers.json"
# 供节流测试还原真实实现（autouse fixture 会把它替换为 no-op）
_REAL_MAYBE_REFRESH_ASYNC = tdx_nodes.maybe_refresh_async


class _NoopThread:
    """替换 threading.Thread，避免节流测试真的拉起后台探测线程。"""

    def __init__(self, *a, **k):
        pass

    def start(self):
        pass


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """隔离 CONFIG_DIR + 清空内存态 + 默认屏蔽后台重扫（get_servers 在 stale 时会拉起线程）。"""
    import engine.config as cfg

    monkeypatch.setattr(cfg, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(tdx_nodes, "maybe_refresh_async", lambda *a, **k: False)
    tdx_nodes._reset_state()
    yield
    tdx_nodes._reset_state()


def _write_pool(tmp_path, nodes, updated_at):
    payload = {"version": 1, "updated_at": updated_at, "nodes": nodes}
    with open(os.path.join(str(tmp_path), _POOL_FILE), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


# ---------------------------------------------------------------- 加载 / 兜底
def test_seed_fallback_when_no_persistence():
    """无持久化文件 → 回落 SEED_SERVERS（永不返回空池）。"""
    assert tdx_nodes.get_servers() == list(tdx_nodes.SEED_SERVERS)
    st = tdx_nodes.status()
    assert st["pool_source"] == "seed"
    assert st["pool_size"] == len(tdx_nodes.SEED_SERVERS)
    assert st["updated_at"] is None


def test_persisted_pool_is_loaded(tmp_path):
    """有持久化 → 直接使用，零探测。"""
    _write_pool(tmp_path, [{"ip": "1.1.1.1", "port": 7709, "ms": 5.0},
                           {"ip": "2.2.2.2", "port": 7709, "ms": 9.0}],
                "2026-10-05T10:00:00")
    assert tdx_nodes.get_servers() == [("1.1.1.1", 7709), ("2.2.2.2", 7709)]
    st = tdx_nodes.status()
    assert st["pool_source"] == "persisted"
    assert st["pool_size"] == 2


def test_status_loads_pool_without_triggering_refresh(tmp_path, monkeypatch):
    """status() 首次调用即报出真实池大小，且只读诊断不触发重扫。"""
    _write_pool(tmp_path, [{"ip": "1.1.1.1", "port": 7709}], "2026-10-05T10:00:00")
    calls = []
    monkeypatch.setattr(tdx_nodes, "maybe_refresh_async",
                        lambda reason="auto": calls.append(reason) or True)
    st = tdx_nodes.status()
    assert st["pool_size"] == 1
    assert st["pool_source"] == "persisted"
    assert calls == []


def test_corrupt_persistence_falls_back_to_seed(tmp_path):
    """持久化损坏（非 JSON）→ 不抛异常，回落种子。"""
    with open(os.path.join(str(tmp_path), _POOL_FILE), "w", encoding="utf-8") as f:
        f.write("{ this is not json")
    assert tdx_nodes.get_servers() == list(tdx_nodes.SEED_SERVERS)
    assert tdx_nodes.status()["pool_source"] == "seed"


# ---------------------------------------------------------------- 重扫
def test_refresh_updates_pool_and_persists(tmp_path, monkeypatch):
    """重扫成功 → 切换池 + 落盘 + updated_at 更新。"""
    monkeypatch.setattr(tdx_nodes, "_load_builtin_hosts", lambda: [("a", "9.9.9.9", 7709)])
    monkeypatch.setattr(tdx_nodes, "_probe_hosts", lambda hosts: [("9.9.9.9", 7709, 3.0)])
    r = tdx_nodes.refresh("test")
    assert r == {"found": 1, "total": 1, "pool": 1}
    assert tdx_nodes.get_servers() == [("9.9.9.9", 7709)]
    assert tdx_nodes.status()["pool_source"] == "persisted"
    with open(os.path.join(str(tmp_path), _POOL_FILE), encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["nodes"][0]["ip"] == "9.9.9.9"


def test_refresh_keeps_pool_when_probe_empty(tmp_path, monkeypatch):
    """重扫全失败（found 空）→ 保留现有池，绝不清空。"""
    _write_pool(tmp_path, [{"ip": "1.1.1.1", "port": 7709}], "2026-10-05T10:00:00")
    tdx_nodes.get_servers()  # 载入持久化池
    before = tdx_nodes.status()
    monkeypatch.setattr(tdx_nodes, "_load_builtin_hosts", lambda: [("a", "9.9.9.9", 7709)])
    monkeypatch.setattr(tdx_nodes, "_probe_hosts", lambda hosts: [])
    r = tdx_nodes.refresh("test")
    assert r["found"] == 0
    assert tdx_nodes.get_servers() == [("1.1.1.1", 7709)]
    assert tdx_nodes.status()["updated_at"] == before["updated_at"]


def test_refresh_returns_none_when_hosts_unavailable(monkeypatch):
    """内置池不可用（pytdx 缺失）→ 返回 None，不误清池。"""
    monkeypatch.setattr(tdx_nodes, "_load_builtin_hosts", lambda: [])
    assert tdx_nodes.refresh("test") is None


# ---------------------------------------------------------------- 触发 / 节流
def test_stale_pool_triggers_background_refresh(monkeypatch):
    """种子/过期池 → get_servers 顺手触发后台重扫。"""
    calls = []
    monkeypatch.setattr(tdx_nodes, "maybe_refresh_async",
                        lambda reason="auto": calls.append(reason) or True)
    tdx_nodes.get_servers()
    assert calls == ["stale"]


def test_fresh_pool_does_not_trigger_refresh(tmp_path, monkeypatch):
    """新鲜持久化池 → 不触发重扫。"""
    _write_pool(tmp_path, [{"ip": "1.1.1.1", "port": 7709}],
                datetime.now().isoformat(timespec="seconds"))
    calls = []
    monkeypatch.setattr(tdx_nodes, "maybe_refresh_async",
                        lambda reason="auto": calls.append(reason) or True)
    tdx_nodes.get_servers()
    assert calls == []


def test_note_failure_throttled(monkeypatch):
    """全节点失败上报 → 首次触发，节流窗口内二次拒绝（防失败风暴）。"""
    monkeypatch.setattr(tdx_nodes, "maybe_refresh_async", _REAL_MAYBE_REFRESH_ASYNC)
    monkeypatch.setattr(tdx_nodes.threading, "Thread", _NoopThread)
    monkeypatch.setattr(tdx_nodes, "refresh", lambda reason="auto": {"found": 0, "total": 0, "pool": 0})
    assert tdx_nodes.note_failure() is True
    assert tdx_nodes.note_failure() is False


# ---------------------------------------------------------------- 探测判据
class _FakeAPI:
    """假 pytdx 连接：按调用序返回预设的 get_security_bars 结果。"""

    def __init__(self, bars_by_call=None, connect_ok=True, **_kw):
        self._bars = list(bars_by_call or [])
        self._i = 0
        self._connect_ok = connect_ok

    def connect(self, ip, port, time_out=None):
        return self._connect_ok

    def get_security_bars(self, cat, mkt, code, start, cnt):
        if self._i < len(self._bars):
            v = self._bars[self._i]
            self._i += 1
            return v
        return None

    def disconnect(self):
        pass


def test_probe_one_rejects_tcp_ok_but_no_kline():
    """TCP 可连但两个探针标的都取不到 bar → 不算可用节点。"""
    cls = lambda **kw: _FakeAPI(bars_by_call=[None, None])  # noqa: E731
    assert tdx_nodes._probe_one(cls, ("n", "1.1.1.1", 7709)) is None


def test_probe_one_accepts_when_kline_returned():
    """任一探针标的取到 bar → 视为支持 K 线，返回 (ip, port, ms)。"""
    cls = lambda **kw: _FakeAPI(bars_by_call=[None, [{"close": 1.0}]])  # noqa: E731
    got = tdx_nodes._probe_one(cls, ("n", "1.1.1.1", 7709))
    assert got is not None
    assert got[0] == "1.1.1.1"
    assert got[1] == 7709


def test_probe_one_connect_failure():
    """TCP 建连失败 → None。"""
    cls = lambda **kw: _FakeAPI(connect_ok=False)  # noqa: E731
    assert tdx_nodes._probe_one(cls, ("n", "1.1.1.1", 7709)) is None