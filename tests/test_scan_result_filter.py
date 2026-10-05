# -*- coding: utf-8 -*-
"""全市场扫描「结果筛选」回归测试：星级比较符（≥/=/≤）与涨幅筛选。

背景（2026-10-05 用户实报）：
  ① 「评级」下拉只有累积口径（选3星会带出4、5星），无法只看恰好3星；
  ② 结果筛选没有涨幅条件，无法排除涨停股（涨幅 < 9.8）。
本文件用轻量 fake 直接驱动 `WebAPI.get_scan_progress` 的裁剪链路，覆盖两条筛选。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ui.web_api import WebAPI


class _ScanApi:
    """只提供 get_scan_progress 依赖的最小接口（避开 WebAPI.__init__ 的重依赖）。"""

    def __init__(self):
        self._scan_tasks = {}

    def _is_basic_mode(self):
        return False

    def _scan_rank(self, r):
        return r.get('final_score') or 0

    def _ensure_scan_tech_pass(self, task, results, is_futures=False):
        return list(results), False

    def _aggregate_sectors(self, results):
        return []

    def _get_cache_hit_count(self):
        return 0

    def _scan_cmp(self, op, a, b):
        return WebAPI._scan_cmp(op, a, b)

    @staticmethod
    def _cache_status_from_time(scan_time_str):
        return WebAPI._cache_status_from_time(scan_time_str)


def _make_api(rows, **task_overrides):
    api = _ScanApi()
    task = {
        'results': list(rows),
        'is_running': False,
        'total': len(rows),
        'progress': 100,
        'success_count': len(rows),
        'status': 'completed',
        'min_score': -999999,
        'max_score': None,
        'topn': 0,
        'rating': '全部',
        'rating_op': 'ge',
        'chg_op': None,
        'chg_val': None,
    }
    task.update(task_overrides)
    api._scan_tasks['t1'] = task
    return api


def _row(code, stars, change_pct, score=50.0):
    return {
        'code': code, 'name': code, 'stars': stars,
        'change_pct': change_pct, 'final_score': score, 'tech_strength': score / 100.0,
    }


_ROWS = [
    _row('s5', '⭐' * 5, 1.0),
    _row('s4', '⭐' * 4, 2.0),
    _row('s3', '⭐' * 3, 3.0),
    _row('s2', '⭐' * 2, 4.0),
    _row('s1', '⭐' * 1, 5.0),
]


def _codes(resp):
    return [r['code'] for r in resp['results']]


def test_star_exact_match_only_returns_that_star():
    """=3星：只保留恰好 3 星（旧口径 ≥3 会带出 4、5 星）。"""
    api = _make_api(_ROWS, rating='3星', rating_op='eq')
    resp = WebAPI.get_scan_progress(api, 't1', rating='3星', rating_op='eq')
    assert _codes(resp) == ['s3']
    assert resp['matched_count'] == 1


def test_star_ge_keeps_cumulative_semantics():
    """默认 ≥3星：3、4、5 星都保留（不破坏旧行为）。"""
    api = _make_api(_ROWS, rating='3星', rating_op='ge')
    resp = WebAPI.get_scan_progress(api, 't1', rating='3星', rating_op='ge')
    assert set(_codes(resp)) == {'s3', 's4', 's5'}


def test_star_le_keeps_low_ratings():
    """≤3星：只保留 3 星及以下。"""
    api = _make_api(_ROWS, rating='3星', rating_op='le')
    resp = WebAPI.get_scan_progress(api, 't1', rating='3星', rating_op='le')
    assert set(_codes(resp)) == {'s1', 's2', 's3'}


def test_star_all_ignores_operator():
    """rating='全部' 时比较符不生效，全量保留。"""
    api = _make_api(_ROWS, rating='全部', rating_op='eq')
    resp = WebAPI.get_scan_progress(api, 't1', rating='全部', rating_op='eq')
    assert len(resp['results']) == 5


def test_change_pct_lt_excludes_limit_up():
    """涨幅 < 9.8：排除涨停股（10%）。"""
    rows = [_row('normal', '⭐' * 3, 3.2), _row('limitup', '⭐' * 3, 10.0)]
    api = _make_api(rows)
    resp = WebAPI.get_scan_progress(api, 't1', chg_op='lt', chg_val=9.8)
    assert _codes(resp) == ['normal']


def test_change_pct_gt_keeps_high_change():
    """涨幅 > 5：只保留涨幅大于 5% 的。"""
    rows = [_row('a', '⭐' * 3, 3.0), _row('b', '⭐' * 3, 7.0)]
    api = _make_api(rows)
    resp = WebAPI.get_scan_progress(api, 't1', chg_op='gt', chg_val=5)
    assert _codes(resp) == ['b']


def test_change_pct_missing_is_excluded_when_filter_active():
    """change_pct 缺失（旧缓存无该字段）视为不可比较 → 排除，不误放行。"""
    rows = [_row('has', '⭐' * 3, 1.0), _row('none', '⭐' * 3, None)]
    api = _make_api(rows)
    resp = WebAPI.get_scan_progress(api, 't1', chg_op='lt', chg_val=9.8)
    assert _codes(resp) == ['has']


def test_change_pct_cleared_by_empty_value():
    """chg_val='' 清除涨幅条件 → 全量恢复。"""
    rows = [_row('has', '⭐' * 3, 1.0), _row('none', '⭐' * 3, None)]
    api = _make_api(rows, chg_op='lt', chg_val=9.8)
    resp = WebAPI.get_scan_progress(api, 't1', chg_op='lt', chg_val='')
    assert len(resp['results']) == 2


if __name__ == '__main__':
    import unittest
    unittest.main()
