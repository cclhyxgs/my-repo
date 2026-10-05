# -*- coding: utf-8 -*-
"""全市场扫描「技术指标二级过滤」链路回归测试。

覆盖 2026-09-17 用户实报「应用筛选没反应 / 结果仍是全市场」背后的三个缺陷：
  ① 空白种子缺 ``scan_tech_filter`` → ``_validate_config`` 以种子为骨架时把该键静默丢弃
     → ``get_scan_tech_filter()`` 恒为 None → 引擎直接跳过过滤；
  ② ``_validate_config`` 只遍历种子键 → **任何**种子外的新键（backtest 等）都会被丢；
  ③ 前端存的是中文标签（signalOptions 本身是中文数组），判定链只认英文 code
     → ``_evaluate_tech_signal`` 落到末尾 ``return False``。
"""
from engine import quant_config  # noqa: E402
from engine.unified_entry_logic import UnifiedEntryLogic as U  # noqa: E402

# 注意：**不要**在本模块设置 QUANT_SYSTEM_DIR。本文件测的是纯逻辑
# （_validate_config / _build_blank_config / 信号判定），不需要数据目录；
# 而一旦在模块顶层改动该环境变量，会连累同一次 pytest 会话里的其他套件
# （h5_skeleton 的 conftest 隔离失效 → 用全新空目录 → 出现 5 failed/16 errors 假象）。


def test_blank_seed_contains_scan_tech_filter():
    assert "scan_tech_filter" in quant_config._build_blank_config()


def test_validate_config_keeps_keys_outside_seed():
    raw = {
        "scan_tech_filter": [{"signal": "kdj_dead"}],
        "backtest": {"foo": 1},
        "thresholds": {"x": 1},
    }
    cfg = quant_config._validate_config(raw)
    assert cfg["scan_tech_filter"] == [{"signal": "kdj_dead"}]
    assert cfg["backtest"] == {"foo": 1}
    assert cfg["thresholds"] == {"x": 1}


def test_validate_config_fills_seed_defaults():
    cfg = quant_config._validate_config({})
    assert cfg["scan_tech_filter"] == []
    assert cfg["active_factors"] == []
    assert isinstance(cfg["enabled"], dict)


def test_signal_code_normalization():
    assert U._normalize_signal_code("KDJ死叉") == "kdj_dead"
    assert U._normalize_signal_code("布林带上轨突破") == "bb_breakout"
    assert U._normalize_signal_code("kdj_dead") == "kdj_dead"
    assert U._normalize_signal_code("未知信号") == "未知信号"
    assert U._normalize_signal_code(None) == ""


def test_chinese_label_matches_code_judgement():
    """中文标签与英文 code 必须同判定 —— 这是「筛选能生效」的前提。"""
    tech = {"sma_5": 10.0, "sma_20": 9.0, "sma_60": 8.0}
    market = {"ma_arrangement": "多头排列"}
    for label, code in [("KDJ死叉", "kdj_dead"), ("布林带上轨突破", "bb_breakout"),
                        ("站上MA5", "above_ma5"), ("多头排列", "bull_align")]:
        a = U._evaluate_tech_signal(label, tech, market, 11.0, {})
        b = U._evaluate_tech_signal(code, tech, market, 11.0, {})
        assert a == b, f"{label} 与 {code} 判定不一致: {a} vs {b}"


def test_chinese_label_conditions_do_filter():
    spec = [{"signal": "站上MA5"}]
    assert U._evaluate_tech_conditions(spec, {"sma_5": 10.0}, {}, 11.0, {}) is True
    assert U._evaluate_tech_conditions(spec, {"sma_5": 12.0}, {}, 11.0, {}) is False


def test_and_semantics_requires_all_conditions():
    spec = [{"signal": "站上MA5"}, {"signal": "多头排列"}]
    tech_ok = {"sma_5": 10.0}
    assert U._evaluate_tech_conditions(spec, tech_ok, {"ma_arrangement": "多头排列"}, 11.0, {}) is True
    assert U._evaluate_tech_conditions(spec, tech_ok, {"ma_arrangement": "空头排列"}, 11.0, {}) is False


def test_unknown_signal_is_not_treated_as_pass():
    """未知信号不得被当作「满足」，否则筛选会静默变成"全放行"。"""
    assert U._evaluate_tech_signal("unknown_signal_xyz", {}, {}, 1.0, {}) is False


