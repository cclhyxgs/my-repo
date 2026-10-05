# -*- coding: utf-8 -*-
"""市场情绪门控 + 财务按日缓存 的单元测试。

覆盖：
- MarketGate.get_environment / apply_gate 的 sentiment 微调（additive，缺省零影响）
- tdX fetch_finance 进程内按日缓存（日内复用一个网络请求）
"""
import engine.market_gate as market_gate
from engine.market_gate import MarketGate
from engine.data_sources import tdx as tdx_mod


def _enable_gate(monkeypatch):
    monkeypatch.setattr(market_gate, "is_market_gate_enabled", lambda market: True)


# ---------------------------------------------------------------- 情绪门控

def test_sentiment_none_unchanged():
    """不传 sentiment → 行为与改动前一致（不启用门控时 factor 保持 1.0）。"""
    env = MarketGate.get_environment(0.5, "stock")
    assert env["factor"] == 1.0
    assert env["limit"] == 1.0


def test_sentiment_below_threshold_noop(monkeypatch):
    """家数未达阈值 → 不修正（factor 保持 base）。"""
    _enable_gate(monkeypatch)
    env = MarketGate.get_environment(0.5, "stock", {"limit_up": 30, "limit_down": 5})
    assert env["factor"] == 1.0


def test_sentiment_overheat_dampens(monkeypatch):
    """涨停家数过热 → 仓位参考 factor 下调（×0.94）。"""
    _enable_gate(monkeypatch)
    base = MarketGate.get_environment(0.5, "stock")["factor"]  # 未启用 gate 值为 1.0
    hot = MarketGate.get_environment(
        0.5, "stock", {"limit_up": 150, "limit_down": 10}
    )
    assert hot["factor"] < base
    assert abs(hot["factor"] - base * 0.94) < 1e-9


def test_sentiment_panic_dampens(monkeypatch):
    """跌停家数恐慌 → 仓位参考 factor 下调（×0.92）。"""
    _enable_gate(monkeypatch)
    base = MarketGate.get_environment(0.5, "stock")["factor"]
    panic = MarketGate.get_environment(
        0.5, "stock", {"limit_up": 30, "limit_down": 80}
    )
    assert panic["factor"] < base
    assert abs(panic["factor"] - base * 0.92) < 1e-9


def test_sentiment_futures_ignored(monkeypatch):
    """期货恒中性：即便给 sentiment 也不改 factor。"""
    _enable_gate(monkeypatch)
    fut = MarketGate.get_environment(0.5, "futures", {"limit_up": 150, "limit_down": 5})
    assert fut["factor"] == 1.0


def test_sentiment_disabled_gate_ignored(monkeypatch):
    """门控关闭时 sentiment 不生效（factor 保持 1.0）。"""
    monkeypatch.setattr(market_gate, "is_market_gate_enabled", lambda market: False)
    env = MarketGate.get_environment(0.5, "stock", {"limit_up": 150, "limit_down": 5})
    assert env["factor"] == 1.0


def test_apply_gate_sentiment_lowers_position(monkeypatch):
    """仓位参考随情绪过热下调：actual_position 减小。"""
    _enable_gate(monkeypatch)
    p0, _ = MarketGate.apply_gate(0.5, 0.6, "stock")
    p1, _ = MarketGate.apply_gate(0.5, 0.6, "stock", {"limit_up": 130, "limit_down": 5})
    assert p1 < p0


# ---------------------------------------------------------------- 财务缓存

def test_fetch_finance_cached_intraday(monkeypatch):
    """日内复用一个 tdx 网络请求（第二次命中缓存）。"""
    calls = {"n": 0}

    def fake_call(fn):
        calls["n"] += 1
        return {"meigujingzichan": 3.5, "jinglirun": 0.1, "updated_date": 20260924}

    monkeypatch.setattr(tdx_mod, "_call", fake_call)
    monkeypatch.setattr(tdx_mod, "_preflight", lambda: None)
    tdx_mod._finance_cache.clear()
    try:
        f1 = tdx_mod.fetch_finance("600000")
        f2 = tdx_mod.fetch_finance("600000")
        assert calls["n"] == 1, "第二次调用应命中缓存"
        assert f1 is not None and f2 is not None
        assert f1[0]["meigujingzichan"] == 3.5
        assert f2[0]["meigujingzichan"] == 3.5
    finally:
        tdx_mod._finance_cache.clear()


def test_fetch_finance_failure_not_cached(monkeypatch):
    """抓取失败不缓存（下次可重试）；且不污染后续调用。"""
    def fail_call(fn):
        return None

    monkeypatch.setattr(tdx_mod, "_call", fail_call)
    monkeypatch.setattr(tdx_mod, "_preflight", lambda: None)
    tdx_mod._finance_cache.clear()
    try:
        fin, err = tdx_mod.fetch_finance("600000")
        assert fin is None and err
        assert "600000" not in tdx_mod._finance_cache
    finally:
        tdx_mod._finance_cache.clear()