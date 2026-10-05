# -*- coding: utf-8 -*-
"""F-302 期货分析流程 —— 验收测试。

断言来源（纪律 4）：
  feature-matrix.md:23 F-302「验收标准」列 =
    ① `POST /api/analyze {code:'rb2501',market_type:'futures'}` **过期返回「已到期/已交割」**
    ② 有效合约返回 `futures_contract.multiplier` 等元信息
  业务规则列 = 过期合约 `YYMM<now` 拦截；期货 `up_ratio=0.5`；方案 market/direction/period 全匹配
  平台差异列 = **过期判定依赖服务端时区**

用户已拍板（2026-09-12）：
  B29 = ① 期货主体同法上移 `engine/analyze_service.py: analyze_futures()`
  B30 = ① 过期合约 → 422 `CONTRACT_EXPIRED`
  B31 = ① 期货搜索 / 期货行情条**不随本条**，继续挂在 F-204 / F-201

⚠️ 环境说明（不是代码缺陷）：当前配置 `config/quant_model.json` **缺失**（仅存 .bak），
   引擎用自动兜底方案，且其中**无期货方案**。故：
   - 过期拦截 / 周期校验 / 时区边界 等"方案之前/之外"的判定 → 端到端实测；
   - 有效合约的 happy path → 用契约级断言（注入 engine 结果），不依赖期货方案存在。
"""

import ast
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENGINE_ANALYZE = ROOT / "engine" / "analyze_service.py"
UI_WEB_API = ROOT / "ui" / "web_api.py"


# ================================================================
# FS0：B29 上移的结构约束
# ================================================================
def test_fs0a_ui_analyze_futures_is_thin_delegation():
    """FS0a：`_analyze_futures` 退化为委托，评分链不再在 UI 层（AST 精确校验调用）。"""
    src = UI_WEB_API.read_text(encoding="utf-8")
    node = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "_analyze_futures"
    )
    calls = [ast.unparse(c.func) for c in ast.walk(node) if isinstance(c, ast.Call)]
    assert "analyze_futures" in calls, f"未委托到 engine.analyze_futures：{calls}"
    assert not [c for c in calls if "TradingPipeline" in c or "build_report_data" in c], (
        f"评分链/报告组装仍留在 UI 层：{calls}"
    )
    self_attrs = [
        n.attr for n in ast.walk(node)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self"
    ]
    assert set(self_attrs) <= {"_maybe_log_signal"}, f"仍耦合 UI 实例状态：{self_attrs}"


def test_fs0b_engine_side_has_no_ui_dependency():
    """FS0b：engine 侧不得 import `ui.*`（依赖方向）。"""
    import re

    src = ENGINE_ANALYZE.read_text(encoding="utf-8")
    assert not re.findall(r"^\s*(?:from|import)\s+ui(?:\.|\s|$).*$", src, re.M)


# ================================================================
# FS1/F S4/FS5：方案之前或之外的判定（端到端实测）
# ================================================================
def test_fs1_expired_contract_returns_422(client):
    """FS1（验收①）：过期合约 `rb2501` → 422 `CONTRACT_EXPIRED`，message 含「已到期/已交割」。"""
    r = client.post(
        "/api/analyze",
        json={"code": "rb2501", "market_type": "futures", "k_type": "日K"},
    )
    assert r.status_code == 422, f"应 422，实际 {r.status_code}：{r.text}"
    err = r.json()["error"]
    assert err["code"] == "CONTRACT_EXPIRED", err
    assert "已到期/已交割" in err["message"], err["message"]