# ─────────────────────────────────────────────────────────────
# 结果态标记缓存：清空条件必须复位（2026-09-19 探针实测复现的第二个缺陷）
# ─────────────────────────────────────────────────────────────
class _FakeApi:
    """_ensure_scan_tech_pass 只用到 self._is_basic_mode()。"""

    def _is_basic_mode(self):
        return False


def _snap(rsi):
    return {'tech': {'rsi_14': rsi}, 'market': {}, 'adx': {}}


def test_scan_tech_markers_are_reset_when_filter_cleared(monkeypatch):
    """条件清空后，上一次留下的 _tech_pass 标记必须清掉。

    否则 get_scan_progress 仍按旧标记裁剪（filtered = [_tech_pass]），
    表现为「清空条件后点应用筛选，结果不恢复」。
    """
    from engine import quant_config
    from ui.web_api import WebAPI

    results = [
        {'code': 'a', 'price': 10.0, 'tech_snapshot': _snap(80.0)},
        {'code': 'b', 'price': 10.0, 'tech_snapshot': _snap(20.0)},
    ]
    task = {}

    # ① 应用一条会筛掉 b 的条件
    monkeypatch.setattr(quant_config, 'get_scan_tech_filter',
                        lambda: [{'indicator': 'rsi_14', 'op': '>=', 'value': 70}])
    WebAPI._ensure_scan_tech_pass(_FakeApi(), task, results)
    assert task['_tech_filter_sig'] is not None
    assert [r.get('_tech_pass') for r in results] == [True, False]

    # ② 清空条件后再跑一次：标记必须全部消失，否则结果仍被旧条件裁剪
    monkeypatch.setattr(quant_config, 'get_scan_tech_filter', lambda: [])
    WebAPI._ensure_scan_tech_pass(_FakeApi(), task, results)
    assert task['_tech_filter_sig'] is None
    assert all('_tech_pass' not in r and '_tech_skip' not in r for r in results)
    # 缺失标记时 get_scan_progress 的 r.get('_tech_pass', True) 默认放行 → 全量恢复
    assert all(r.get('_tech_pass', True) for r in results)


def test_scan_tech_markers_refresh_when_filter_changes(monkeypatch):
    """条件从 X 改成 Y（都非空）时必须重判，不得复用旧标记。"""
    from engine import quant_config
    from ui.web_api import WebAPI

    results = [{'code': 'a', 'price': 10.0, 'tech_snapshot': _snap(80.0)}]
    task = {}

    monkeypatch.setattr(quant_config, 'get_scan_tech_filter',
                        lambda: [{'indicator': 'rsi_14', 'op': '>=', 'value': 90}])
    WebAPI._ensure_scan_tech_pass(_FakeApi(), task, results)
    assert results[0]['_tech_pass'] is False

    monkeypatch.setattr(quant_config, 'get_scan_tech_filter',
                        lambda: [{'indicator': 'rsi_14', 'op': '>=', 'value': 70}])
    WebAPI._ensure_scan_tech_pass(_FakeApi(), task, results)
    assert results[0]['_tech_pass'] is True


# ─────────────────────────────────────────────────────────────
# 自定义指标（cf_*）入筛选：全链路（2026-10-05）
# ─────────────────────────────────────────────────────────────
def test_custom_indicator_condition_filters_scan_results(monkeypatch):
    """自定义公式因子可作为「技术指标筛选」条件并真正筛掉结果。

    链路：factor_values(自定义因子原始值) → build_tech_snapshot 写入 cf_* →
    _ensure_scan_tech_pass 用 _evaluate_tech_conditions 阈值比较 → _tech_pass 标记。
    """
    from engine import quant_config
    from engine.market_scan_core import build_tech_snapshot
    from ui.web_api import WebAPI

    results = [
        {'code': 'a', 'price': 10.0,
         'tech_snapshot': build_tech_snapshot({}, {}, {}, {'cf_my_rsi': 80.0, 'rsi_14': 10.0})},
        {'code': 'b', 'price': 10.0,
         'tech_snapshot': build_tech_snapshot({}, {}, {}, {'cf_my_rsi': 20.0, 'rsi_14': 90.0})},
    ]
    # 只按自定义指标筛（内置 rsi_14 故意反向，确保命中的是 cf_ 键而非内置键）
    monkeypatch.setattr(quant_config, 'get_scan_tech_filter',
                        lambda: [{'indicator': 'cf_my_rsi', 'op': '>=', 'value': 70}])
    WebAPI._ensure_scan_tech_pass(_FakeApi(), {}, results)
    assert [r.get('_tech_pass') for r in results] == [True, False]


