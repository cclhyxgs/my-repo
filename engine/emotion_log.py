#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""情绪化操作记录 · 独立自察觉引擎

定位：与「信号执行纪律画像」（discipline_log）**并列且独立**的自察觉小节。
用户手动登记一笔"情绪化操作"（追高/扛单/手痒/怕踏空/恐慌砍仓等），
工具锁定操作价、随每次刷新按现价实时算「情绪化差」，让用户亲眼看到
情绪让自己"套了多少"或"拿不住少赚了多少"。

核心口径：
  1. 情绪化差 = (现价 - 操作价) / 操作价 × 100%。红绿走平台惯例（红=涨/+，绿=跌/−），
     正负号即方向，不配任何解释性文案——数字本身就是要呈现的"情绪账本"。
  2. 记录一律跟随现价、不做任何"已了结/清仓"状态（比信号复盘更少一个操作）；
     这样才能持续照见"拿不住卖早→少赚"的机会成本，直到用户删除该笔。
  3. 完全独立：不进冷却判定、不混入信号纪律盈亏，只作展示与自省。

设计约束（对齐 discipline_log）：
  全离线本地 JSON 持久化；线程安全；所有字段 .get() 兜底，绝不因脏数据抛异常。
"""

import json
import logging
import os
import uuid
import threading

logger = logging.getLogger(__name__)

_VERSION = 1
_LOCK = threading.RLock()

# 操作码 → 中文标签
ACTION_CN = {
    'buy': '买入', 'add': '加仓', 'reduce': '减仓', 'sell': '卖出', 'clear': '清仓',
}
# 情绪标签（前端按具体「操作」联动出对应标签；此处为全集参考）。
# 注意：不含「扛单」——它是"不操作"，账本只能记录动手的那一下。
EMOTION_TAGS = ('追涨', '博反弹', '怕踏空', '冲动', '摊平', '怕回调', '拿不住', '恐慌', '其他')


class EmotionJournal:
    """情绪化操作记录（独立自察觉引擎）。线程安全。"""

    def __init__(self, config_dir=None):
        self._path = os.path.join(
            config_dir or os.getcwd(), 'emotion_journal.json')
        self._data = self._load()

    # ── 持久化 ────────────────────────────────────────────────
    def _load(self):
        try:
            if os.path.exists(self._path):
                with open(self._path, 'r', encoding='utf-8') as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    d.setdefault('version', _VERSION)
                    d.setdefault('records', [])
                    return d
        except Exception as e:
            logger.warning("读取情绪化操作记录失败(将重建): %s", e)
        return {'version': _VERSION, 'records': []}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = self._path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._path)
        except Exception as e:
            logger.warning("保存情绪化操作记录失败: %s", e)

    # ── 记录 ──────────────────────────────────────────────────
    def add_record(self, *, code, name='', market='stock', action='buy',
                   tag='其他', note='', op_price=0.0, latest_price=None):
        try:
            op = float(op_price or 0)
        except (TypeError, ValueError):
            op = 0.0
        if not code:
            return {'error': '缺少标的代码'}
        try:
            latest = float(latest_price) if latest_price not in (None, '') else op
        except (TypeError, ValueError):
            latest = op
        rec = {
            'id': uuid.uuid4().hex[:12],
            'ts': __import__('datetime').datetime.now().isoformat(timespec='seconds'),
            'code': code, 'name': name or '',
            'market': market, 'action': action,
            'action_cn': ACTION_CN.get(action, action),
            'tag': tag or '其他',
            'note': note or '',
            'op_price': round(op, 4),
            'latest_price': round(latest, 4),
            'latest_at': __import__('datetime').datetime.now().isoformat(timespec='seconds'),
        }
        with _LOCK:
            self._data.setdefault('records', []).insert(0, rec)
            self._save()
        return self._decorate(rec)

    def list_records(self, limit=200):
        out = []
        for r in self._data.get('records', [])[:limit]:
            out.append(self._decorate(r))
        return out

    def delete_record(self, record_id):
        with _LOCK:
            before = len(self._data.get('records', []))
            self._data['records'] = [
                r for r in self._data.get('records', [])
                if r.get('id') != record_id]
            if len(self._data['records']) != before:
                self._save()
                return {'ok': True, 'id': record_id}
            return {'error': '记录不存在', 'id': record_id}

    def update_price_by_code(self, code, latest_price):
        try:
            latest = float(latest_price)
        except (TypeError, ValueError):
            return 0
        if latest <= 0:
            return 0
        n = 0
        with _LOCK:
            for r in self._data.get('records', []):
                if r.get('code') == code and r.get('op_price', 0) > 0:
                    r['latest_price'] = round(latest, 4)
                    r['latest_at'] = __import__('datetime').datetime.now().isoformat(timespec='seconds')
                    n += 1
            if n:
                self._save()
        return n

    # ── 展示 ──────────────────────────────────────────────────
    def _is_emotional_loss(self, r):
        """该笔是否属于「情绪化亏损」——只有亏损方向才计入"最容易犯的错"。

        口径：
          - 买入/加仓（仍在场）：被套 = 现价 < 操作价 → diff < 0
          - 减仓/清仓/卖出（已走）：卖早少赚 = 现价 > 卖出价 → diff > 0
        无价或 diff 未算出的不纳入。
        """
        d = self._decorate(r).get('diff_pct')
        if d is None:
            return False
        act = r.get('action', '')
        if act in ('buy', 'add'):
            return d < 0
        if act in ('reduce', 'clear', 'sell'):
            return d > 0
        return d < 0  # 未知操作默认按"被套"计

    def summary(self):
        recs = self._data.get('records', [])
        total = len(recs)
        sum_diff = 0.0
        diff_ok = 0
        for r in recs:
            d = self._decorate(r).get('diff_pct')
            if d is not None:
                sum_diff += d
                diff_ok += 1
        # 「最容易犯的错」：只统计情绪化亏损的记录（赚的不算"错"）
        tag_count = {}
        loss_n = 0
        for r in recs:
            if not self._is_emotional_loss(r):
                continue
            loss_n += 1
            t = r.get('tag') or '其他'
            tag_count[t] = tag_count.get(t, 0) + 1
        top_tag = max(tag_count, key=tag_count.get) if tag_count else ''
        return {
            'total': total,
            'sum_diff': round(sum_diff, 2),
            'diff_n': diff_ok,
            'loss_n': loss_n,
            'top_tag': top_tag,
            'top_tag_count': tag_count.get(top_tag, 0) if top_tag else 0,
            'tags': tag_count,
        }

    def _decorate(self, r):
        op = float(r.get('op_price') or 0)
        latest = float(r.get('latest_price') or 0)
        diff = None
        if op and op > 0 and latest and latest > 0:
            diff = round((latest - op) / op * 100, 2)
        return {
            'id': r.get('id'),
            'ts': r.get('ts'),
            'code': r.get('code'),
            'name': r.get('name', ''),
            'market': r.get('market', 'stock'),
            'action': r.get('action'),
            'action_cn': r.get('action_cn') or ACTION_CN.get(r.get('action'), r.get('action') or ''),
            'tag': r.get('tag', '其他'),
            'note': r.get('note', ''),
            'op_price': round(op, 3),
            'latest_price': round(latest, 3),
            'latest_at': r.get('latest_at'),
            'diff_pct': diff,
        }


# 全局单例
_JOURNAL = None


def get_emotion_journal(config_dir=None):
    global _JOURNAL
    if _JOURNAL is None:
        _JOURNAL = EmotionJournal(config_dir=config_dir)
    return _JOURNAL


def reset_emotion_journal():
    global _JOURNAL
    _JOURNAL = None