def test_fs4_futures_has_no_weekly_period(client):
    """FS4：期货周期集沿用 F-203（无周K）→ 周K 请求 422 `UNSUPPORTED_PERIOD`。"""
    r = client.post(
        "/api/analyze", json={"code": "rb0", "market_type": "futures", "k_type": "周K"}
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "UNSUPPORTED_PERIOD"


def test_fs5_unknown_futures_symbol_returns_404(client):
    """FS5：未知期货品种 → 404（与 F-203 期货代码语义一致）。"""
    r = client.post(
        "/api/analyze", json={"code": "zzz9", "market_type": "futures", "k_type": "日K"}
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_fs6_expiry_boundary_uses_injected_now():
    """FS6（平台差异列：过期判定依赖服务端时区）：跨月边界由注入的 `now` 决定。

    `rb2609` 在 2026-09-30（YYMM=2609）为有效、在 2026-10-01（YYMM=2610）为过期。
    """
    from engine.analyze_service import analyze_futures

    # 2026-09-30：当前 YYMM=2609，合约 2609 未过期 → 不应返回「已到期/已交割」
    ok = analyze_futures("rb2609", k_type="日K", now=datetime(2026, 9, 30, 10, 0))
    assert "已到期/已交割" not in str(ok.get("error")), f"2609 在 2026-09-30 不应判过期：{ok}"

    # 2026-10-01：当前 YYMM=2610，合约 2609 已过期
    expired = analyze_futures("rb2609", k_type="日K", now=datetime(2026, 10, 1, 10, 0))
    assert "已到期/已交割" in str(expired.get("error")), f"2609 在 2026-10-01 应判过期：{expired}"


def test_fs6b_server_injects_shanghai_clock(monkeypatch):
    """FS6b：服务端确实注入 Asia/Shanghai 的 `now`（不是 naive 本地时间）。"""
    from server.adapters import engine_bridge
    from server.adapters import analysis

    seen = {}

    def _fake(symbol, *, k_type="日K", scheme_name=None, now=None, **kw):
        seen["now"] = now
        return {"error": "已到期/已交割"}

    monkeypatch.setattr(engine_bridge, "analyze_futures", _fake)
    monkeypatch.setattr(engine_bridge, "futures_spec", lambda code: object())
    with pytest.raises(Exception) as excinfo:
        analysis.analyze("rb2501", market_type="futures", k_type="日K")
    assert excinfo.value.code == "CONTRACT_EXPIRED"
    assert seen["now"] is not None and seen["now"].tzinfo is not None, "注入的时刻必须带时区"
    assert seen["now"].utcoffset().total_seconds() == 8 * 3600, f"应为 +08:00：{seen['now']}"


# ================================================================
# FS2/FS3/FS7：happy path 的响应整形（不依赖期货方案存在）
# ================================================================
def test_fs2_and_fs3_contract_shaping(client, monkeypatch):
    """FS2/FS3：有效期货 → 顶层暴露 `futures_contract.multiplier`，并回显市场/方向/合约元信息。"""
    from server.adapters import engine_bridge

    synthetic = {
        "success": True,
        "reportData": {
            "factorScore": 12.3,
            "status": "标准",
            "env": "中性",
            "entry_action": "标准建仓",
            "market_type": "futures",
            "futures_contract": {
                "symbol": "rb", "name": "螺纹钢", "exchange": "SHFE",
                "multiplier": 10, "margin_rate": 0.13, "tick_size": 1.0,
                "main_code": "rb0", "contract_month": None, "is_continuous": True,
            },
        },
        "report_text": "期货报告",
        "used_scheme_name": "期货日K", "used_scheme_period": "日K",
        "used_scheme_direction": "long", "scheme_summary": {"active_factor_count": 7},
    }
    monkeypatch.setattr(engine_bridge, "futures_spec", lambda code: object())
    monkeypatch.setattr(engine_bridge, "analyze_futures", lambda *a, **k: synthetic)

    r = client.post("/api/analyze", json={"code": "rb0", "market_type": "futures", "k_type": "日K"})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["futures_contract"]["multiplier"] == 10, body          # 验收②
    assert body["futures_contract"]["is_continuous"] is True           # FS3
    assert body["futures_contract"]["contract_month"] is None          # FS3
    assert body["used_scheme_market"] == "futures"                     # FS7
    assert body["used_scheme_direction"] == "long"
    assert body["entry_tier"] == "标准"


def test_fs7_futures_up_ratio_is_neutral():
    """FS7（业务规则：期货 `up_ratio=0.5`）：engine 侧固定 0.5 中性值。"""
    src = ENGINE_ANALYZE.read_text(encoding="utf-8")
    assert "up_ratio = 0.5" in src, "期货未使用 0.5 中性 up_ratio"
    assert "breadth_fetched=True" in src, "期货应标记 breadth_fetched=True 避免重试"
