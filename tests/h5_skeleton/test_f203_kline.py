# -*- coding: utf-8 -*-
"""F-203 K 线查询 —— 验收测试。

断言来源（纪律 4，不得自行发明）：
  feature-matrix.md:20 F-203「验收标准」列 =
    ① `GET /api/kline?code=sh600519&period=日K&days=300` 返回 300 根序列
    ② 期货 `rb0` 日K 夜盘合并到下一交易日
  业务规则列 = 周期 日/周/60m/30m/15m/5m/1m；腾讯源仅日/周；日K300/分钟1023 根
  输出契约 = app-architecture §3.2 `{code, period, bars[{t,o,h,l,c,v}]}` ∥ 404/422
  时区    = §2.13-2 `Asia/Shanghai`（夜盘归属，R-19）

FT0 为 B7 方案 1（夜盘聚合上移 engine）的**结构等价断言**：
  冻结 hash 由「上移前 UI 层函数体」的 AST dump 计算，比对不通过即说明上移产生了语义漂移。
"""

import ast
import hashlib
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

# 上移前 `ui/web_api.py::WebAPI._merge_futures_night` 函数体（去 docstring）AST dump 的 sha256
FROZEN_NIGHT_BODY_SHA256 = "8c69ee2ae47f9870bf8e1945903480016da4e8e4ac56b28519a94247e8f31a49"

ROOT = Path(__file__).resolve().parents[2]
UI_WEB_API = ROOT / "ui" / "web_api.py"
ENGINE_NIGHT = ROOT / "engine" / "futures_night.py"


# ---------------------------------------------------------------- helpers
def _body_ast(path: Path, func_name: str) -> str:
    """取函数体（剔除 docstring）的 AST dump —— 结构等价判据，忽略缩进与注释。"""
    src = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0].value, "value", None), str):
                body = body[1:]
            return ast.dump(ast.Module(body=body, type_ignores=[]))
    raise AssertionError(f"{path} 中未找到 {func_name}")


def _daily_df() -> pd.DataFrame:
    """合成日K：周三~周五（09-09 ~ 09-11）。"""
    return pd.DataFrame({
        "trade_time": pd.to_datetime(["2026-09-09", "2026-09-10", "2026-09-11"]),
        "open": [3000.0, 3010.0, 3020.0],
        "high": [3050.0, 3060.0, 3070.0],
        "low": [2980.0, 2990.0, 3000.0],
        "close": [3010.0, 3020.0, 3030.0],
        "volume": [1000.0, 1100.0, 1200.0],
    })


def _minute_df() -> pd.DataFrame:
    """合成 5 分钟：周四夜盘（归 09-10）、周五夜盘 + 周六凌晨段（均归 09-11）。"""
    return pd.DataFrame({
        "trade_time": pd.to_datetime([
            "2026-09-10 21:05", "2026-09-10 22:00",
            "2026-09-11 21:05", "2026-09-11 22:30",
            "2026-09-12 01:30",
        ]),
        "open": [3100.0, 3110.0, 3200.0, 3210.0, 3250.0],
        "high": [3120.0, 3130.0, 3240.0, 3260.0, 3260.0],
        "low": [3090.0, 3100.0, 3190.0, 3200.0, 3240.0],
        "close": [3110.0, 3120.0, 3230.0, 3250.0, 3255.0],
        "volume": [10.0, 20.0, 30.0, 40.0, 50.0],
    })


@pytest.fixture()
def patched_minute(monkeypatch):
    """把 engine.futures_data.fetch_futures_minute 换成合成数据（确定性）。"""
    from engine import futures_data

    monkeypatch.setattr(futures_data, "fetch_futures_minute", lambda *a, **k: _minute_df())
    return _minute_df()


# ---------------------------------------------------------------- FT0（B7 结构等价）
def test_ft0a_night_logic_body_identical_to_pre_move_frozen_hash():
    """FT0a：engine 中夜盘聚合函数体 == 上移前 UI 层函数体（AST 结构，冻结 hash）。"""
    digest = hashlib.sha256(_body_ast(ENGINE_NIGHT, "merge_night_session").encode("utf-8")).hexdigest()
    assert digest == FROZEN_NIGHT_BODY_SHA256, (
        f"夜盘聚合函数体已漂移：{digest} != {FROZEN_NIGHT_BODY_SHA256}（上移引入了语义改动）"
    )


