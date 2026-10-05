# -*- coding: utf-8 -*-
"""出网代理层（engine/net_proxy.py）确定性单测。

全部离线：不真的发请求，只验配置解析、选取策略、失败拉黑、环境变量覆盖与掩码。
"""
import json
import os
import tempfile

# 必须在 import engine.* 之前隔离数据目录，避免 engine.config 触碰真实用户配置
os.environ.setdefault("QUANT_SYSTEM_DIR", tempfile.mkdtemp(prefix="mbull_netproxy_"))

from engine import net_proxy  # noqa: E402


def _use_conf(tmp_path, payload):
    """把 net_proxy 的配置路径固定到临时文件（绕开 engine.config 的真实 CONFIG_DIR）。"""
    p = tmp_path / "proxy.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    net_proxy._config_path_cached = str(p)
    net_proxy.reset()
    net_proxy._config_path_cached = str(p)   # reset 不清它，这里显式保证
    return p


def test_disabled_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("MBULL_PROXY", raising=False)
    net_proxy._config_path_cached = str(tmp_path / "not_exists.json")
    net_proxy.reset()
    net_proxy._config_path_cached = str(tmp_path / "not_exists.json")
    assert net_proxy.enabled() is False
    assert net_proxy.acquire("tencent_kline") is None
    assert net_proxy.has_alive() is False


def test_env_var_overrides_and_enables(tmp_path, monkeypatch):
    _use_conf(tmp_path, {"enabled": False, "proxies": []})
    monkeypatch.setenv("MBULL_PROXY", "http://1.1.1.1:8080, socks5://2.2.2.2:1080")
    conf = net_proxy.load_config(force_reload=True)
    assert conf["enabled"] is True
    assert conf["proxies"] == ["http://1.1.1.1:8080", "socks5://2.2.2.2:1080"]


def test_invalid_mode_and_limits_fall_back(tmp_path, monkeypatch):
    monkeypatch.delenv("MBULL_PROXY", raising=False)
    _use_conf(tmp_path, {"enabled": True, "mode": "whatever", "strategy": "zzz",
                         "proxies": ["http://a:1"], "max_fails": 0, "blacklist_sec": -5})
    conf = net_proxy.load_config(force_reload=True)
    assert conf["mode"] == "on_failure"
    assert conf["strategy"] == "round_robin"
    # max_fails=0 无意义（≥1）→ 回退默认；blacklist_sec=0 有语义（不拉黑）→ 只钳负值
    assert conf["max_fails"] == 3
    assert conf["blacklist_sec"] == 0


def test_on_failure_prefers_direct_then_switches(tmp_path, monkeypatch):
    """核心语义：直连优先；出现失败记录后才给代理（避免为一次抖动白绕代理）。"""
    monkeypatch.delenv("MBULL_PROXY", raising=False)
    _use_conf(tmp_path, {"enabled": True, "mode": "on_failure",
                         "proxies": ["http://p1:1", "http://p2:2"]})
    assert net_proxy.acquire("tencent_kline") is None          # 首次：直连
    net_proxy.report("tencent_kline", None, ok=False)          # 直连失败
    got = net_proxy.acquire("tencent_kline")
    assert got in ("http://p1:1", "http://p2:2")               # 第二次：走代理


def test_always_mode_returns_proxy_immediately(tmp_path, monkeypatch):
    monkeypatch.delenv("MBULL_PROXY", raising=False)
    _use_conf(tmp_path, {"enabled": True, "mode": "always", "proxies": ["http://p1:1"]})
    assert net_proxy.acquire("sina_realtime") == "http://p1:1"
    assert net_proxy.has_alive() is True


def test_round_robin_rotates(tmp_path, monkeypatch):
    monkeypatch.delenv("MBULL_PROXY", raising=False)
    _use_conf(tmp_path, {"enabled": True, "mode": "always", "strategy": "round_robin",
                         "proxies": ["http://p1:1", "http://p2:2"]})
    seq = [net_proxy.acquire("s") for _ in range(4)]
    assert len(set(seq)) == 2, f"应当在两个代理间轮换，实际 {seq}"


def test_blacklist_after_max_fails_then_recover(tmp_path, monkeypatch):
    monkeypatch.delenv("MBULL_PROXY", raising=False)
    _use_conf(tmp_path, {"enabled": True, "mode": "always",
                         "proxies": ["http://p1:1"], "max_fails": 3, "blacklist_sec": 300})
    for _ in range(3):
        net_proxy.report("s", "http://p1:1", ok=False)
    assert net_proxy.acquire("s") is None            # 已被拉黑
    assert net_proxy.has_alive() is False            # → 调用方据此决定是否冷却整源
    net_proxy.reset()
    net_proxy._config_path_cached = str(tmp_path / "proxy.json")
    assert net_proxy.acquire("s") == "http://p1:1"   # 重置后恢复


def test_success_clears_fail_counter(tmp_path, monkeypatch):
    monkeypatch.delenv("MBULL_PROXY", raising=False)
    _use_conf(tmp_path, {"enabled": True, "mode": "always",
                         "proxies": ["http://p1:1"], "max_fails": 3})
    net_proxy.report("s", "http://p1:1", ok=False)
    net_proxy.report("s", "http://p1:1", ok=True)
    net_proxy.report("s", "http://p1:1", ok=False)
    assert net_proxy.acquire("s") == "http://p1:1"   # 未被拉黑（计数已清零）


def test_to_requests_shape():
    assert net_proxy.to_requests(None) is None
    assert net_proxy.to_requests("http://p:1") == {"http": "http://p:1", "https": "http://p:1"}


def test_mask_hides_credentials():
    masked = net_proxy._mask("http://user:secret@1.2.3.4:8080")
    assert "secret" not in masked and "1.2.3.4:8080" in masked


def test_status_shape(tmp_path, monkeypatch):
    monkeypatch.delenv("MBULL_PROXY", raising=False)
    _use_conf(tmp_path, {"enabled": True, "mode": "always", "proxies": ["http://p1:1"]})
    st = net_proxy.status()
    assert st["enabled"] is True and st["configured"] == 1 and st["alive"] == 1
    assert st["pool"][0]["blacklisted_sec"] == 0
