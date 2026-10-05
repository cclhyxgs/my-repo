# -*- coding: utf-8 -*-
"""全市场扫描「财务筛选」链路回归测试（2026-10-06 新增）。

覆盖：技术筛选之后对前 TopN 候选叠加财务筛选——
爆雷硬剔除（每股净资产/净利<0，恒生效）+ 高级模式可配置阈值
（PB/PE/总市值/EPS/资产负债率）；剔除不补位、查询失败安全放行、
enabled=False 性能短路；配置种子键 + config_mapper 双向映射。
"""
from engine.market_scan_core import finance_rules_pass, apply_finance_guard
from engine import quant_config
from ui.config_mapper import _config_to_quant_data, _quant_data_to_config


# ── finance_rules_pass：单票判定 ─────────────────────────────

def _fin(**over):
    """标准财务样例：PB=2（10/5）、PE=1000（10*1e10/1e8）、
    市值=1000亿（10*1e10）、EPS=0.01（1e8/1e10）、负债率=40%。"""
    d = {
        'meigujingzichan': 5.0,
        'jinglirun': 1e8,
        'zongguben': 1e10,
        'zongzichan': 1e11,
        'liudongfuzhai': 3e10,
        'changqifuzhai': 1e10,
    }
    d.update(over)
    return d


def test_blowup_negative_net_asset_rejected():
    assert finance_rules_pass(None, 10.0, _fin(meigujingzichan=-1.0)) is False


def test_blowup_negative_profit_rejected():
    assert finance_rules_pass(None, 10.0, _fin(jinglirun=-5e8)) is False


def test_rules_none_keeps_normal_stock():
    assert finance_rules_pass(None, 10.0, _fin()) is True


def test_pb_max():
    assert finance_rules_pass({'pb_max': 1.5}, 10.0, _fin()) is False
    assert finance_rules_pass({'pb_max': 3.0}, 10.0, _fin()) is True


def test_pe_max_rejects_high_pe_and_zero_profit():
    assert finance_rules_pass({'pe_max': 100}, 10.0, _fin()) is False
    assert finance_rules_pass({'pe_max': 2000}, 10.0, _fin()) is True
    # 净利=0（非爆雷但无盈利）：PE 无意义 → 隐含剔除
    assert finance_rules_pass({'pe_max': 2000}, 10.0, _fin(jinglirun=0.0)) is False


def test_mcap_min():
    assert finance_rules_pass({'mcap_min': 500}, 10.0, _fin()) is True
    assert finance_rules_pass({'mcap_min': 2000}, 10.0, _fin()) is False


def test_eps_min():
    assert finance_rules_pass({'eps_min': 0.005}, 10.0, _fin()) is True
    assert finance_rules_pass({'eps_min': 0.05}, 10.0, _fin()) is False


def test_debt_ratio_max():
    assert finance_rules_pass({'debt_ratio_max': 40}, 10.0, _fin()) is True
    assert finance_rules_pass({'debt_ratio_max': 35}, 10.0, _fin()) is False


def test_missing_fields_pass():
    """总股本缺失（0）→ 市值/EPS 规则放行，不误杀。"""
    fin = _fin(zongguben=0.0)
    assert finance_rules_pass({'mcap_min': 500, 'eps_min': 0.05}, 10.0, fin) is True


def test_price_missing_pass():
    assert finance_rules_pass({'pb_max': 1.0, 'mcap_min': 500}, 0.0, _fin()) is True


def test_asset_zero_debt_rule_pass():
    assert finance_rules_pass({'debt_ratio_max': 10}, 10.0, _fin(zongzichan=0.0)) is True


def test_empty_rules_only_blowup():
    """空规则 dict：仅爆雷硬剔除，正常票保留。"""
    assert finance_rules_pass({}, 10.0, _fin(meigujingzichan=-1.0)) is False
    assert finance_rules_pass({}, 10.0, _fin()) is True


# ── apply_finance_guard：TopN 截断 + 不补位 + 失败放行 + 短路 ──

class _FakeTdx:
    by_code = {}

    @staticmethod
    def fetch_finance(code):
        if code in _FakeTdx.by_code:
            return _FakeTdx.by_code[code], None
        return None, 'not found'


def _result(code, price=10.0):
    return {'code': code, 'name': 'n' + code, 'price': price}