def test_ft0b_ui_layer_is_thin_delegation():
    """FT0b：UI 层不再是实现方，而是薄委托（否则两份实现会漂移）。"""
    body = _body_ast(UI_WEB_API, "_merge_futures_night")
    assert "merge_night_session" in body, "UI 层未委托给 engine.futures_night"
    assert "bdate_range" not in body and "fetch_futures_minute" not in body, "UI 层仍保留夜盘实现细节"
    assert body.count("Return") == 1, f"UI 层委托体应为单条 return，实际 AST：{body[:200]}"


# ---------------------------------------------------------------- FT2 夜盘合并（确定性）
def test_ft2a_night_bar_belongs_to_next_trading_day(patched_minute):
    """FT2 核心（验收②）：夜盘聚合成一根，且归属**下一交易日**（09-11 → 09-14，跨周末）。"""
    from engine.futures_night import merge_night_session

    out = merge_night_session(_daily_df(), "rb", "113.rb0")
    assert len(out) == 4, f"应追加 1 根夜盘K线，实际 {len(out)} 根"

    last = out.iloc[-1]
    assert bool(last["_is_night"]) is True
    assert pd.Timestamp(last["trade_time"]).date() == date(2026, 9, 14), (
        f"夜盘归属应为下一交易日 2026-09-14，实际 {last['trade_time']}"
    )
    # 夜盘 OHLCV 聚合口径：open=首、high=最高、low=最低、close=末、volume=合计
    assert last["open"] == pytest.approx(3200.0)
    assert last["high"] == pytest.approx(3260.0)
    assert last["low"] == pytest.approx(3190.0)
    assert last["close"] == pytest.approx(3255.0)
    assert last["volume"] == pytest.approx(120.0)


def test_ft2b_night_bar_does_not_duplicate_when_next_day_exists():
    """FT2 边界：下一交易日日K 已存在 → 不再追加（防重复 bar）。"""
    from engine.futures_night import merge_night_session

    df = _daily_df()
    df.loc[len(df)] = [pd.Timestamp("2026-09-14"), 3300.0, 3350.0, 3280.0, 3320.0, 1500.0]
    from engine import futures_data
    orig = futures_data.fetch_futures_minute
    futures_data.fetch_futures_minute = lambda *a, **k: _minute_df()
    try:
        out = merge_night_session(df, "rb", "113.rb0")
    finally:
        futures_data.fetch_futures_minute = orig
    assert len(out) == len(df), "下一交易日已存在时不应追加夜盘K线"


def test_ft6a_midnight_bars_belong_to_previous_trading_day(patched_minute):
    """FT6（时区/归属，§2.13-2 + R-19）：凌晨 01:30 归**前一交易日**，不得算作次日。"""
    from engine.futures_night import merge_night_session

    out = merge_night_session(_daily_df(), "rb", "113.rb0")
    night = out.iloc[-1]
    # 01:30 的 bar 归 09-11 → 参与聚合（其 close 作为夜盘收盘），且聚合日 ≠ 01-30 当天
    assert night["close"] == pytest.approx(3255.0), "凌晨段未按前一交易日并入聚合"
    assert pd.Timestamp(night["trade_time"]).date() == date(2026, 9, 14)


def test_ft2c_noop_without_night_bars(monkeypatch):
    """FT2 边界：无夜盘 bar → 原 df 原样返回。"""
    from engine import futures_data
    from engine.futures_night import merge_night_session

    day_only = _minute_df()
    day_only["trade_time"] = pd.to_datetime(["2026-09-11 09:05", "2026-09-11 10:00",
                                             "2026-09-11 11:00", "2026-09-11 14:00",
                                             "2026-09-11 15:00"])
    monkeypatch.setattr(futures_data, "fetch_futures_minute", lambda *a, **k: day_only)
    df = _daily_df()
    out = merge_night_session(df, "rb", "113.rb0")
    assert len(out) == len(df)


def test_ft2d_empty_input_safe():
    """FT2 边界：空 df 不抛错。"""
    from engine.futures_night import merge_night_session

    assert merge_night_session(pd.DataFrame(), "rb", "113.rb0").empty


def test_ft2e_upstream_failure_preserved_as_noop(monkeypatch):
    """FT2 行为契约：数据源异常时静默返回原 df —— **上移前既有语义，刻意保留**。"""
    from engine import futures_data
    from engine.futures_night import merge_night_session

    def _boom(*a, **k):
        raise RuntimeError("source down")

    monkeypatch.setattr(futures_data, "fetch_futures_minute", _boom)
    df = _daily_df()
    out = merge_night_session(df, "rb", "113.rb0")
    assert len(out) == len(df), "既有语义被改变：异常时不应改变 df"


