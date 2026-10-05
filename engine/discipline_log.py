#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""执行与复盘闭环 · 情绪账本引擎

定位：
  帮助情绪化散户做「情绪复盘」——看清「按信号做的」和「没按信号做的」事后各赚亏多少，
  差距就是情绪化违背信号的成本；并据此在情绪偏差过大时触发「冷却」软提醒，强制停下来。

核心口径（与"资金盈亏结算"区分开）：
  1. 信号日志：每次触发建仓/加仓/减仓/清仓信号时落一条（触发价 = 信号出现那刻现价）。
  2. 执行回填：用户对每条信号勾「是否执行 + 执行成交价」。
  3. 对照（反推信号 + 复盘自己）：
     - 已执行：用「执行价 → 现价」算照做至今盈亏；
     - 未执行：用「触发价 → 现价」算没做、事后走成这样（体现违背信号的代价 / 或躲过一劫）。
     每条都保留这两个价格供用户自己反推"这个信号当时准不准"——不做命中率统计（避免过度工程）。
  4. 情绪偏差账本：已执行合计 vs 未执行合计 + 情绪代价差。
  5. 冷却（软提醒）：情绪偏差（未执行信号的累计方向盈亏）≤ 阈值时触发，红条警示"冷却期内禁止开仓"。

设计约束：
  全离线本地 JSON 持久化；所有字段 .get() 兜底，绝不因脏数据抛异常。
