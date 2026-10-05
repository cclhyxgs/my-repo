# -*- coding: utf-8 -*-
"""回归：回测净值曲线的「实时输出」链路（引擎 → 进度通道 → 前端增量绘制）。

背景（2026-09-19）：净值原本只 append 到子进程内存 `equity_curve`，回测结束才
`to_csv`，中途无任何出口，于是 UI 只能等 done 后才画曲线。现在改为：
  `_record_equity` 按节流（每 5 个交易日 1 点 + 首点）经 `equity_cb` 发出净值点 →
  `ProgressWriter.equity()` 落成 type='equity' 的进度事件 → `get_backtest_progress`
  归集为 `equity_delta`（不进 logs）→ 前端 `appendLiveEquity` 增量补点。

⛔ 该通道仅供过程展示：终态曲线仍以 equity.csv 为准，前端 done 时全量替换。
"""
import json
import os
import tempfile
import unittest

from engine.backtest_protocol import ProgressWriter
from engine.backtest_strategy import StrategyBacktester


# ============================================================
# 1. ProgressWriter.equity：结构化事件
# ============================================================
class TestProgressWriterEquityEvent(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='bt_live_')
        self.path = os.path.join(self.tmp, 'bt_x.progress.jsonl')

    def _lines(self):
        with open(self.path, encoding='utf-8') as f:
            return [json.loads(x) for x in f if x.strip()]

    def test_equity_event_shape(self):
        w = ProgressWriter(self.path)
        w.equity({'d': '2026-01-05', 'nav': 1000000.0, 'dd': 0.0, 'dr': 0.0, 'pos': 0})
        ev = self._lines()[0]
        self.assertEqual(ev['type'], 'equity')
        self.assertIsNone(ev['line'])
        self.assertIsNone(ev['message'])
        self.assertEqual(ev['point']['d'], '2026-01-05')
        self.assertEqual(ev['point']['nav'], 1000000.0)

    def test_equity_and_log_are_distinguishable(self):
        """log 事件不带 point，equity 事件不带 line —— 后端据此分流。"""
        w = ProgressWriter(self.path)
        w.log('[进度] 1/243 交易日')
        w.equity({'d': '2026-01-05', 'nav': 1000000.0, 'dd': 0, 'dr': 0, 'pos': 0})
        evs = self._lines()
        self.assertEqual(evs[0]['type'], 'log')
        self.assertIsNone(evs[0].get('point'))
        self.assertEqual(evs[1]['type'], 'equity')
        self.assertIsNone(evs[1]['line'])

    def test_collect_delta_excludes_logs(self):
        """模拟 get_backtest_progress 的归集：equity 进 delta、log 进 logs。"""
        w = ProgressWriter(self.path)
        for i in range(3):
            w.log(f'日志{i}')
            w.equity({'d': f'2026-01-0{i+1}', 'nav': 1000000 + i, 'dd': 0, 'dr': 0, 'pos': i})
        logs, delta = [], []
        for ev in self._lines():
            if ev.get('type') == 'equity':
                pt = ev.get('point')
                if isinstance(pt, dict) and pt.get('d'):
                    delta.append(pt)
                continue
            if ev.get('line'):
                logs.append(ev['line'])
        self.assertEqual(len(logs), 3)
        self.assertEqual([p['d'] for p in delta], ['2026-01-01', '2026-01-02', '2026-01-03'])


# ============================================================
# 2. _record_equity：节流发出净值点
# ============================================================
class TestRecordEquityEmitsLivePoints(unittest.TestCase):

    def _make_bt(self, cb, cash=1000000.0):
        """绕过 __init__（需读 config），只装配 _record_equity 依赖的属性。

        注意 `nav` 是只读 property（= cash + 持仓市值），positions 为空时 nav == cash，
        所以这里通过改 cash 来驱动净值。
        """
        bt = object.__new__(StrategyBacktester)
        bt.cash = cash
        bt.peak_nav = cash
        bt._current_date = None
        bt.equity_curve = []
        bt.positions = []
        bt.equity_cb = cb
        return bt

    def test_no_cb_means_no_emission_and_no_crash(self):
        bt = self._make_bt(None)
        for d in range(1, 8):
            bt.cash = 1000000.0 + d * 1000
            bt._record_equity(f'2026-01-{d:02d}')
        self.assertEqual(len(bt.equity_curve), 7)   # 曲线照常累积

    def test_throttle_first_and_every_fifth(self):
        pts = []
        bt = self._make_bt(pts.append)
        for d in range(1, 11):
            bt.cash = 1000000.0 + d * 1000
            bt._record_equity(f'2026-01-{d:02d}')
        # 第 1 / 5 / 10 个交易日发送，共 3 点
        self.assertEqual(len(pts), 3)
        self.assertEqual([p['d'] for p in pts], ['2026-01-01', '2026-01-05', '2026-01-10'])
        for p in pts:
            self.assertIn('nav', p)
            self.assertIn('dd', p)
            self.assertIn('dr', p)
            self.assertIn('pos', p)

    def test_point_nav_matches_equity_curve(self):
        pts = []
        bt = self._make_bt(pts.append, cash=1012345.67)
        bt._record_equity('2026-01-05')
        self.assertEqual(pts[0]['nav'], bt.equity_curve[0]['nav'])
        self.assertEqual(pts[0]['nav'], 1012345.67)

    def test_cb_exception_does_not_break_backtest(self):
        def boom(_p):
            raise RuntimeError('cb 坏了')
        bt = self._make_bt(boom)
        bt._record_equity('2026-01-05')          # 不应抛出
        self.assertEqual(len(bt.equity_curve), 1)

    def test_equity_pts_counter_is_per_instance(self):
        bt = self._make_bt(None)
        bt._record_equity('2026-01-05')
        self.assertEqual(bt._equity_pts, 1)