# ---------------------------------------------------------------- FT1 / FT2 真数据
def test_ft1_daily_kline_300_live(client):
    """FT1（验收①）：`code=sh600519&period=日K&days=300` → 200 且 300 根序列。"""
    r = client.get("/api/kline", params={"code": "sh600519", "period": "日K", "days": 300})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code"] == "sh600519"
    assert body["period"] == "日K"
    assert body["count"] == 300, f"应返回 300 根，实际 {body['count']}"
    assert len(body["bars"]) == 300


def test_ft2f_futures_rb0_daily_night_live(client):
    """FT2（验收②，逐字用验收标准的 `rb0`）：最后一根为夜盘，且归属下一交易日。

    ⚠️ 时段性（2026-09-15 追加说明）：夜盘 bar **只有**在「夜盘已发生 且 其归属日的
    下一交易日日K尚未生成」时才单独追加（`engine/futures_night.py:69-70`——下一交易日
    日K一旦存在，夜盘已自然并入其中，不再重复追加）。因此「最后一根必为夜盘」只在
    周末/节假日前夜成立；周一~周五盘后运行时夜盘已并入真实日K，该断言退化为
    「夜盘归属规则无违规 + 序列无重复/乱序」，否则本用例每个工作日约 18 小时假红。
    """
    r = client.get("/api/kline", params={"code": "rb0", "period": "日K", "days": 300})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_futures"] is True
    # days=300 → 不得超过 300 根（源站/缓存都可能多返回，engine 侧须截尾）
    assert body["count"] == 300, f"应返回 300 根，实际 {body['count']}"

    bars = body["bars"]
    last = bars[-1]
    seq = [b["t"] for b in bars]
    assert seq == sorted(seq), "bars 必须按时间升序"
    assert len(set(seq)) == len(seq), "同一交易日不得出现重复 bar"

    flagged = [b for b in bars if b.get("is_night")]
    if last.get("is_night") is True:
        night_day = pd.Timestamp(last["t"]).date()
        # 夜盘归属下一交易日：必须晚于期间内最后一个真实日K交易日，且为工作日
        assert night_day.weekday() < 5, f"夜盘归属日 {night_day} 落在周末，归属规则错"
        assert night_day > pd.Timestamp(bars[-2]["t"]).date(), "夜盘日应晚于其所属交易日"
        # 回归：`pd.concat` 追加后原日K行的 `_is_night` 列为 NaN，而 bool(NaN)==True，
        # 若不做显式 notna 判定会把**全部**K线误标为夜盘（engine `web_api.py:1308-1309` 同陷阱）。
        assert len(flagged) == 1, f"应只有 1 根夜盘K线，实际 {len(flagged)} 根（NaN 真值陷阱）"
        assert flagged[0]["t"] == last["t"]
    else:
        # 工作日盘后：夜盘已并入次日真实日K → 不应有孤立的夜盘 bar 残留
        assert not flagged, f"非夜盘收尾时不应出现夜盘 bar：{flagged}"


def test_ft2g_nan_night_flag_not_treated_as_true():
    """FT2 回归（确定性）：`_is_night` 为 NaN / 缺失时不得判为夜盘。"""
    from server.adapters.market.kline import _night_flag

    assert _night_flag({"t": "2026-09-11", "_is_night": float("nan")}) is False
    assert _night_flag({"t": "2026-09-11"}) is False
    assert _night_flag({"t": "2026-09-11", "_is_night": None}) is False
    assert _night_flag({"t": "2026-09-11", "_is_night": True}) is True
    assert _night_flag({"t": "2026-09-11", "is_night": True}) is True


# ---------------------------------------------------------------- FT3~FT5 错误分支
def test_ft3_futures_week_period_rejected_422(client):
    """FT3（B8 已定）：期货不支持周K → 422，不静默降级。"""
    r = client.get("/api/kline", params={"code": "rb0", "period": "周K", "days": 100})
    assert r.status_code == 422, f"应 422，实际 {r.status_code}: {r.text}"
    assert r.json()["error"]["code"] == "UNSUPPORTED_PERIOD"


def test_ft3b_unknown_period_rejected_422(client):
    """FT3 边界：契约外语义周期名 → 422（非静默降级为 日K）。"""
    r = client.get("/api/kline", params={"code": "sh600519", "period": "60m", "days": 100})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "UNSUPPORTED_PERIOD"