def test_guard_drops_blowup_in_topn(monkeypatch):
    monkeypatch.setattr('engine.data_sources.tdx.fetch_finance', _FakeTdx.fetch_finance)
    _FakeTdx.by_code = {'000001': _fin(), '000002': _fin(meigujingzichan=-1.0)}
    results = [_result('000001'), _result('000002'), _result('000003')]
    # topn=2：只查前两只；000002 爆雷剔除；000003 在后段不受影响（不补位）
    out, dropped = apply_finance_guard(results, 2, None)
    assert dropped == 1
    assert [r['code'] for r in out] == ['000001', '000003']


def test_guard_rules_filter_threshold(monkeypatch):
    monkeypatch.setattr('engine.data_sources.tdx.fetch_finance', _FakeTdx.fetch_finance)
    _FakeTdx.by_code = {'000001': _fin(), '000002': _fin(jinglirun=-5e8)}
    results = [_result('000001'), _result('000002')]
    # rules={'pe_max':100}：000001 保留（爆雷无，PE 1000>100 → 剔除）
    out, dropped = apply_finance_guard(results, 5, {'pe_max': 100})
    assert dropped == 2
    assert out == []


def test_guard_query_failure_pass(monkeypatch):
    """全部查询失败 → 安全放行，不误杀整批。"""
    monkeypatch.setattr('engine.data_sources.tdx.fetch_finance', _FakeTdx.fetch_finance)
    _FakeTdx.by_code = {}
    results = [_result('000001'), _result('000002')]
    out, dropped = apply_finance_guard(results, 5, {'pe_max': 100})
    assert dropped == 0
    assert len(out) == 2


def test_guard_rules_none_only_blowup(monkeypatch):
    """rules=None（初级模式现状）：仅爆雷剔除。"""
    monkeypatch.setattr('engine.data_sources.tdx.fetch_finance', _FakeTdx.fetch_finance)
    _FakeTdx.by_code = {'000001': _fin(jinglirun=-1e8)}
    out, dropped = apply_finance_guard([_result('000001')], 5, None)
    assert dropped == 1
    assert out == []


def test_guard_enabled_false_short_circuit(monkeypatch):
    """enabled=False → 不触发 fetch_finance（性能短路）。"""
    calls = []

    def _fetch(code):
        calls.append(code)
        return _fin(meigujingzichan=-1.0), None

    monkeypatch.setattr('engine.data_sources.tdx.fetch_finance', _fetch)
    out, dropped = apply_finance_guard(
        [_result('000001')], 5, {'enabled': False, 'pb_max': 1.0})
    assert dropped == 0
    assert len(out) == 1
    assert calls == []


def test_guard_topn_zero_pass():
    out, dropped = apply_finance_guard([_result('000001')], 0, None)
    assert dropped == 0
    assert len(out) == 1


# ── 配置：种子键 + getter + config_mapper 双向映射 ─────────

def test_blank_seed_contains_scan_finance_filter():
    assert 'scan_finance_filter' in quant_config._build_blank_config()


def test_get_scan_finance_filter_defaults_enabled(monkeypatch):
    """缺键/空 dict → 默认 {'enabled': True}（保持爆雷护栏开启）。"""
    monkeypatch.setattr(quant_config, '_cfg', lambda: {})
    assert quant_config.get_scan_finance_filter() == {'enabled': True}
    monkeypatch.setattr(quant_config, '_cfg', lambda: {'scan_finance_filter': {}})
    assert quant_config.get_scan_finance_filter() == {'enabled': True}


def test_get_scan_finance_filter_passthrough(monkeypatch):
    monkeypatch.setattr(quant_config, '_cfg',
                       lambda: {'scan_finance_filter': {'enabled': False, 'pe_max': 50}})
    assert quant_config.get_scan_finance_filter() == {'enabled': False, 'pe_max': 50}


def test_config_mapper_roundtrip_scan_finance_filter():
    cfg = {'scan_finance_filter': {'enabled': True, 'pb_max': 3.0, 'pe_max': None}}
    qd = _config_to_quant_data(cfg, direction='long')
    assert qd['scanFinanceFilter']['pb_max'] == 3.0
    out = _quant_data_to_config(qd, direction='long')
    assert out['scan_finance_filter'] == {'enabled': True, 'pb_max': 3.0, 'pe_max': None}


def test_config_mapper_default_when_key_absent():
    qd = _config_to_quant_data({}, direction='long')
    assert qd['scanFinanceFilter'] == {'enabled': True}
    out = _quant_data_to_config({}, direction='long')
    assert out['scan_finance_filter'] == {'enabled': True}