# ============================================================
# 3. 链路守卫：签名与前端接线（防后续改动误删）
# ============================================================
class TestLiveEquityWiring(unittest.TestCase):

    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _src(self, rel):
        with open(os.path.join(self._ROOT, rel), encoding='utf-8') as f:
            return f.read()

    def test_run_strategy_accepts_equity_cb(self):
        import inspect
        from engine.backtest_runner import run_strategy
        params = inspect.signature(run_strategy).parameters
        self.assertIn('equity_cb', params)
        self.assertIsNone(params['equity_cb'].default)
        self.assertIn('bt.equity_cb = equity_cb', self._src('engine/backtest_runner.py'))

    def test_cli_passes_progress_writer_equity(self):
        self.assertIn('equity_cb=progress_writer.equity', self._src('engine/backtest_cli.py'))

    def test_web_api_returns_equity_delta(self):
        src = self._src('ui/web_api.py')
        self.assertIn("'equity_delta': eq_delta", src)
        self.assertIn("if _t == 'equity':", src)

    def test_frontend_has_live_chart_hooks(self):
        src = self._src('ui_mockup/index.html')
        self.assertIn('function startLiveEquityChart()', src)
        self.assertIn('function appendLiveEquity(delta)', src)
        self.assertIn('appendLiveEquity(r.equity_delta)', src)
        self.assertIn('startLiveEquityChart();', src)

    def test_frontend_does_not_early_return_on_backtest_error(self):
        """回测自身异常的返回体带 done 键，不能被当成接口级错误提前 return。"""
        src = self._src('ui_mockup/index.html')
        self.assertIn("if(r.error && r.done === undefined)", src)


# ============================================================
# 4. 回测中切页/切 Tab：允许自由切换 + 图表 resize 兜底
# ============================================================
class TestSwitchViewDuringBacktest(unittest.TestCase):
    """2026-09-19 用户决策：回测中允许自由切换页面。

    背景：`switchView` 原有「回测中禁止切页」拦截，但判的是 `window.btTaskId`，
    而 `btTaskId` 是回测模块**闭包内的 let**（`window` 上取不到）⇒ 恒为 undefined、
    从未生效。现按决策移除拦截，并把同类失效引用（`window.charts`）一并修正。
    """

    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def setUp(self):
        with open(os.path.join(self._ROOT, 'ui_mockup/index.html'), encoding='utf-8') as f:
            self.src = f.read()

    def _code_lines(self):
        """剔除以 // 开头的纯注释行，避免注释里的说明文字干扰断言。"""
        for ln in self.src.split('\n'):
            if ln.strip().startswith('//'):
                continue
            yield ln

    def test_blocking_guard_removed(self):
        self.assertNotIn("showToast('回测正在运行中，请先停止回测或等待完成后再切换页面')", self.src)

    def test_resize_helper_defined_and_wired(self):
        self.assertIn('function resizeBtCharts()', self.src)
        # switchView('quant') 与 switchSub('q-backtest') 两条入口都要调
        self.assertIn('setTimeout(()=>{ fillQuantGrids(); resizeBtCharts(); },50)', self.src)
        self.assertIn("if(paneId === 'q-backtest') setTimeout(resizeBtCharts, 60);", self.src)

    def test_running_flag_synced_for_beforeunload(self):
        self.assertIn('window.__btRunning = true;', self.src)
        self.assertIn('window.__btRunning = false;', self.src)
        self.assertIn('if(window.__btRunning){', self.src)

    def test_no_stale_window_refs_in_code(self):
        """`charts` / `btTaskId` 都不是 window 属性，代码里不得出现 window.xxx 引用。"""
        code = '\n'.join(self._code_lines())
        self.assertNotIn('window.charts', code)
        self.assertNotIn('window.btTaskId', code)


if __name__ == '__main__':
    unittest.main()