"""

import json
import logging
import os
import uuid
import threading
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_VERSION = 1
_LOCK = threading.RLock()

# 默认冷却参数（可在程序内配置）
DEFAULT_COOLDOWN = {
    'threshold_pct': -8.0,   # 情绪偏差（未执行累计方向盈亏）达到 -8% 触发冷却
    'duration_days': 3,      # 冷却持续天数
}

# 归因/模式识别/纪律联动的数据门槛（单一来源，前端提示「还差多少才启用」也用这两个值）
#   单桶（某类信号 / 某市场）至少 MIN_SAMPLES 条，才纳入结论；
#   模式识别至少 MIN_BUCKETS 个达标桶，才比较出「舒适区 / 别扭区」。
#   取 3 是「最小可信样本」下限：低于此只展示计数，不硬下判断，避免把小样本噪音当洞察。
#   删市场维度后仅有「信号类型」一个分桶维度，故达标桶门槛降为 1：单一类型的达标样本
#   也足以给出「执行率稳定 / 看不出舒适区」这类诚实结论，避免用户永远拿不到任何结论。
MIN_SAMPLES = 3
MIN_BUCKETS = 1


class DisciplineJournal:
    """情绪账本（信号日志 + 执行对照 + 偏差 + 冷却）。线程安全。"""

    def __init__(self, config_dir=None):
        self._path = os.path.join(
            config_dir or os.getcwd(), 'discipline_journal.json')
        self._data = self._load()

    # ── 持久化 ────────────────────────────────────────────────
    def _load(self):
        try:
            if os.path.exists(self._path):
                with open(self._path, 'r', encoding='utf-8') as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    d.setdefault('version', _VERSION)
                    d.setdefault('cooldown', {})
                    d.setdefault('signals', [])
                    # 自动修复异常冷却状态
                    cd = d.get('cooldown', {})
                    threshold = cd.get('threshold_pct')
                    if isinstance(threshold, (int, float)) and threshold > 0:
                        # 阈值是正值（异常），重置为默认值
                        cd['threshold_pct'] = DEFAULT_COOLDOWN['threshold_pct']
                        cd['active'] = False
                        cd['started_at'] = None
                        cd['until'] = None
                        cd['reason'] = None
                        cd['bias_pct'] = 0.0
                    return d
        except Exception as e:
            logger.warning("读取情绪账本失败(将重建): %s", e)
        return {'version': _VERSION, 'cooldown': {}, 'signals': []}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = self._path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._path)
        except Exception as e:
            logger.warning("保存情绪账本失败: %s", e)

    # ── 信号日志 ──────────────────────────────────────────────
    def append_signal(self, *, code, name='', market='stock', direction='long',
                      period='日K', scheme='', signal_type='buy', signal_label='',
                      price=0.0, factor_score=None, position_ratio=None, reason=''):
        with _LOCK:
            today = datetime.now().strftime('%Y-%m-%d')
            for s in self._data.get('signals', []):
                if (s.get('code') == code and s.get('signal_type') == signal_type
                        and s.get('period') == period
                        and (s.get('ts') or '').startswith(today)):
                    return s  # 同日同股同类型去重（幂等）
            rec = {
                'id': uuid.uuid4().hex[:12],
                'ts': datetime.now().isoformat(timespec='seconds'),
                'code': code, 'name': name,
                'market': market, 'direction': direction,
                'period': period, 'scheme': scheme,
                'signal_type': signal_type, 'signal_label': signal_label,
                'trigger_price': float(price or 0),   # 信号触发时现价
                'factor_score': factor_score,
                'position_ratio': position_ratio,
                'reason': reason or '',
                'execution': None,    # None | {executed: bool, exec_price, at}
                'latest_price': float(price or 0),   # 最近核对价（默认=触发价）
                'latest_at': datetime.now().isoformat(timespec='seconds'),
            }
            self._data.setdefault('signals', []).insert(0, rec)
            self._save()
            return rec

    def set_execution(self, signal_id, executed, exec_price=None, exec_ratio=None):
        """回填「是否执行 + 执行成交价 + 实际执行比例%(相对建议比例，0~100)」。

        exec_ratio：用户实际按建议比例执行的程度；未执行可留空=按0算。
        """
        with _LOCK:
            for s in self._data.get('signals', []):
                if s.get('id') == signal_id:
                    s['execution'] = {
                        'executed': bool(executed),
                        'exec_price': float(exec_price) if exec_price not in (None, '') else None,
                        'exec_ratio': float(exec_ratio) if exec_ratio not in (None, '') else None,
                        'at': datetime.now().isoformat(timespec='seconds'),
                    }
                    self._save()
                    return {'ok': True, 'id': signal_id,
                            'cooldown': self.cooldown_status()}
            return {'error': '信号不存在', 'id': signal_id}

    def record_price(self, signal_id, latest_price):
        try:
            latest = float(latest_price)
        except (TypeError, ValueError):
            return
        with _LOCK:
            for s in self._data.get('signals', []):
                if s.get('id') == signal_id:
                    s['latest_price'] = latest
                    s['latest_at'] = datetime.now().isoformat(timespec='seconds')
                    break
            self._recompute_cooldown()  # 价格变→情绪偏差变→冷却可能触发/解除
            self._save()

    def update_price_by_code(self, code, latest_price):
        """按代码批量刷新该股未了结信号的现价对照（诊断同一股时调用，让账本自动更新）。"""
        try:
            latest = float(latest_price)
        except (TypeError, ValueError):
            return 0
        n = 0
        with _LOCK:
            for s in self._data.get('signals', []):
                if s.get('code') == code:
                    s['latest_price'] = latest
                    s['latest_at'] = datetime.now().isoformat(timespec='seconds')
                    n += 1
            if n:
                self._recompute_cooldown()  # 现价刷新→情绪偏差刷新→冷却同步
                self._save()
        return n

    def delete_signal(self, signal_id):
        with _LOCK:
            before = len(self._data.get('signals', []))
            self._data['signals'] = [
                s for s in self._data.get('signals', [])
                if s.get('id') != signal_id]
            if len(self._data['signals']) != before:
                self._recompute_cooldown()
                self._save()
                return {'ok': True, 'id': signal_id,
                        'cooldown': self.cooldown_status()}
            return {'error': '信号不存在', 'id': signal_id}

    # ── 冷却（基于情绪偏差：未执行信号的累计方向盈亏）────────────
    def get_cooldown_config(self):
        cd = self._data.get('cooldown', {})
        threshold = cd.get('threshold_pct', DEFAULT_COOLDOWN['threshold_pct'])
        # 冷却阈值应该是负值（代表亏损触发冷却），正值不合理，重置为默认
        if isinstance(threshold, (int, float)) and threshold > 0:
            threshold = DEFAULT_COOLDOWN['threshold_pct']
        return {
            'threshold_pct': threshold,
            'duration_days': cd.get('duration_days', DEFAULT_COOLDOWN['duration_days']),
        }

    def set_cooldown_config(self, threshold_pct=None, duration_days=None):
        cd = self._data.setdefault('cooldown', {})
        try:
            if threshold_pct is not None:
                t = float(threshold_pct)
                # 冷却阈值必须是负值（代表亏损触发冷却）
                if t > 0:
                    return {'error': '冷却阈值必须是负值（如 -8）'}
                cd['threshold_pct'] = t
            if duration_days is not None:
                cd['duration_days'] = max(1, int(float(duration_days)))
        except (TypeError, ValueError):
            return {'error': '冷却参数格式错误'}
        self._recompute_cooldown()
        self._save()
        return {'ok': True, **self.get_cooldown_config(),
                'cooldown': self.cooldown_status()}

    def _emotion_bias_pct(self):
        """情绪偏差 = 各信号「情绪化差」合计（含未做足+多执行），口径同 emotion_diff。

        未做足(er<1)按触发价→现价、多执行(er>1)按执行价→现价、方向归一。
        累计偏负 → 偏离建议整体在吃亏 → 触发冷却。
        """
        total = 0.0
        n = 0
        for s in self._data.get('signals', []):
            d = self._decorate(s)
            e = d.get('emotion_diff')
            if e is not None:
                total += e
                n += 1
        return round(total, 2), n

    def _recompute_cooldown(self):
        cd = self._data.setdefault('cooldown', {})
        threshold = float(cd.get('threshold_pct', DEFAULT_COOLDOWN['threshold_pct']))
        duration = max(1, int(cd.get('duration_days', DEFAULT_COOLDOWN['duration_days'])))
        bias, bias_n = self._emotion_bias_pct()
        cd['bias_pct'] = bias

        active = bool(cd.get('active'))
        now = datetime.now()
        if active:
            # 冷却清除条件：到期 / 无信号 / 情绪偏差恢复到阈值以上
            should_clear = False
            until = cd.get('until')
            if until:
                try:
                    if now >= datetime.fromisoformat(until):
                        should_clear = True  # 到期
                except ValueError:
                    should_clear = True
            if bias_n == 0:
                should_clear = True  # 无信号
            elif bias > threshold:
                should_clear = True  # 情绪偏差恢复
            if should_clear:
                cd['active'] = False
                cd['started_at'] = None
                cd['until'] = None
                cd['reason'] = None
                cd['bias_pct'] = 0.0
                active = False
        if not active and bias_n and bias <= threshold:
            cd['active'] = True
            cd['started_at'] = now.isoformat(timespec='seconds')
            cd['until'] = (now + timedelta(days=duration)).isoformat(timespec='seconds')
            cd['reason'] = f"情绪偏差累计 {bias:.1f}% ≤ {threshold:.1f}% 阈值（未执行信号持续吃亏）"
        return self.cooldown_status()

    def cooldown_status(self):
        cd = self._data.get('cooldown', {})
        active = bool(cd.get('active'))
        return {
            'active': active,
            'reason': cd.get('reason'),
            'started_at': cd.get('started_at'),
            'until': cd.get('until'),
            'bias_pct': round(float(cd.get('bias_pct', 0)), 2),
            'threshold_pct': cd.get('threshold_pct', DEFAULT_COOLDOWN['threshold_pct']),
            'duration_days': cd.get('duration_days', DEFAULT_COOLDOWN['duration_days']),
        }

    # ── 账本 / 列表 ───────────────────────────────────────────
    def list_signals(self, limit=100, market=None):
        out = []
        for s in self._data.get('signals', [])[:limit]:
            if market and market != 'all' and s.get('market') != market:
                continue
            out.append(self._decorate(s))
        return out

    def _decorate(self, s):
        """单条信号对照：已执行用「执行价→现价」，未执行用「触发价→现价」，并给一句话结论。"""
        latest = float(s.get('latest_price') or 0)
        direction = s.get('direction', 'long')
        mult = -1 if direction == 'short' else 1  # 空单：下跌为盈利

        def _pct(ref, cur):
            if not ref or ref <= 0:
                return None
            return round((cur - ref) / ref * 100 * mult, 2)

        ex = s.get('execution')
        executed = bool(ex and ex.get('executed'))
        exec_price = (ex.get('exec_price') if ex else None)
        if executed and exec_price and exec_price > 0:
            ref, ref_kind = exec_price, '执行价'
            pct = _pct(ref, latest)
        else:
            ref, ref_kind = s.get('trigger_price') or 0, '触发价'
            pct = _pct(ref, latest)
        # 方向归一（买卖对称）：买入涨=有利、卖出跌=有利。
        # 对卖出向(clear/reduce)信号，价格对该动作持有者的盈亏方向要反转——
        # 否则"卖出后价格下跌(你躲开了)"会被算成负值，与实际"卖对了=赚"相反。
        st = s.get('signal_type', '')
        if pct is not None and st in ('clear', 'reduce'):
            pct = -pct  # 卖出向：下跌=有利=正

        # 触发价→现价、方向归一（有利=正）：未执行/未做足部分的盈亏基数
        trig_pct = _pct(s.get('trigger_price') or 0, latest)
        if trig_pct is not None and st in ('clear', 'reduce'):
            trig_pct = -trig_pct
        # 已执行部分盈亏基数：执行价→现价、方向归一（即当前 pct）
        exec_pct = pct if (executed and exec_price and exec_price > 0) else trig_pct

        conclusion = self._conclusion(s, executed, exec_pct)

        # 情绪化差（比例插值）：
        #   未做足的比例 × 该部分仓位的方向涨跌；符号 负=吃亏 / 正=占便宜。
        #   「实际%」填相对持仓的实际执行比例（与触发信号建议同口径）：
        #   建议减仓30% → 填30=做足100%，填10=实际减掉持仓10%（=建议的1/3）。
        #   做足份额 er = 实际执行 / 建议比例（01）；未做足份额 un = 1 − er。
        suggest = float(s.get('position_ratio') or 0)  # 相对持仓的建议仓位(分数)
        suggest_frac = suggest if suggest > 0 else 1.0   # 无比例按整份 1.0
        er = 0.0
        if ex and ex.get('exec_ratio') not in (None, ''):
            exec_frac = min(float(ex.get('exec_ratio')) / 100, 1.0)   # 相对持仓≤100%（满仓为顶，不能超101）
            er = max(0.0, exec_frac / suggest_frac)                   # 做足份额，可>1=多执行
        elif executed:
            er = 1.0   # 勾执行未填比例 → 默认足额
        un = max(0.0, 1.0 - er)   # 未做足份额（仅少做；多执行无未做足）
        dev = er - 1.0            # 偏离建议：负=少做 / 正=多做（都是情绪化）
        emotion = None
        emo_note = ''
        # 情绪化差 = 比例偏离 + 价格偏离
        #   纪律盈亏 = 建议比例 × trig_pct（按建议比例执行的盈亏）
        #   情绪化盈亏 = 实际比例 × exec_pct（按实际比例执行的盈亏）
        #   比例偏离 = 偏离建议比例的部分：
        #       多做(A>S)：真实成交，按执行价盈亏 exec_pct
        #       少做(A<S)：没成交，机会成本按信号盈亏 trig_pct
        #   价格偏离 = 对应基准比例 × (exec_pct - trig_pct)
        #       多做时基准=建议比例S；少做时基准=实际比例A
        #   恒等式：比例偏离 + 价格偏离 = 情绪化盈亏 - 纪律盈亏 = 情绪化差
        if trig_pct is not None:
            ratio_cost = 0.0  # 比例偏离
            price_cost = 0.0  # 价格偏离
            
            if executed and exec_pct is not None:
                exec_frac_actual = min(float((ex or {}).get('exec_ratio') or 0) / 100, 1.0) if ex and ex.get('exec_ratio') not in (None, '') else suggest_frac
                delta = exec_frac_actual - suggest_frac  # 偏离量：正=多做，负=少做
                if delta >= 0:
                    # 多做（含足量执行）：偏离部分真实成交→按执行价；价差按建议比例
                    ratio_cost = delta * exec_pct
                    price_cost = suggest_frac * (exec_pct - trig_pct)
                else:
                    # 少做：没成交部分成本→按信号；价差按实际比例
                    ratio_cost = delta * trig_pct
                    price_cost = exec_frac_actual * (exec_pct - trig_pct)
                emotion = round(ratio_cost + price_cost, 2)
            else:
                # 未执行：亏掉全部纪律盈亏
                emotion = round(-trig_pct, 2)
                ratio_cost = -trig_pct  # 全部都是比例偏离（完全没做）
            
            if abs(emotion) > 0.005:
                # 生成更直观的注释
                if executed and exec_pct is not None:
                    parts = []
                    if abs(ratio_cost) > 0.005:
                        parts.append('比例%+.2f%%' % ratio_cost)
                    if abs(price_cost) > 0.005:
                        parts.append('价格%+.2f%%' % price_cost)
                    detail = '（' + '，'.join(parts) + '）' if parts else ''
                    emo_note = '情绪化%+.2f%%' % emotion + detail
                else:
                    emo_note = '情绪化%+.2f%%（比例%+.2f%%）' % (emotion, ratio_cost)
            else:
                emotion = 0.0
                emo_note = '纪律执行'
        else:
            emotion = 0.0
            emo_note = '无数据'
        return {
            'id': s.get('id'), 'ts': s.get('ts'),
            'code': s.get('code'), 'name': s.get('name'),
            'market': s.get('market'), 'direction': direction,
            'period': s.get('period'), 'scheme': s.get('scheme'),
            'signal_type': s.get('signal_type', ''), 'signal_label': s.get('signal_label'),
            'signal_type_cn': {'buy': '建仓', 'add': '加仓', 'reduce': '减仓', 'clear': '清仓'}
                              .get(s.get('signal_type', ''), s.get('signal_label') or s.get('signal_type', '')),
            'trigger_price': round(float(s.get('trigger_price') or 0), 2),
            'factor_score': s.get('factor_score'),
            'position_ratio': s.get('position_ratio'),
            'reason': s.get('reason'),
            'execution': ex,
            'latest_price': round(latest, 2),
            'ref_kind': ref_kind,
            'executed': executed,
            'diff_pct': trig_pct,  # 触发至今列＝触发价→现价、方向归一，恒与执行无关，看信号准不准
            'conclusion': conclusion,
            'emotion_diff': emotion,
            'un_pct': round(abs(dev) * 100, 1),
            'emotion_note': emo_note,
            'exec_pct': exec_pct,
            'trig_pct': trig_pct,
            'er': round(er, 4),
            'un': round(un, 4),
            'dev': round(dev, 4),
        }

    @staticmethod
    def _conclusion(s, executed, pct):
        if pct is None:
            return '缺价格数据'
        st = s.get('signal_type', '')
        sign = '+' if pct >= 0 else ''
        if executed:
            head = '已执行'
        else:
            head = '未执行'
        if st in ('buy', 'add'):
            # 做多向信号：现价相对基准越高越有利。
            if executed:
                tail = ('—— 方向跟对，盈利拿到手' if pct >= 0 else '—— 买在了下跌前，这笔在亏')
                ref_word = '买入价'
            else:
                tail = ('—— 踏空，没赚到这笔' if pct >= 0 else '—— 躲过回调，没做反而省了')
                ref_word = '当时'
        elif st in ('clear', 'reduce'):
            # 卖出向信号（多头减仓/清仓）：价格越跌对你卖出越有利。
            if executed:
                # 关键参照：卖出价相对信号触发价卖得高还是低 + 卖后价格走势
                ex_price = float((s.get('execution') or {}).get('exec_price') or 0)
                trg = float(s.get('trigger_price') or 0)
                if ex_price and trg:
                    dv = (ex_price - trg) / trg * 100
                    sig_txt = '你%.2f卖出，比信号价%.2f%s%.1f%%' % (ex_price, trg, ('高' if dv >= 0 else '低'), abs(dv))
                else:
                    sig_txt = ''
                raw = -pct  # 卖出后价格实际涨跌（原始符号：跌=负）；pct 已被反转为"对卖方有利=正"
                if raw < 0:
                    tail = '—— %s；卖出后价格又跌%.1f%%，你躲开了下跌，卖得好（这次是赚）' % (sig_txt, abs(raw))
                else:
                    tail = '—— %s；卖出后价格还涨%.1f%%，卖早了一些' % (sig_txt, raw)
                ref_word = '卖出价'
            else:
                raw = -pct
                tail = ('—— 没卖，信号后价格下跌被你死扛住了' if raw < 0
                        else '—— 没卖，信号后没怎么跌，留着反而对')
                ref_word = '信号价'
        else:
            tail = ''
            ref_word = '当时'
        return f"{head}，若从{ref_word}算起 {sign}{pct:.1f}%{tail}"

    def attribution(self, market=None, min_samples=MIN_SAMPLES):
        """复盘归因（F-1106）：按信号类型聚合「你最不执行哪类信号」。

        纯读现有信号（signal_type + execution + 现价），不新增任何采集。口径：
          - 执行率 = 该类已执行条数 / 该类信号条数
          - 情绪化差合计 = 该类各条 emotion_diff 之和（口径同 ledger_summary）
          - 纪律盈亏合计 = 按建议比例加权的 trig_pct 之和

        诚实收窄：某类信号条数 < min_samples 时只展示计数，不参与「最容易不执行」
        结论——小样本给用户看到的不是洞察而是噪音。
        """
        cn = {'buy': '建仓', 'add': '加仓', 'reduce': '减仓', 'clear': '清仓'}
        order = ('buy', 'add', 'reduce', 'clear')
        sigs = self._data.get('signals', [])
        if market and market != 'all':
            sigs = [s for s in sigs if s.get('market') == market]
        buckets = {k: {'signal_type': k, 'signal_type_cn': cn[k], 'count': 0,
                       'executed': 0, 'not_executed': 0, 'emotion_sum': 0.0,
                       'emotion_n': 0, 'disciple_sum': 0.0} for k in order}
        for s in sigs:
            b = buckets.get(s.get('signal_type', ''))
            if b is None:
                continue
            d = self._decorate(s)
            b['count'] += 1
            ex = s.get('execution')
            if ex and ex.get('executed'):
                b['executed'] += 1
            else:
                b['not_executed'] += 1
            e = d.get('emotion_diff')
            if e is not None:
                b['emotion_sum'] += e
                b['emotion_n'] += 1
            trig = d.get('trig_pct')
            if trig is not None:
                suggest = float(s.get('position_ratio') or 0)
                b['disciple_sum'] += (suggest if suggest > 0 else 1.0) * trig

        rows = []
        for k in order:
            b = buckets[k]
            if not b['count']:
                continue
            b['exec_rate'] = round(b['executed'] / b['count'] * 100, 1)
            b['emotion_sum'] = round(b['emotion_sum'], 2)
            b['disciple_sum'] = round(b['disciple_sum'], 2)
            b['reliable'] = b['count'] >= min_samples
            rows.append(b)

        reliable = [r for r in rows if r['reliable']]
        worst = min(reliable, key=lambda r: (r['exec_rate'], -r['count'])) if reliable else None
        cost_cand = [r for r in reliable if r['emotion_n'] > 0]
        costliest = min(cost_cand, key=lambda r: r['emotion_sum']) if cost_cand else None
        return {
            'by_type': rows,
            'worst_exec': worst,
            'costliest': costliest,
            'headline': self._attribution_headline(rows, worst, costliest, min_samples),
            'min_samples': min_samples,
            'total': len(sigs),
        }

    @staticmethod
    def _attribution_headline(rows, worst, costliest, min_samples):
        """一句话归因结论（诚实：样本不足时明说，不硬下判断）。"""
        if not rows:
            return '还没有信号记录——先做一次个股分析，系统才会开始记录信号。'
        if worst is None:
            return ('样本还不够下结论（每类信号至少 %d 条）。再攒一些，才能看出你'
                    '最容易漏掉哪一类。' % min_samples)
        w = worst
        if w['not_executed'] == 0:
            base = '『%s』信号你每次都执行了，这一类没掉过链子。' % w['signal_type_cn']
        elif w['executed'] == 0:
            base = '『%s』信号你 %d 次一次都没执行——这是你最不听话的一类。' % (
                w['signal_type_cn'], w['count'])
        else:
            base = '『%s』信号你最容易不执行：%d 次里只执行了 %d 次（执行率 %.0f%%）。' % (
                w['signal_type_cn'], w['count'], w['executed'], w['exec_rate'])
        if costliest and costliest['emotion_sum'] < 0:
            if w['signal_type'] == costliest['signal_type']:
                base += ' 它累计的情绪化差 %.2f%%，也是亏得最多的一类。' % costliest['emotion_sum']
            else:
                base += ' 其中『%s』情绪化差累计 %.2f%%，是亏得最多的一类。' % (
                    costliest['signal_type_cn'], costliest['emotion_sum'])
        return base

    def pattern(self, market=None, min_samples=MIN_SAMPLES, min_buckets=MIN_BUCKETS):
        """模式识别（F-1107）：找「舒适区」——在哪种信号类型下你最守纪律。

        维度：信号类型（建仓/加仓/减仓/清仓）。每个分桶统计执行率，仅保留样本 >=
        min_samples 的桶；达到 min_buckets 个可靠桶才给「舒适区 / 别扭区」结论，
        否则返回还差多少条（ready=False + headline 里带提示），避免小样本硬下判断。
        """
        cn = {'buy': '建仓', 'add': '加仓', 'reduce': '减仓', 'clear': '清仓'}
        sigs = self._data.get('signals', [])
        if market and market != 'all':
            sigs = [s for s in sigs if s.get('market') == market]
        groups = {}
        for s in sigs:
            st = s.get('signal_type') or ''
            if st:
                groups.setdefault(('signal_type', st), []).append(s)

        buckets = []
        for (dim, key), items in groups.items():
            n = len(items)
            executed = sum(1 for x in items if (x.get('execution') or {}).get('executed'))
            label = cn.get(key, key)
            buckets.append({'dimension': dim, 'key': key, 'label': label, 'count': n,
                            'executed': executed, 'exec_rate': round(executed / n * 100, 1),
                            'reliable': n >= min_samples})
        dim_order = {'signal_type': 0}
        buckets.sort(key=lambda b: (dim_order.get(b['dimension'], 9), -b['exec_rate']))

        reliable = [b for b in buckets if b['reliable']]
        ready = len(reliable) >= min_buckets
        comfort = max(reliable, key=lambda b: (b['exec_rate'], b['count'])) if ready else None
        strain = min(reliable, key=lambda b: (b['exec_rate'], -b['count'])) if ready else None
        return {
            'buckets': buckets,
            'comfort': comfort,
            'strain': strain,
            'ready': ready,
            'reliable_n': len(reliable),
            'min_samples': min_samples,
            'min_buckets': min_buckets,
            'total': len(sigs),
            'headline': self._pattern_headline(buckets, reliable, comfort, strain,
                                               min_samples, min_buckets),
        }

    @staticmethod
    def _pattern_headline(buckets, reliable, comfort, strain, min_samples, min_buckets):
        """一句话舒适区结论（数据不足时明说还差多少条）。"""
        if not buckets:
            return '还没有信号记录——先做一次个股分析，系统才会开始记录信号。'
        if len(reliable) < min_buckets:
            near = None
            for b in sorted(buckets, key=lambda x: min_samples - x['count']):
                if b['count'] < min_samples:
                    near = b
                    break
            tip = ('最接近的是『%s』，还差 %d 条。' % (near['label'], min_samples - near['count'])
                   if near else '')
            return ('数据还不够识别舒适区：需要至少 %d 类达标（每类 ≥%d 条），当前 %d 类。%s' % (
                min_buckets, min_samples, len(reliable), tip))
        c, s = comfort, strain
        if c['exec_rate'] == s['exec_rate']:
            return ('你的执行率整体稳定在 %.0f%%（%d 条）——各类条件差别不大，暂时看不出舒适区。' % (
                c['exec_rate'], c['count']))
        return ('你的舒适区是『%s』：执行率 %.0f%%（%d 条）；最别扭的是『%s』：执行率 %.0f%%（%d 条）。' % (
            c['label'], c['exec_rate'], c['count'], s['label'], s['exec_rate'], s['count']))

    def linkage(self, market=None, min_samples=MIN_SAMPLES):
        """纪律联动（F-1108）：把归因结论转成执行干预。

        盯防对象 = 归因里「最不执行」且样本达标的信号类型。启用后，该类未执行信号
        由前端高亮提示；样本不足时给出「还差多少条才启用」的提示。只做提示层干预，
        不触碰加减仓 / 风控等交易逻辑。
        """
        att = self.attribution(market, min_samples)
        watch = att['worst_exec']
        sigs = self._data.get('signals', [])
        if market and market != 'all':
            sigs = [s for s in sigs if s.get('market') == market]

        pending = []
        if watch and watch['not_executed'] > 0:
            for s in sigs:
                if s.get('signal_type') != watch['signal_type']:
                    continue
                if (s.get('execution') or {}).get('executed'):
                    continue
                d = self._decorate(s)
                pending.append({
                    'id': s.get('id'), 'code': s.get('code'), 'name': s.get('name'),
                    'ts': s.get('ts'), 'trigger_price': s.get('trigger_price'),
                    'latest_price': s.get('latest_price'),
                    'diff_pct': d.get('diff_pct'), 'emotion_diff': d.get('emotion_diff'),
                })
        enabled = bool(watch) and watch['not_executed'] > 0
        return {
            'enabled': enabled,
            'watch': watch,
            'pending': pending,
            'min_samples': min_samples,
            'hint': self._linkage_hint(att, min_samples),
        }

    @staticmethod
    def _linkage_hint(att, min_samples):
        """未启用时告诉用户「还差多少条」；启用时为空。"""
        rows = att.get('by_type') or []
        if not rows:
            return '还没有信号记录，同类信号攒够 %d 条即可启用盯防。' % min_samples
        worst = att.get('worst_exec')
        if worst is None:
            near = min(rows, key=lambda r: min_samples - r['count'])
            return '还需 %d 条『%s』信号才能启用盯防（当前 %d/%d）。' % (
                max(0, min_samples - near['count']), near['signal_type_cn'],
                near['count'], min_samples)
        if worst['not_executed'] == 0:
            return '盯防对象『%s』目前全部已执行，暂无需干预。' % worst['signal_type_cn']
        return ''

    def ledger_summary(self, market=None):
        sigs = self._data.get('signals', [])
        if market and market != 'all':
            sigs = [s for s in sigs if s.get('market') == market]
        disc_total = 0.0
        emotion_total = 0.0
        disc_n = 0
        for s in sigs:
            ex = s.get('execution')
            executed = bool(ex and ex.get('executed'))
            d = self._decorate(s)
            trig_pct_val = d.get('trig_pct')
            # 纪律盈亏：按建议比例执行的盈亏
            suggest = float(s.get('position_ratio') or 0)
            suggest_frac = suggest if suggest > 0 else 1.0
            if trig_pct_val is not None:
                disc_total += suggest_frac * trig_pct_val  # 按建议比例加权
            e = d.get('emotion_diff')
            if e is not None:
                emotion_total += e
            if executed:
                disc_n += 1
        disc_total = round(disc_total, 2)
        emotion_total = round(emotion_total, 2)

        return {
            'total_signals': len(sigs),
            'disciple_count': disc_n,
            'not_executed_count': len(sigs) - disc_n,
            'disciple_pct_sum': disc_total,
            'emotion_pct_sum': emotion_total,
            'emotion_pct_n': sum(1 for s in sigs if self._decorate(s).get('emotion_diff') is not None),
            'bias_pct': round(emotion_total, 2),
            'cooldown': self.cooldown_status(),
            'config': self.get_cooldown_config(),
        }


# 全局单例
_JOURNAL = None


def get_journal(config_dir=None):
    global _JOURNAL
    if _JOURNAL is None:
        _JOURNAL = DisciplineJournal(config_dir=config_dir)
    return _JOURNAL


def reset_journal():
    global _JOURNAL
    _JOURNAL = None