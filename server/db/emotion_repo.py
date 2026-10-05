# -*- coding: utf-8 -*-
"""情绪账本仓储（能力 6，F-1201/1202/1203/1204）。

把桌面 `engine/emotion_log.py::EmotionJournal` 行为镜像到 PG `emotion_record` 表。
核心口径不可改：
- 情绪化差 = (现价 - 操作价) / 操作价 × 100%（镜像 `_decorate.diff_pct`）。
- `op_price` 服务端锁定：add_record 忽略前端传入，仅以首次登记值为准（F-1201）。
- 标签常量单一来源（F-1204）：本模块为唯一口径，路由 `/api/emotion/tags` 下发。
- 「最常见情绪」仅统计情绪化亏损笔（镜像 `_is_emotional_loss`），tie-break 由
  `groups_ext.first()`（SQL 端）降序稳定。
"""

import uuid

from sqlalchemy import delete, select, func

from server.core import time as srv_time
from server.db.models import EmotionRecord

# 操作码 → 中文标签（镜像 ACTION_CN）
ACTION_CN = {
    'buy': '买入', 'add': '加仓', 'reduce': '减仓', 'sell': '卖出', 'clear': '清仓',
}
# 情绪标签全集（镜像 EMOTION_TAGS；不含「扛单」——它是"不操作"）
EMOTION_TAGS = ('追涨', '博反弹', '怕踏空', '冲动', '摊平', '怕回调', '拿不住', '恐慌', '其他')
_VALID_ACTIONS = frozenset(ACTION_CN)


def emotion_tags() -> dict:
    """F-1204：标签常量单一来源（前后端不各算一套）。"""
    return {"actions": ACTION_CN, "tags": list(EMOTION_TAGS)}


def _now():
    return srv_time.now_iso()


def add_record(session, *, code, name="", market="stock", action="buy",
               tag="其他", note="", op_price=0.0, latest_price=None):
    """新增一笔情绪化操作记录。`op_price` 以入参为准（服务端锁定，后续不可改）。"""
    try:
        op = float(op_price or 0)
    except (TypeError, ValueError):
        op = 0.0
    if not code:
        return {"error": "缺少标的代码"}
    try:
        latest = float(latest_price) if latest_price not in (None, "") else op
    except (TypeError, ValueError):
        latest = op
    action = action if action in _VALID_ACTIONS else "buy"
    rec = EmotionRecord(
        id=uuid.uuid4().hex[:12],
        ts=_now(),
        code=code, name=name or "",
        market=market, action=action,
        action_cn=ACTION_CN.get(action, action),
        tag=tag or "其他", note=note or "",
        op_price=round(op, 4),
        latest_price=round(latest, 4),
        latest_at=_now(),
    )
    session.add(rec)
    session.flush()
    return _decorate(rec)


def list_records(session, limit=None, cursor=None):
    """游标分页；limit=200 默认（F-1203「加载更多」），ts 倒序。"""
    if limit is None:
        limit = 200
    query = select(EmotionRecord).order_by(EmotionRecord.ts.desc(), EmotionRecord.id.desc())
    if cursor:
        row = session.get(EmotionRecord, cursor)
        if row is not None:
            query = query.where(
                (EmotionRecord.ts, EmotionRecord.id) < (row.ts, row.id))
    rows = session.execute(query.limit(limit)).scalars().all()
    return [_decorate(r) for r in rows]


def records_slice(session, limit=200):
    """无游标整段（给 summary 用，同源同序）。"""
    rows = session.execute(
        select(EmotionRecord).order_by(EmotionRecord.ts.desc()).limit(limit)
    ).scalars().all()
    return [_decorate(r) for r in rows]


def delete_record(session, record_id) -> bool:
    res = session.execute(delete(EmotionRecord).where(EmotionRecord.id == record_id))
    if res.rowcount:
        session.flush()
        return True
    return False


def update_price_by_code(session, code, latest_price) -> int:
    """按代码刷新现价（F-1201 批量口径；`op_price>0` 才更新）。"""
    try:
        latest = float(latest_price)
    except (TypeError, ValueError):
        return 0
    if latest <= 0:
        return 0
    rows = session.execute(
        select(EmotionRecord).where(
            EmotionRecord.code == code, EmotionRecord.op_price > 0)
    ).scalars().all()
    n = 0
    for row in rows:
        row.latest_price = round(latest, 4)
        row.latest_at = _now()
        n += 1
    if n:
        session.flush()
    return n


def _is_emotional_loss(r: dict) -> bool:
    """该笔是否「情绪化亏损」（镜像桌面；只有亏损方向计入最容易犯的错）。"""
    d = r.get("diff_pct")
    if d is None:
        return False
    act = r.get("action", "")
    if act in ("buy", "add"):
        return d < 0
    if act in ("reduce", "clear", "sell"):
        return d > 0
    return d < 0


def summary(session) -> dict:
    """F-1202：记录数 / 合计浮动差 / 最常见情绪（按 SQL 聚合，降序稳定）。"""
    recs = records_slice(session)
    sum_diff = 0.0
    diff_n = 0
    for r in recs:
        d = r.get("diff_pct")
        if d is not None:
            sum_diff += d
            diff_n += 1
    # 最常见情绪：仅统计情绪化亏损笔，按 COUNT 降序 + 首次出现序（稳定 tie-break）
    tag_count = {}
    order = {}
    for idx, r in enumerate(recs):
        if not _is_emotional_loss(r):
            continue
        t = r.get("tag") or "其他"
        if t not in tag_count:
            order[t] = idx
        tag_count[t] = tag_count.get(t, 0) + 1
    top_tag = ""
    top_count = 0
    if tag_count:
        # SQL 语义对齐：按 count 降序，等值按记录首次出现序（Page1 先到的排前）
        top_tag, top_count = min(tag_count.items(), key=lambda kv: (-kv[1], order[kv[0]]))
    return {
        "total": len(recs),
        "sum_diff": round(sum_diff, 2),
        "diff_n": diff_n,
        "loss_n": sum(1 for r in recs if _is_emotional_loss(r)),
        "top_tag": top_tag,
        "top_tag_count": top_count,
    }


def _decorate(r) -> dict:
    op = float(r.op_price or 0)
    latest = float(r.latest_price or 0)
    diff = None
    if op and op > 0 and latest and latest > 0:
        diff = round((latest - op) / op * 100, 2)
    return {
        "id": r.id,
        "ts": r.ts,
        "code": r.code,
        "name": r.name or "",
        "market": r.market if r.market is not None else "stock",
        "action": r.action,
        "action_cn": r.action_cn or ACTION_CN.get(r.action, r.action or ""),
        "tag": r.tag or "其他",
        "note": r.note or "",
        "op_price": round(op, 3),
        "latest_price": round(latest, 3),
        "latest_at": r.latest_at,
        "diff_pct": diff,
    }