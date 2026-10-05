# -*- coding: utf-8 -*-
"""纪律账本仓储（能力 6，F-1101/1103/1104/1105）。

把桌面 `engine/discipline_log.py::DisciplineJournal` 的行为**逐字镜像**到 PG
`signal` + `cooldown` 两表（app-architecture.md §4.1 能力 6）。核心口径不可改：
- 情绪化差（`emotion_diff`）：未执行按「触发价→现价」、未做足/多执行按比例插值，
  方向归一（买入向涨=正、卖出向跌=正）；`clear/reduce` 反向。
- 冷却：基于「未执行信号累计方向盈亏」（`bias_pct`）≤ 阈值触发，全局跨市场。
- 时间列统一 `server.core.time.now_iso()`（R-19 禁裸 datetime.now）。

落册判定（F-1103：结构化 signal_type 枚举、冷却激活不入新信号）归
`server/adapters/ledger.py`（接线层），本仓只负责增删查与展示计算。
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select

from server.core import time as srv_time
from server.core.time import TZ
from server.db.models import Cooldown, Signal

# 默认冷却参数（与桌面 DEFAULT_COOLDOWN 一致；F-1103：threshold=-8.0 / duration=3）
DEFAULT_COOLDOWN_THRESHOLD = -8.0
DEFAULT_COOLDOWN_DURATION = 3

# 信号类型 → 中文（镜像 discipline_log._decorate）
SIGNAL_TYPE_CN = {'buy': '建仓', 'add': '加仓', 'reduce': '减仓', 'clear': '清仓'}
_VALID_SIGNAL_TYPES = frozenset(SIGNAL_TYPE_CN)

# 卖出向（其持有者盈亏方向与价格反向）
_SELL_TYPES = frozenset({'clear', 'reduce'})


# ---------------------------------------------------------------- 工具
def _now():
    return srv_time.now_iso()


def _row_signal(row: Signal) -> dict:
    """行 → 展示字典（宽松兜底）。"""
    execution = row.execution
    executed = bool(execution and execution.get("executed"))
    return {
        "id": row.id,
        "ts": row.ts,
        "code": row.code,
        "name": row.name,
        "market": row.market,
        "direction": row.direction,
        "period": row.period,
        "scheme": row.scheme,
        "signal_type": row.signal_type,
        "signal_label": row.signal_label,
        # F-1103 结构化枚举取代文案前缀匹配；signal_label 仅作展示
        "signal_type_cn": SIGNAL_TYPE_CN.get(row.signal_type, row.signal_label or row.signal_type),
        "trigger_price": row.trigger_price,
        "factor_score": row.factor_score,
        "position_ratio": row.position_ratio,
        "reason": row.reason,
        "execution": execution,
        "latest_price": row.latest_price,
        "latest_at": row.latest_at,
        "executed": executed,
    }


def _decorate(s: dict) -> dict:
    """单条信号对照：已执行用「执行价→现价」，未执行用「触发价→现价」，并算情绪化差。

    与桌面 `discipline_log._decorate` 逐字对齐（含结论文案），保证口径一致。
    """
    latest = float(s.get("latest_price") or 0)
    direction = s.get("direction", "long")
    mult = -1 if direction == "short" else 1  # 空单：下跌为盈利

    def _pct(ref, cur):
        if not ref or ref <= 0:
            return None
        return round((cur - ref) / ref * 100 * mult, 2)

    ex = s.get("execution")
    executed = bool(ex and ex.get("executed"))
    exec_price = (ex.get("exec_price") if ex else None)
    if executed and exec_price and exec_price > 0:
        ref, ref_kind = exec_price, "执行价"
        pct = _pct(ref, latest)
    else:
        ref, ref_kind = s.get("trigger_price") or 0, "触发价"
        pct = _pct(ref, latest)
    st = s.get("signal_type", "")
    # 对卖出向(clear/reduce)：价格下跌=对持有者有利=正
    if pct is not None and st in _SELL_TYPES:
        pct = -pct

    trig_pct = _pct(s.get("trigger_price") or 0, latest)
    if trig_pct is not None and st in _SELL_TYPES:
        trig_pct = -trig_pct
    exec_pct = pct if (executed and exec_price and exec_price > 0) else trig_pct

    conclusion = _conclusion(s, executed, pct)

    suggest = float(s.get("position_ratio") or 0)
    suggest_frac = suggest if suggest > 0 else 1.0
    er = 0.0
    if ex and ex.get("exec_ratio") not in (None, ""):
        exec_frac = min(float(ex.get("exec_ratio")) / 100, 1.0)
        er = max(0.0, exec_frac / suggest_frac)
    elif executed:
        er = 1.0
    un = max(0.0, 1.0 - er)
    dev = er - 1.0
    emotion = None
    emo_note = ""

    if trig_pct is not None:
        ratio_cost = 0.0
        price_cost = 0.0
        if executed and exec_pct is not None:
            if ex and ex.get("exec_ratio") not in (None, ""):
                exec_frac_actual = min(float(ex.get("exec_ratio")) / 100, 1.0)
            else:
                exec_frac_actual = suggest_frac
            delta = exec_frac_actual - suggest_frac
            if delta >= 0:
                ratio_cost = delta * exec_pct
                price_cost = suggest_frac * (exec_pct - trig_pct)
            else:
                ratio_cost = delta * trig_pct
                price_cost = exec_frac_actual * (exec_pct - trig_pct)
            emotion = round(ratio_cost + price_cost, 2)
        else:
            emotion = round(-trig_pct, 2)
            ratio_cost = -trig_pct
        if abs(emotion) > 0.005:
            if executed and exec_pct is not None:
                parts = []
                if abs(ratio_cost) > 0.005:
                    parts.append("比例%+.2f%%" % ratio_cost)
                if abs(price_cost) > 0.005:
                    parts.append("价格%+.2f%%" % price_cost)
                detail = "（" + "，".join(parts) + "）" if parts else ""
                emo_note = "情绪化%+.2f%%" % emotion + detail
            else:
                emo_note = "情绪化%+.2f%%（比例%+.2f%%）" % (emotion, ratio_cost)
        else:
            emotion = 0.0
            emo_note = "纪律执行"
    else:
        emotion = 0.0
        emo_note = "无数据"

    return {
        "id": s["id"], "ts": s["ts"],
        "code": s["code"], "name": s["name"],
        "market": s["market"], "direction": direction,
        "period": s["period"], "scheme": s["scheme"],
        "signal_type": s.get("signal_type", ""), "signal_label": s.get("signal_label"),
        "signal_type_cn": SIGNAL_TYPE_CN.get(s.get("signal_type", ""),
                                             s.get("signal_label") or s.get("signal_type", "")),
        "trigger_price": round(float(s.get("trigger_price") or 0), 2),
        "factor_score": s.get("factor_score"),
        "position_ratio": s.get("position_ratio"),
        "reason": s.get("reason"),
        "execution": s.get("execution"),
        "latest_price": round(latest, 2),
        "ref_kind": ref_kind,
        "executed": executed,
        "diff_pct": trig_pct,
        "conclusion": conclusion,
        "emotion_diff": emotion,
        "un_pct": round(abs(dev) * 100, 1),
        "emotion_note": emo_note,
        "exec_pct": exec_pct,
        "trig_pct": trig_pct,
        "er": round(er, 4),
        "un": round(un, 4),
        "dev": round(dev, 4),
    }


def _conclusion(s, executed, pct):
    if pct is None:
        return "缺价格数据"
    st = s.get("signal_type", "")
    sign = "+" if pct >= 0 else ""
    head = "已执行" if executed else "未执行"
    if st in ("buy", "add"):
        if executed:
            tail = ("—— 方向跟对，盈利拿到手" if pct >= 0 else "—— 买在了下跌前，这笔在亏")
            ref_word = "买入价"
        else:
            tail = ("—— 踏空，没赚到这笔" if pct >= 0 else "—— 躲过回调，没做反而省了")
            ref_word = "当时"
    elif st in ("clear", "reduce"):
        ex_price = float((s.get("execution") or {}).get("exec_price") or 0)
        trg = float(s.get("trigger_price") or 0)
        if executed:
            if ex_price and trg:
                dv = (ex_price - trg) / trg * 100
                sig_txt = "你%.2f卖出，比信号价%.2f%s%.1f%%" % (
                    ex_price, trg, ("高" if dv >= 0 else "低"), abs(dv))
            else:
                sig_txt = ""
            raw = -pct
            if raw < 0:
                tail = "—— %s；卖出后价格又跌%.1f%%，你躲开了下跌，卖得好（这次是赚）" % (sig_txt, abs(raw))
            else:
                tail = "—— %s；卖出后价格还涨%.1f%%，卖早了一些" % (sig_txt, raw)
            ref_word = "卖出价"
        else:
            raw = -pct
            tail = ("—— 没卖，信号后价格下跌被你死扛住了" if raw < 0
                    else "—— 没卖，信号后没怎么跌，留着反而对")
            ref_word = "信号价"
    else:
        tail = ""
        ref_word = "当时"
    return f"{head}，若从{ref_word}算起 {sign}{pct:.1f}%{tail}"


# ---------------------------------------------------------------- 冷却（单例行）
def _get_cooldown(session) -> Cooldown:
    row = session.execute(select(Cooldown).where(Cooldown.id == 1)).scalar_one_or_none()
    if row is None:
        row = Cooldown(id=1, threshold_pct=DEFAULT_COOLDOWN_THRESHOLD,
                       duration_days=DEFAULT_COOLDOWN_DURATION)
        session.add(row)
        session.flush()
    return row


def cooldown_status(session) -> dict:
    row = _get_cooldown(session)
    return {
        "active": bool(row.active),
        "reason": row.reason,
        "started_at": row.started_at,
        "until": row.until,
        "bias_pct": round(float(row.bias_pct or 0), 2),
        "threshold_pct": float(row.threshold_pct or DEFAULT_COOLDOWN_THRESHOLD),
        "duration_days": int(row.duration_days or DEFAULT_COOLDOWN_DURATION),
    }


def set_cooldown_config(session, threshold_pct=None, duration_days=None) -> dict:
    row = _get_cooldown(session)
    if threshold_pct is not None:
        t = float(threshold_pct)
        if t > 0:
            raise ValueError("冷却阈值必须是负值（如 -8）")
        row.threshold_pct = t
    if duration_days is not None:
        row.duration_days = max(1, int(float(duration_days)))
    recompute_cooldown(session)
    return {"ok": True, **get_cooldown_config(session), "cooldown": cooldown_status(session)}


def get_cooldown_config(session) -> dict:
    row = _get_cooldown(session)
    return {
        "threshold_pct": float(row.threshold_pct or DEFAULT_COOLDOWN_THRESHOLD),
        "duration_days": int(row.duration_days or DEFAULT_COOLDOWN_DURATION),
    }


def _emotion_bias_pct(session, rows) -> tuple:
    total = 0.0
    n = 0
    for row in rows:
        d = _decorate(_row_signal(row))
        e = d.get("emotion_diff")
        if e is not None:
            total += e
            n += 1
    return round(total, 2), n


def recompute_cooldown(session) -> dict:
    """每次信号变化/现价刷新后触发。镜像桌面 `_recompute_cooldown`。"""
    row = _get_cooldown(session)
    threshold = float(row.threshold_pct or DEFAULT_COOLDOWN_THRESHOLD)
    duration = max(1, int(row.duration_days or DEFAULT_COOLDOWN_DURATION))
    rows = session.execute(select(Signal).order_by(Signal.ts.desc())).scalars().all()
    bias, bias_n = _emotion_bias_pct(session, rows)
    row.bias_pct = bias

    # ISO 串（+08:00，按序可字典序比较）
    now = _now()
    active = bool(row.active)
    if active:
        should_clear = False
        until = row.until
        if until:
            # 到期判定：until 解析后转为 aware datetime 比较
            try:
                end = datetime.fromisoformat(until.replace("Z", "+00:00"))
                if not end.tzinfo:
                    end = end.replace(tzinfo=TZ)
                if srv_time.now() >= end:
                    should_clear = True  # 到期
            except ValueError:
                should_clear = True
        if bias_n == 0:
            should_clear = True
        elif bias > threshold:
            should_clear = True
        if should_clear:
            row.active = False
            row.started_at = None
            row.until = None
            row.reason = None
            row.bias_pct = 0.0
            active = False
    if not active and bias_n and bias <= threshold:
        row.active = True
        row.started_at = now
        row.until = (srv_time.now() + timedelta(days=duration)).isoformat(timespec="seconds")
        row.reason = f"情绪偏差累计 {bias:.1f}% ≤ {threshold:.1f}% 阈值（未执行信号持续吃亏）"
    return cooldown_status(session)


# ---------------------------------------------------------------- 信号增删改查
def append_signal(session, *, code, name="", market="stock", direction="long",
                  period="日K", scheme="", signal_type="buy", signal_label="",
                  price=0.0, factor_score=None, position_ratio=None, reason="",
                  cooldown_gate=True):
    """落一条信号。F-1103：冷却激活时不入新信号（cooldown_gate=True 时）。
    同日同股同类型幂等去重。返回 {'signal': dict|None, 'cooldown': status}。
    """
    if signal_type not in _VALID_SIGNAL_TYPES:
        signal_type = "buy"
    status = cooldown_status(session)
    if cooldown_gate and status.get("active"):
        return {"signal": None, "cooldown": status}
    today = _now()[:10]
    dup = session.execute(
        select(Signal.id).where(
            Signal.code == code, Signal.signal_type == signal_type,
            Signal.period == period, Signal.ts.like(today + "%"))).scalar_one_or_none()
    if dup:
        return {"signal": _row_signal(session.get(Signal, dup)), "cooldown": status}
    now = _now()
    sig_id = uuid.uuid4().hex[:12]
    row = Signal(
        id=sig_id, ts=now, code=code, name=name, market=market, direction=direction,
        period=period, scheme=scheme, signal_type=signal_type, signal_label=signal_label,
        trigger_price=float(price or 0), factor_score=factor_score,
        position_ratio=position_ratio, reason=reason or "",
        execution=None, latest_price=float(price or 0), latest_at=now,
    )
    session.add(row)
    session.flush()
    return {"signal": _row_signal(row), "cooldown": cooldown_status(session)}


def set_execution(session, signal_id, executed, exec_price=None, exec_ratio=None):
    """回填执行（F-1105 乐观更新）。返回 {'ok', 'emotion_diff'}。信号不存在 → None。"""
    row = session.get(Signal, signal_id)
    if row is None:
        return None
    row.execution = {
        "executed": bool(executed),
        "exec_price": float(exec_price) if exec_price not in (None, "") else None,
        "exec_ratio": float(exec_ratio) if exec_ratio not in (None, "") else None,
        "at": _now(),
    }
    d = _decorate(_row_signal(session.get(Signal, signal_id)))
    recompute_cooldown(session)
    session.flush()
    return {"ok": True, "id": signal_id, "emotion_diff": d.get("emotion_diff")}


def record_price(session, signal_id, latest_price) -> None:
    try:
        latest = float(latest_price)
    except (TypeError, ValueError):
        return
    row = session.get(Signal, signal_id)
    if row is None:
        return
    row.latest_price = latest
    row.latest_at = _now()
    recompute_cooldown(session)
    session.flush()


def update_price_by_code(session, code, latest_price) -> int:
    """按代码批量刷新该股未了结信号的现价（F-1101 批量/缓存口径）。"""
    try:
        latest = float(latest_price)
    except (TypeError, ValueError):
        return 0
    rows = session.execute(select(Signal).where(Signal.code == code)).scalars().all()
    n = 0
    for row in rows:
        row.latest_price = latest
        row.latest_at = _now()
        n += 1
    if n:
        recompute_cooldown(session)
        session.flush()
    return n


def delete_signal(session, signal_id) -> bool:
    res = session.execute(delete(Signal).where(Signal.id == signal_id))
    if res.rowcount:
        recompute_cooldown(session)
        session.flush()
        return True
    return False


def list_signals(session, market=None, limit=100, cursor=None) -> list[dict]:
    """游标分页：cursor=上一页末条 id；按 ts 倒序。market='all'/None 不过滤。"""
    query = select(Signal).order_by(Signal.ts.desc(), Signal.id.desc())
    if market and market != "all":
        query = query.where(Signal.market == market)
    if cursor:
        row = session.get(Signal, cursor)
        if row is not None:
            query = query.where(
                (Signal.ts, Signal.id) < (row.ts, row.id))
    rows = session.execute(query.limit(limit)).scalars().all()
    return [_decorate(_row_signal(r)) for r in rows]


def count_signals(session, market=None) -> int:
    query = select(Signal)
    if market and market != "all":
        query = query.where(Signal.market == market)
    return session.execute(select(func.count()).select_from(query.subquery())).scalar() or 0


def ledger_summary(session, market=None) -> dict:
    """F-1104：4 指标——纪律盈亏/执行数/情绪化差合计/未按纪律数+冷却。

    纪律盈亏按建议比例加权，情绪化差「没赚就是亏」口径来自 _decorate。
    """
    rows = session.execute(select(Signal).order_by(Signal.ts.desc())).scalars().all()
    if market and market != "all":
        rows = [r for r in rows if r.market == market]
    disc_total = 0.0
    emotion_total = 0.0
    disc_n = 0
    emotion_n = 0
    for r in rows:
        d = _decorate(_row_signal(r))
        trig = d.get("trig_pct")
        suggest = float(r.position_ratio or 0)
        suggest_frac = suggest if suggest > 0 else 1.0
        if trig is not None:
            disc_total += suggest_frac * trig
        e = d.get("emotion_diff")
        if e is not None:
            emotion_total += e
            emotion_n += 1
        if r.execution and r.execution.get("executed"):
            disc_n += 1
    return {
        "total_signals": len(rows),
        # 纪律盈亏（按建议比例加权）
        "disciple_pct_sum": round(disc_total, 2),
        # 执行数
        "disciple_count": disc_n,
        # 未按纪律数
        "not_executed_count": len(rows) - disc_n,
        # 情绪化差合计
        "emotion_pct_sum": round(emotion_total, 2),
        "emotion_pct_n": emotion_n,
        "bias_pct": round(emotion_total, 2),
        "cooldown": cooldown_status(session),
        "config": get_cooldown_config(session),
    }