# ─────────────────────────────────────────────────────────────
# 期货（多/空方案隔离）方向化判定：2026-10-05
# ─────────────────────────────────────────────────────────────
def _snap_ma5(sma5=10.0):
    """tech 只有 sma_5，配合 price 即可判定 above_ma5 / below_ma5。"""
    return {'tech': {'sma_5': sma5}, 'market': {}, 'adx': {}}


def test_scan_task_direction_defaults_long():
    """股票任务无 scan_direction → 恒 long；期货空单方案取 'short'。"""
    from ui.web_api import WebAPI

    assert WebAPI._scan_task_direction(None) == 'long'
    assert WebAPI._scan_task_direction({}) == 'long'
    assert WebAPI._scan_task_direction({'scan_direction': 'short'}) == 'short'
    assert WebAPI._scan_task_direction({'scan_direction': 'long'}) == 'long'


def test_ensure_scan_tech_pass_uses_task_direction(monkeypatch):
    """空单方案必须按 short 翻转信号，否则筛选语义与实际入场方向相反。

    price=11 > MA5=10：多头口径「站上MA5」命中；空头口径翻转为「跌破MA5」→ 不命中。
    """
    from engine import quant_config
    from ui.web_api import WebAPI

    monkeypatch.setattr(quant_config, 'get_scan_tech_filter',
                        lambda: [{'signal': 'above_ma5'}])

    long_results = [{'code': 'a', 'price': 11.0, 'tech_snapshot': _snap_ma5()}]
    WebAPI._ensure_scan_tech_pass(_FakeApi(), {'scan_direction': 'long'}, long_results)
    assert long_results[0]['_tech_pass'] is True

    short_results = [{'code': 'a', 'price': 11.0, 'tech_snapshot': _snap_ma5()}]
    WebAPI._ensure_scan_tech_pass(_FakeApi(), {'scan_direction': 'short'}, short_results)
    assert short_results[0]['_tech_pass'] is False


def test_apply_scan_tech_filter_respects_direction(monkeypatch):
    """结果态过滤同样按传入方向判定（期货不再被 is_futures 短路）。"""
    from engine import quant_config
    from ui.web_api import WebAPI

    monkeypatch.setattr(quant_config, 'get_scan_tech_filter',
                        lambda: [{'signal': 'above_ma5'}])
    rows = [{'code': 'a', 'price': 11.0, 'tech_snapshot': _snap_ma5()}]

    kept, _ = WebAPI._apply_scan_tech_filter(_FakeApi(), list(rows), direction='long')
    assert [r['code'] for r in kept] == ['a']
    kept, _ = WebAPI._apply_scan_tech_filter(_FakeApi(), list(rows), direction='short')
    assert kept == []


def test_futures_snapshot_supports_builtin_and_custom_indicator(monkeypatch):
    """期货扫描结果快照（build_tech_snapshot(tech, market, adx, factor_values)）可筛：
    内置数值指标 + 自定义指标都能按阈值命中。"""
    from engine import quant_config
    from engine.market_scan_core import build_tech_snapshot
    from ui.web_api import WebAPI

    snap = build_tech_snapshot(
        {'rsi_14': 25.0}, {'ma_arrangement': 2, 'volume_price': '放量上涨'}, {'adx': 30},
        {'cf_my_rsi': 80.0})
    results = [{'code': 'rb0', 'price': 3000.0, 'tech_snapshot': snap}]

    monkeypatch.setattr(quant_config, 'get_scan_tech_filter',
                        lambda: [{'indicator': 'rsi_14', 'op': '<=', 'value': 30},
                                 {'indicator': 'cf_my_rsi', 'op': '>=', 'value': 70}])
    WebAPI._ensure_scan_tech_pass(_FakeApi(), {'scan_direction': 'long'}, results)
    assert results[0]['_tech_pass'] is True

    # 阈值不满足 → 空单/多单都不命中
    monkeypatch.setattr(quant_config, 'get_scan_tech_filter',
                        lambda: [{'indicator': 'cf_my_rsi', 'op': '>=', 'value': 90}])
    WebAPI._ensure_scan_tech_pass(_FakeApi(), {'scan_direction': 'short'}, results)
    assert results[0]['_tech_pass'] is False