def test_ft4_daily_days_over_limit_rejected_422(client):
    """FT4（业务规则：日K 300 / 分钟 1023 根上限）：日K 超限 → 422。"""
    r = client.get("/api/kline", params={"code": "sh600519", "period": "日K", "days": 500})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "DAYS_OUT_OF_RANGE"


def test_ft4b_limit_rule_source_independent():
    """FT4 补充：300/1023 的边界判定与数据源配置无关（直接验规则层，确定性）。

    单测规则层而非走 HTTP，是因为分钟级能否走到该规则取决于 `kline_source`
    （见 FT4c），不应让上限断言随部署配置漂移。
    """
    from server.adapters.market.kline import _normalize_days
    from server.core.errors import ApiError

    assert _normalize_days("日K", 300) == 300
    assert _normalize_days("周K", 300) == 300
    assert _normalize_days("5分钟", 1023) == 1023
    assert _normalize_days("日K", None) == 300
    assert _normalize_days("5分钟", None) == 1023

    for period, over in (("日K", 301), ("周K", 301), ("5分钟", 1024), ("1分钟", 1024), ("日K", 0)):
        with pytest.raises(ApiError) as excinfo:
            _normalize_days(period, over)
        assert excinfo.value.code == "DAYS_OUT_OF_RANGE", f"{period}={over} 未按超限拒绝"
        assert excinfo.value.status_code == 422, "必须 422（B8），不得 400/静默截断"


def test_ft4c_minute_period_gated_by_source_config(client):
    """FT4c（源依赖，如实断言）：分钟级可用性取决于 `kline_source`，不可用时必须 422。

    通达信源（engine 默认）7 周期全开；腾讯源仅 日K/周K；新浪源 7 周期全开。
    本条不断言部署用哪种源，只断言「不可用时不得静默降级为日K」。
    """
    from server.adapters.market.kline import supported_periods

    supported = supported_periods("stock")
    assert set(supported) <= {"日K", "周K", "60分钟", "30分钟", "15分钟", "5分钟", "1分钟"}
    assert {"日K", "周K"} <= set(supported), "日K/周K 必须恒可用"

    if "5分钟" in supported:
        assert set(supported) == {"日K", "周K", "60分钟", "30分钟", "15分钟", "5分钟", "1分钟"}
    else:
        r = client.get("/api/kline", params={"code": "sh600519", "period": "5分钟", "days": 100})
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "UNSUPPORTED_PERIOD"


def test_ft5_unknown_code_rejected_404(client):
    """FT5（§3.2 `∥404`）：不存在的代码 → 404。"""
    r = client.get("/api/kline", params={"code": "sh999999", "period": "日K", "days": 300})
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_ft5b_empty_code_rejected_422(client):
    """FT5 边界：空 code → 422。"""
    r = client.get("/api/kline", params={"code": "  ", "period": "日K", "days": 300})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_CODE"


# ---------------------------------------------------------------- FT7 / FT8 契约
def test_ft7_repeat_request_idempotent_and_contract_fields(client):
    """FT7：缓存语义（R-08）—— 同请求两次返回完全一致；`cached`/`source` 字段齐备。

    注：`cached` 采用「磁盘 store mtime 未变即命中」的启发式判定，故此处只硬断言
    字段存在与响应幂等，不断言 cached 恒为 True（那是引擎内部行为，不能靠外部臆测）。
    """
    params = {"code": "sh600519", "period": "日K", "days": 300}
    first = client.get("/api/kline", params=params).json()
    second = client.get("/api/kline", params=params).json()

    assert isinstance(first["cached"], bool)
    assert isinstance(first["source"], str) and first["source"]
    assert first["bars"] == second["bars"], "同一请求两次返回不一致，缓存层有脏数据"


def test_ft8_bar_time_key_is_iso_and_schema_fixed(client):
    """FT8（§3.2 契约）：bars 键名严格 t/o/h/l/c/v；日K 的 t 为 `YYYY-MM-DD`。"""
    body = client.get("/api/kline", params={"code": "sh600519", "period": "日K", "days": 60}).json()
    allowed = {"t", "o", "h", "l", "c", "v", "is_night"}
    for bar in body["bars"]:
        assert set(bar).issubset(allowed), f"出现契约外键：{set(bar) - allowed}"
        assert {"t", "o", "h", "l", "c", "v"} <= set(bar), f"缺契约键：{bar}"
    import re
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", body["bars"][-1]["t"]), body["bars"][-1]["t"]
