#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""报告构建器 v5.0 - 最终版"""

from engine.quant_config import (
    load_entry_condition,
    get_risk_params,
    get_reduce_params,
    get_entry_conditions,
    get_thresholds,
    get_entry_params,
    get_usage_mode,
    VETO_FLAG_LABELS,
    VETO_KEYS,
    veto_keys_for_level,
)
from engine.unified_entry_logic import UnifiedEntryLogic
from engine.market_gate import MarketGate
from engine.factor_registry import get_factor, factor_explanation, parse_raw_value
from engine.indicators import MATechnical


class DecisionResult:
    """建仓决策的单一真相源（在 TradingPipeline.execute 中计算一次并缓存到 ctx.decision）。

    报告各板块（核心结论 / 操作参考 / 多空信号综合）与 web_api 均只读取本对象，
    不再各自重算裁决，从根本上消除『核心结论』与『多空信号综合结论』前后文不一致。
    """

    __slots__ = ('entry_levels', 'verdict', 'position', 'badge_text', 'hit_line_name')

    def __init__(self, entry_levels, verdict, position, badge_text, hit_line_name=None):
        self.entry_levels = entry_levels          # list[dict]：_evaluate_strategic_entry_lines 的结果
        self.verdict = verdict                    # 'hit' / 'rebound' / 'wait' / 'none'
        self.position = position                  # 命中/观望档门控后仓位（0-1）
        self.badge_text = badge_text             # 空仓核心结论徽章（含 ⏳/✅ 前缀）
        self.hit_line_name = hit_line_name        # 实际命中的线名（hit=level_name / rebound='博反弹线'），用于综合结论精准化


def compute_decision(ctx):
    """计算并缓存建仓裁决（单次真相源）。

    逻辑完全等价旧 _render_summary_board 空仓分支：命中 rebound/hit 优先，否则看观望线
    是否真正命中（verdict=='wait'），再区分『得分达线被否决项/技术信号挡』与『得分未达线』。
    """
    # 初级用法（因子休眠）：结论文案直接采用「触发指标建仓 / 空仓观望」，
    # 与 UnifiedEntryLogic.check_entry basic 分支一致，不出现 强势线/标准线 及「得分X分」。
    if get_usage_mode() == 'basic':
        entry_levels = _evaluate_strategic_entry_lines(ctx)
        _action = getattr(ctx, 'entry_action', '') or ''
        _pos = float(getattr(ctx, 'entry_position', 0) or 0)
        _reason = getattr(ctx, 'entry_reason', '') or ''
        if _action in ('关注建仓', '观望仓', '博反弹', '恐慌反转') and _pos > 0:
            badge = f"✅ 技术条件建仓 — {_reason}，计划仓位 {_pos*100:.0f}%"
            return DecisionResult(entry_levels=entry_levels, verdict='hit', position=_pos,
                                  badge_text=badge, hit_line_name='技术条件建仓')
        badge = f"⏳ 空仓观望 — {_reason}" if _reason else '⏳ 空仓观望 — 请先在「仓位管理 → 建仓参数」配置触发指标'
        return DecisionResult(entry_levels=entry_levels, verdict='wait', position=0.0,
                              badge_text=badge, hit_line_name=_reason or None)

    entry_levels = _evaluate_strategic_entry_lines(ctx)
    rebound = next((lv for lv in entry_levels if lv['verdict'] == 'rebound'), None)
    hit = next((lv for lv in entry_levels if lv['verdict'] == 'hit'), None)
    if rebound:
        badge = f"✅ 小仓试探 — 博反弹线命中（{rebound['reason']}）"
        verdict, position, line_name = 'rebound', rebound['position'], '博反弹线'
    elif hit:
        badge = (f"✅ 建仓条件已触发 — 命中「{hit['level_name']}」"
                 f"（按您的方案设定，触发时计划动用总资金的 {hit['position']*100:.0f}%）")
        verdict, position, line_name = 'hit', hit['position'], hit['level_name']
    else:
        pend = next((lv for lv in entry_levels if lv['level_key'] == 'pending'), None)
        if pend and pend.get('verdict') == 'wait':
            badge = f"⏳ 观望 — 得分达观望线，小仓观察（约 {pend['position']*100:.0f}%）"
            verdict, position, line_name = 'wait', pend['position'], None
        else:
            blocked = any(
                lv.get('score_ok') and lv['verdict'] != 'hit'
                for lv in entry_levels if lv['level_key'] != 'rebound'
            )
            if blocked:
                badge = "⏳ 观望 — 得分已达建仓线，但被否决项/技术信号挡，暂不满足建仓条件"
            elif pend and not pend.get('score_ok'):
                badge = "⏳ 观望 — 未达观望线，等待信号"
            else:
                badge = "⏳ 观望 — 未命中任何建仓线，等待信号"
            verdict, position, line_name = 'wait', 0.0, None
    return DecisionResult(entry_levels=entry_levels, verdict=verdict,
                         position=position, badge_text=badge, hit_line_name=line_name)


def _bull_bear_desc(bull_count, bear_count, direction='long'):
    """多空计数概览文案；空单视角 bull=对空头有利，措辞方向化（偏多↔偏空）。"""
    if bull_count > bear_count:
        return "多数信号偏空（趋势判断）" if direction == 'short' else "多数信号偏多（趋势判断）"
    elif bear_count > bull_count:
        return "多数信号偏多（趋势走弱）" if direction == 'short' else "多数信号偏空（趋势走弱）"
    return "信号分歧（方向不明）"


def _signals_conclusion(verdict, bull_count, bear_count, reduce_triggered, has_position,
                        hit_line_name=None, hit_position=0.0, direction='long'):
    """多空信号综合结论：report_builder 与 web_api 共用的唯一映射，杜绝文案散落硬编码。

    返回 (icon, phrase)：
      - report_builder 拼成「{icon} 综合：{phrase}」
      - web_api 直接用 phrase 作为 bullBearSummary / positionBullBearSummary
    verdict 来自 ctx.decision（空仓时决定『支持建仓』还是『维持观望』），
    故综合结论与顶部核心结论天然一致，不再机械数 emoji。

    hit_line_name / hit_position 用于把综合结论精准化：命中标准线/强势线时引用线名与仓位，
    不再一律复读「试探建仓」万能词（试探建仓仅对应最低的试探线）。
    direction：'short'（空单）时 bull/bear 计数已按空头视角（bull_bear ok 已取反），
    文案随之方向化（偏多→偏空），避免空单报告出现「多数信号偏多」的误导。
    """
    _is_short = (direction == 'short')
    _bull_txt = '偏空' if _is_short else '偏多'   # 空单视角：bull=对空头有利
    _bear_txt = '偏多' if _is_short else '偏空'
    _open_txt = '建空' if _is_short else '建仓'
    if has_position:
        if reduce_triggered:
            return ('⚠️', f"{_bull_bear_desc(bull_count, bear_count, direction)}，但风控减仓档已触发（风险约束）——操作上以风控优先，趋势信号作为后续再入场的参考")
        if bull_count > bear_count:
            return ('✅', f"多数信号{_bull_txt} → 未触及减仓参考条件")
        elif bear_count > bull_count:
            return ('❌', f"多数信号{_bear_txt} → 已触及减仓参考条件")
        return ('⚠️', "信号分歧 → 多看少动")
    # 空仓
    if verdict in ('hit', 'rebound'):
        if bull_count > bear_count:
            if verdict == 'rebound':
                return ('✅', f"多数信号{_bull_txt}，满足试探{_open_txt}参考条件（博反弹线命中）")
            # 标准线 / 强势线 等正式建仓档：精准引用线名与计划仓位
            if hit_line_name and hit_position:
                return ('✅', f"多数信号{_bull_txt}，满足{_open_txt}参考条件（命中「{hit_line_name}」，参考仓位{hit_position*100:.0f}% 按您的方案设定）")
            if hit_line_name:
                return ('✅', f"多数信号{_bull_txt}，满足{_open_txt}参考条件（命中「{hit_line_name}」）")
            return ('✅', f"多数信号{_bull_txt}，满足{_open_txt}参考条件")
        elif bear_count > bull_count:
            return ('⚠️', f"信号多空分化，已触发建仓档但部分信号{_bear_txt}，注意分批与风控")
        return ('⚠️', "信号分歧 → 多看少动")
    # 未触发建仓（得分未达线/被技术信号或否决项挡/仅达观望线）：与『观望』一致，不鼓吹建仓
    if bull_count > bear_count:
        # 初级用法（因子休眠）：无得分概念，改用『触发指标未满足』表述
        _not_hit = '触发指标未满足' if get_usage_mode() == 'basic' else '得分未达建仓触发线'
        return ('⚠️', f"技术面多数信号{_bull_txt}，但{_not_hit}，维持观望（小仓观察）")
    elif bear_count > bull_count:
        return ('❌', f"多数信号{_bear_txt} → 偏向观望")
    return ('⚠️', "信号分歧 → 多看少动")


def _factor_label(code):
    """因子 code → 中文 label，未知 code 原样返回（兜底）。"""
    f = get_factor(code)
    return f.label if f else code


# ── 因子 → 直白解释映射表 ──────────────────────────────────────────
# 每个因子配一对：(贡献>0时用, 贡献<0时用)
# 说明：因子贡献的正负取决于因子原始值方向 × 校准方向(+1/-1)
# 解释直接对应「这份贡献意味着什么」，不区分原始值的技术含义
_FACTOR_EXPLANATIONS = {
    'bb_bandwidth': (
        '布林带扩张，高波动环境，方向确定性降低',
        '布林带收窄，波动压缩后倾向于方向性突破',
    ),
    'current_drawdown': (
        '回撤幅度温和，趋势未受破坏',
        '回撤较深，短期趋势偏弱，需要时间修复',
    ),
    'di_spread': (
        '多方力量占优，上升动能充足',
        '空方力量占优，下跌压力较大',
    ),
    'macd_hist_norm': (
        'MACD动能正向，短期惯性有利',
        'MACD动能负向，短期惯性偏弱',
    ),
    'ma_slope': (
        '均线斜率向上，中期趋势向好',
        '均线斜率向下，中期趋势走弱',
    ),
    'ma_arrangement': (
        '均线多头排列，趋势状态健康',
        '均线空头排列，趋势状态偏弱',
    ),
    'obv_trend': (
        '资金持续流入，量能趋势向上',
        '资金持续流出，量能趋势向下',
    ),
    'volatility_cone': (
        '波动率处于低位，走势稳健',
        '波动率偏高，注意大幅波动风险',
    ),
    'atr_norm': (
        'ATR偏高，日内振幅较大',
        'ATR温和，价格波动在可控范围',
    ),
    'bias_value': (
        '价格不偏离均线，无回归压力',
        '价格大幅偏离均线，存在均值回归风险',
    ),
    'pattern_reverse': (
        'K线形态出现积极信号，反转概率上升',
        'K线形态偏空，注意下行风险',
    ),
    'fib_position': (
        '价格在斐波那契支撑区附近，反弹概率较高',
        '价格接近斐波那契阻力区，上行空间有限',
    ),
    'relative_strength_20d': (
        '近期走势强于市场，动量持续',
        '近期走势弱于市场，动量不足',
    ),
    'rsi_value': (
        'RSI适中，市场情绪不极端',
        'RSI失衡，市场情绪偏向极端',
    ),
    'volume_ratio': (
        '成交量温和，无异常放量',
        '成交量异常放大，注意主力动向',
    ),
    'volume_price_signal': (
        '量价配合良好，上涨有资金支持',
        '量价背离，上涨缺乏成交量确认',
    ),
    'pivot_distance': (
        '价格接近枢轴支撑位，有技术支撑',
        '价格远离支撑位，缺乏短期技术参考',
    ),
    'chip_concentration': (
        '筹码集中度高，上方抛压较小',
        '筹码分散，套牢盘压力较重',
    ),
}


def _explain_factor(entry_line):
    """从一条 score_detail 行提取因子名和贡献值，返回直白解释。

    entry_line 支持三种格式:
      - 因子:     "bb_bandwidth: 0.0342 -> z=1.23 x w=0.410 = +15.1"
      - 冲突调整: "冲突调整: -8.0 (放量下跌(抛压确认))"
    返回: (explanation, contribution_val) 或 None
    """
    # ── 非因子条目：冲突调整（真实计入因子预期分，原 '->' 检查会吞掉，现单独解析）──
    if entry_line.startswith('冲突调整'):
        try:
            contrib = float(entry_line.split(':', 1)[1].split('(', 1)[0].strip())
        except (ValueError, IndexError):
            contrib = 0.0
        label = entry_line.split('(', 1)[-1].rstrip(')') if '(' in entry_line else ''
        if contrib < 0:
            expl = f"存在冲突信号，拉低因子预期（{label}）"
        elif contrib > 0:
            expl = f"存在反转加成信号，提升因子预期（{label}）"
        else:
            expl = "无显著冲突信号，因子预期未被削弱"
        return (expl, contrib)

    if '=' not in entry_line or '->' not in entry_line:
        return None
    # 提取因子名
    name = entry_line.split(':')[0].strip()
    # 提取贡献值（最后一个 = 之后）
    try:
        contrib_str = entry_line.rsplit('=', 1)[-1].strip()
        contribution = float(contrib_str)
    except (ValueError, IndexError):
        return None

    pair = _FACTOR_EXPLANATIONS.get(name)
    if pair is None:
        return None

    # 按因子 raw 档位/值域选文本（多档位/非单调因子修复，2026-08-19）；
    # 单调因子回退按贡献正负选二元文本（原逻辑，行为不变）
    explanation = factor_explanation(name, parse_raw_value(entry_line), contribution, pair)
    return (explanation, contribution)


def _build_entry_conditions(ctx):
    """空仓场景：构建「可设置的监控条件」清单，明确每条条件的当前触发状态。

    定位：单次即时查询条件触发器。
    - 条件来源于您在【模型配置】中设定的参数（MA周期/前高/量价/MACD/风险阈值等）
    - 工具只负责把实时行情与您的阈值比对，给出 ✅已触发 / ⏳未触发 / 🛑风险触发 状态
    - 不给出任何买卖建议、仓位或目标价；是否操作由您独立判断
    """
    cfg = load_entry_condition()
    if cfg is None:
        return []  # 用户未启用建仓条件动态参数
    vol_cfg = cfg['volatility']
    score_cfg = cfg['score_bias']

    conditions = []  # 每条: {icon, name, state, detail}
    current_price = ctx.latest_price
    tech = ctx.tech
    market = ctx.market
    data_list = ctx.data_list
    final_score = getattr(ctx, 'final_score', 0)

    # ── 波动率（环境信息，非触发条件）──
    atr_ratio = tech.get('atr_ratio', 0.02)
    high_vol = atr_ratio >= vol_cfg['atr_high_threshold']
    low_vol = atr_ratio <= vol_cfg['atr_low_threshold']
    if high_vol:
        vol_label = f"高波动（日均振幅{atr_ratio*100:.1f}%）→ MA周期延长过滤假突破"
    elif low_vol:
        vol_label = f"低波动（日均振幅{atr_ratio*100:.1f}%）→ MA周期缩短提高灵敏度"
    else:
        vol_label = f"中等波动（日均振幅{atr_ratio*100:.1f}%）"
    conditions.append({'icon': 'ℹ️', 'name': '波动率环境', 'state': 'info', 'detail': vol_label})

    # ── 因子预期（环境信息）──
    # 空单视角：final_score 已是方向化后的分（方向性因子取反），文案标注视角防误读
    _view_tag = '（空单视角）' if getattr(ctx, 'direction', 'long') == 'short' else ''
    if final_score >= score_cfg['score_high_threshold']:
        score_label = f"因子预期较强{_view_tag}（{final_score:.0f}分），信号可靠"
    elif final_score < score_cfg['score_low_threshold']:
        score_label = f"因子预期偏弱{_view_tag}（{final_score:.0f}分），信号存疑"
    else:
        score_label = f"因子预期中性{_view_tag}（{final_score:.0f}分）"
    conditions.append({'icon': '📊', 'name': '因子预期', 'state': 'info', 'detail': score_label})

    # ── 自适应 MA 周期 ──
    if high_vol:
        sf = vol_cfg['ma_stretch_factor']
    elif low_vol:
        sf = vol_cfg['ma_shrink_factor']
    else:
        sf = 1.0
    base_periods = [5, 10, 20, 60]
    scaled_periods = [max(3, int(p * sf)) for p in base_periods]
    ma_keys = [f'sma_{p}' for p in base_periods]
    ma_labels = ['MA5', 'MA10', 'MA20', 'MA60']
    mas = {}
    for key, p in zip(ma_keys, scaled_periods):
        mas[key] = {'price': tech.get(key, current_price), 'label': f'MA{p}'}
    above = {k: current_price > v['price'] for k, v in mas.items()}

    # ── 近期高低点 ──
    if data_list and len(data_list) >= 20:
        recent_high = max(d['high'] for d in data_list[-20:])
        recent_low = min(d['low'] for d in data_list[-20:])
    else:
        recent_high = current_price * 1.10
        recent_low = current_price * 0.90

    # ── 偏多触发条件：站上 MA ──
    for key, label in zip(ma_keys, ma_labels):
        ma_price = mas[key]['price']
        if above[key]:
            conditions.append({
                'icon': '✅', 'name': f'站上{label}', 'state': 'trig',
                'detail': f'已触发 — 当前价 {current_price:.2f} > {label}({ma_price:.2f})',
            })
        else:
            gap = (ma_price - current_price) / current_price * 100 if current_price else 0
            conditions.append({
                'icon': '⏳', 'name': f'站上{label}', 'state': 'wait',
                'detail': f'未触发 — 当前价 {current_price:.2f} < {label}({ma_price:.2f})，差 {gap:.1f}%',
            })

    # ── 偏多触发条件：突破前高 ──
    if recent_high <= current_price:
        conditions.append({'icon': '✅', 'name': '突破前高', 'state': 'trig',
                           'detail': f'已触发 — 当前价 {current_price:.2f} 已创近期新高（前高 {recent_high:.2f}）'})
    else:
        gap = (recent_high - current_price) / current_price * 100 if current_price else 0
        conditions.append({'icon': '⏳', 'name': '突破前高', 'state': 'wait',
                           'detail': f'未触发 — 当前价 {current_price:.2f} < 前高 {recent_high:.2f}，差 {gap:.1f}%'})

    # ── 偏多触发条件：放量上涨 ──
    vol_price = market.get('volume_price', '')
    if vol_price == '放量上涨':
        conditions.append({'icon': '✅', 'name': '放量上涨', 'state': 'trig', 'detail': '已触发 — 量价配合，资金积极介入'})
    elif vol_price == '缩量上涨':
        conditions.append({'icon': '⏳', 'name': '放量上涨', 'state': 'wait', 'detail': '未触发 — 当前为缩量上涨，量能不足'})
    else:
        conditions.append({'icon': '⏳', 'name': '放量上涨', 'state': 'wait', 'detail': f'未触发 — 当前量价：{vol_price or "正常"}'})

    # ── 偏多触发条件：MACD 金叉 ──
    macd_status = tech.get('macd_status', '')
    if '金叉' in macd_status:
        conditions.append({'icon': '✅', 'name': 'MACD金叉', 'state': 'trig', 'detail': '已触发 — 短期动能向上'})
    elif '死叉' in macd_status:
        conditions.append({'icon': '⏳', 'name': 'MACD金叉', 'state': 'wait', 'detail': '未触发 — 当前死叉状态，等待金叉'})
    else:
        conditions.append({'icon': '⏳', 'name': 'MACD金叉', 'state': 'wait', 'detail': '未触发 — 动能走平'})

    # ── 风险监控条件（反向）：跌破 MA20 / 前低 ──
    if above['sma_20']:
        conditions.append({'icon': '🟢', 'name': '跌破MA20风险', 'state': 'risk',
                           'detail': f'未触发 — 当前价 {current_price:.2f} > MA20({mas["sma_20"]["price"]:.2f})，趋势完好'})
    else:
        conditions.append({'icon': '🛑', 'name': '跌破MA20风险', 'state': 'risk_on',
                           'detail': f'已触发 — 当前价 {current_price:.2f} < MA20({mas["sma_20"]["price"]:.2f})，中期趋势破坏'})
    if recent_low < current_price and above['sma_20']:
        conditions.append({'icon': '🟢', 'name': '跌破前低风险', 'state': 'risk',
                           'detail': f'未触发 — 当前价 {current_price:.2f} > 前低 {recent_low:.2f}，未破位'})
    elif recent_low >= current_price:
        conditions.append({'icon': '🛑', 'name': '跌破前低风险', 'state': 'risk_on',
                           'detail': f'已触发 — 当前价已跌破前低 {recent_low:.2f}，破位下行'})

    return conditions


def _build_tech_signals(ctx):
    """从 ctx.tech / ctx.market 提取关键技术指标，转为直白解释。
    
    覆盖 MA排列、MACD、RSI、量价关系四项，用统一风格输出。
    信号符号：✅ 偏多/健康 ｜ ⚠️ 存疑/超买/中性待确认 ｜ ❌ 偏空。
    筹码/斐波那契/枢轴仍在调用方单独处理。
    """
    lines = []
    tech = ctx.tech
    market = ctx.market

    # 1. MA 排列（按档位分档，避免"多头初期"被夸大为"多头格局"，2026-08-19）
    ma_arr = market.get('ma_arrangement', '')
    _ma_level = MATechnical.arrangement_level(ma_arr)
    if _ma_level >= 2.0:
        lines.append("  ✅ 均线趋势：短期均线在长期均线之上，多头排列，趋势向上有支撑")
    elif _ma_level == 1.0:
        lines.append("  ✅ 均线趋势：短期均线初步上穿，多头初期，趋势待确认")
    elif _ma_level <= -2.0:
        lines.append("  ❌ 均线趋势：短期均线在长期均线之下，空头排列，上方压力较重")
    elif _ma_level == -1.0:
        lines.append("  ❌ 均线趋势：短期均线初步下穿，空头初期，趋势转弱")
    else:
        lines.append("  ⚠️ 均线趋势：均线交织缠绕，方向不明，等待趋势明朗")

    # 2. MACD
    macd_status = tech.get('macd_status', '')
    if '金叉' in macd_status:
        lines.append("  ✅ MACD动能：金叉状态，短期动能向上，多头占优")
    elif '死叉' in macd_status:
        lines.append("  ❌ MACD动能：死叉状态，短期动能向下，空头占优")
    else:
        lines.append("  ⚠️ MACD动能：动能走平，多空力量均衡")

    # 3. RSI
    rsi = tech.get('rsi', tech.get('rsi_14', 50))
    if rsi > 70:
        lines.append(f"  ⚠️ 短期情绪：RSI {rsi:.0f}，处于超买区域，短期追高风险较大")
    elif rsi < 30:
        lines.append(f"  ✅ 短期情绪：RSI {rsi:.0f}，处于超卖区域，反弹概率较高")
    else:
        lines.append(f"  ✅ 短期情绪：RSI {rsi:.0f}，中性区间，既未超买也未超卖")

    # 4. 量价关系
    vol_price = market.get('volume_price', '')
    if vol_price == '放量上涨':
        lines.append("  ✅ 量价关系：放量上涨，资金积极介入，上涨有成交量确认")
    elif vol_price == '放量下跌':
        lines.append("  ❌ 量价关系：放量下跌，抛压明显，空方主导")
    elif vol_price == '缩量上涨':
        lines.append("  ⚠️ 量价关系：缩量上涨，参与资金不足，上涨持续性存疑")
    elif vol_price == '缩量下跌':
        lines.append("  ✅ 量价关系：缩量下跌，抛压衰竭，可能接近底部")
    else:
        lines.append("  ✅ 量价关系：成交量正常，无异常信号")

    return lines


def _build_factor_basis_lines(ctx):
    """从 ctx.score_detail 构建「决策依据」板块的文本行列表。
    
    解析每项因子的贡献值，转为直白解释，按加减分分组展示。
    不依赖 MA/MACD/量价/RSI 等技术指标判断。
    """
    lines = []
    lines.append("  【决策依据】")

    pos_factors = []
    neg_factors = []
    parsed_any = False  # score_detail 是否有可解析条目

    for entry in getattr(ctx, 'score_detail', []):
        result = _explain_factor(entry)
        if result is None:
            continue
        parsed_any = True
        expl, contrib = result
        # 零贡献因子无方向信号，不计入加减分（避免"加分项里写负向内容"的矛盾）
        if contrib > 0:
            pos_factors.append((expl, contrib))
        elif contrib < 0:
            neg_factors.append((expl, contrib))

    # 按贡献绝对值排序
    pos_factors.sort(key=lambda x: -abs(x[1]))
    neg_factors.sort(key=lambda x: -abs(x[1]))

    # 全部展示加减分项，不折叠（用户要求）
    if pos_factors:
        lines.append("  ▎加分项（推高因子预期）：")
        for expl, contrib in pos_factors:
            # 注意：{contrib:+.0f} 已自带正号，不要再写字面量 '+'，否则出现 "++"
            lines.append(f"   {contrib:+.0f}  {expl}")

    if neg_factors:
        if pos_factors:
            lines.append("")
        lines.append("  ▎减分项（拉低因子预期）：")
        for expl, contrib in neg_factors:
            lines.append(f"   {contrib:+.0f}  {expl}")

    if not parsed_any:
        # 真·无数据：score_detail 为空或全部条目无法解析
        lines.append("  ⚠️ 因子数据不足，无法解析详细依据")
    else:
        # 透明度：展示各项贡献合计与最终因子预期分（含截断），帮助理解分数构成
        raw_total = sum(c for _, c in pos_factors) + sum(c for _, c in neg_factors)
        lines.append("")
        lines.append(f"  → 各项贡献合计：{raw_total:+.0f}（截断后因子预期分：{getattr(ctx, 'final_score', 0):.0f}）")
        if not pos_factors and not neg_factors:
            # 所有因子贡献均为 0（如全部 z=0）：补一句中性说明，避免误显示"数据不足"
            lines.append("  （全部因子贡献为 0，因子预期无方向性偏移）")

    return lines


def _build_tech_resonance_lines(ctx):
    """从 ctx.signal_rating 构建「技术共振明细」板块。

    展示技术面强度(tech_strength)与技术共振构成：方案因子中"显著表态
    （|贡献|≥阈值）"的能量占总能量的比重。加分/减分因子分别列出。
    星级只描述技术面强不强/确不确定，不回答多空（合规：不替用户判断方向）。

    注：技术共振配置留空（threshold/bands 未填写）时视为不启用，本板块整段隐藏。
    """
    sr = getattr(ctx, 'signal_rating', None)
    if not isinstance(sr, dict):
        return []
    ts = sr.get('tech_strength')
    if ts is None:
        return []

    # 未启用技术共振（留空）时不显示该板块
    threshold = sr.get('tech_resonance_threshold')
    bands = sr.get('tech_strength_bands')
    if threshold is None or not isinstance(bands, (list, tuple)) or len(bands) != 4:
        return []

    lines = []
    lines.append("")
    lines.append("  【技术共振明细 · 纯技术面强度】")
    level = sr.get('level', '') or ''
    stars = sr.get('stars', '') or ''
    lines.append(f"  技术面强度：{stars} {level}（共振占比 {ts * 100:.0f}%）")
    lines.append(f"  显著阈值：单因子 |贡献| ≥ {threshold:.1f} 才计入共振（加减分同口径）")
    sig = sr.get('significant_factors') or []
    if not sig:
        lines.append("  无显著因子（各因子贡献均低于阈值，技术面无明确表态）")
        return lines
    pos = [(n, c) for (n, c) in sig if c > 0]
    neg = [(n, c) for (n, c) in sig if c < 0]
    if pos:
        lines.append("  加分显著因子（技术面看多表态）：")
        for n, c in sorted(pos, key=lambda x: -abs(x[1])):
            lines.append(f"   +{c:.1f}  {_factor_label(n)}")
    if neg:
        lines.append("  减分显著因子（技术面看空表态）：")
        for n, c in sorted(neg, key=lambda x: -abs(x[1])):
            lines.append(f"   {c:.1f}  {_factor_label(n)}")
    lines.append("  → 星级仅衡量技术面强度/确定性，不预示涨跌；方向由上述加减分自行判断")
    return lines


def _clean_trigger_label(label):
    """从减仓引擎的触发标签中剥离括号里的价格，保留可读触发名。

    例："MA5（1303.80）"→"MA5"；"跌幅3%（1234.56）"→"跌幅3%"；"MACD死叉（柱…）"→"MACD死叉"
    """
    import re
    m = re.match(r'^(.*?)[（(]', label or '')
    return m.group(1).strip() if m else (label or '')


def _thr_text(thr):
    """格式化分类阈值。0/负数都是合法阈值，绝不打"未设定阈值"标签。"""
    if thr is None:
        return "（未配置）"
    return f"≥{thr}"


# 中文档位标签：减仓档用 减仓①②，清仓为单一最终档 清仓；加仓用 加仓①②③
CN_TIER_NUM = {1: '①', 2: '②', 3: '③', 4: '④'}
ADD_SLOT_LABEL = {'t1': '加仓①', 't2': '加仓②', 't3': '加仓③'}


def _emit_tier_lines(lines, rs, keys, cp, triggered_labels, is_clear=False,
                     tier_prefix='K', tier_ratio=None, start_idx=1, show_untriggered=True):
    """渲染减仓/清仓档触发状态行（持仓报告用，全部 live）。

    与加仓完全对称：中文档位标签（减仓①/减仓②/清仓） + 触发条件 + 价格对比 + 比例，一行闭环。
    rs: ctx.reduce_suggestion 字典；keys: ('tier1','tier2') 或 ('tier3',)
    tier_ratio: {tk: 比例} 来自用户在【模型配置】设定的减仓参数
    start_idx: 档位编号起始值——减仓段用 1，清仓段用 3，保持全局连续
    show_untriggered: True=同时展示未触发(⏳)行；False=仅展示已触发(✅)行（清仓板块用，未触发即不显示）
    """
    tier_ratio = tier_ratio or {}
    verb = "清仓" if is_clear else "减仓"
    for i, tk in enumerate(keys, start=start_idx):
        price = rs.get(tk, 0)
        raw_label = rs.get(tk + '_label', '')
        clean = _clean_trigger_label(raw_label)
        # reduce_suggestion 用 tier1/tier2/tier3 作价格键，reduce_params 用 t1/t2/t3 作比例键，需归一化映射
        rk = ('t' + tk[4:]) if tk.startswith('tier') else tk
        ratio = 1.0 if is_clear else (tier_ratio.get(rk, tier_ratio.get(tk, 0)) or 0)
        ratio_txt = "100%" if is_clear else f"{ratio*100:.0f}%"
        # 中文档位标签：减仓档→减仓①②，清仓档（单一最终档）→清仓
        if is_clear:
            tier_label = "清仓"
        else:
            tier_label = f"减仓{CN_TIER_NUM.get(i, str(i))}"
        if raw_label and raw_label in triggered_labels:
            cmp = f"（{price:.2f}→{cp:.2f}）" if price and price > 0 and cp > 0 else ""
            lines.append(f"    ✅ {tier_label}（{clean}）已触发{cmp} → {verb}当前持仓的 {ratio_txt}")
        elif show_untriggered:
            if price and price > 0 and cp > 0:
                drop = (cp - price) / cp * 100
                lines.append(f"    ⏳ {tier_label}（{clean}）未触发（触发价 {price:.2f}，当前 {cp:.2f}，"
                             f"需跌 {drop:.1f}% 触发）→ {verb}当前持仓的 {ratio_txt}")
            elif price and price > 0:
                lines.append(f"    ⏳ {tier_label}（{clean}）触发价 {price:.2f}（当前价无效）→ {verb}当前持仓的 {ratio_txt}")
            elif raw_label:
                lines.append(f"    ⏳ {tier_label}（{clean}）未触发（布尔条件，当前未满足）→ {verb}当前持仓的 {ratio_txt}")
            else:
                # 该档未配置触发条件：跳过不显示，避免「档位：未配置触发」冗余信息干扰决策
                continue
        else:
            # 仅展示已触发：未触发档位直接跳过（用户要求：未触发即不显示）
            continue


# ── 策略级触发判定（按 quant_model.json 的 entry_conditions + add_tiers）──
# 工具只把"用户在【模型配置】设定的策略规则"与"实时行情"做事实比对，
# 严格输出"已满足/未满足/否决项触发"状态与触发价，不出"建议买入/目标价"。
_TECH_SIGNAL_LABELS = {
    'none': '无要求',
    'bull_align': '多头排列',
    'above_ma5': '站上MA5',
    'above_ma20': '站上MA20',
    'above_ma60': '站上MA60',
    'kdj_gold': 'KDJ金叉',
    'macd_gold': 'MACD金叉',
    'ma_gold': '均线金叉',
    'adx_trend': 'ADX趋势确认',
    'volume_up': '放量上涨',
}


def _eval_one_tech_signal(signal_name, ctx):
    """实时行情 vs 技术门槛（可为单信号字符串 / 多条件 AND 列表），返回 (是否满足, 状态描述)。

    布尔判定统一委托 UnifiedEntryLogic._evaluate_tech_conditions —— 与自选股诊断 /
    check_entry / StockClassifier 共用同一份唯一实现，是「技术门槛可 AND」在报告层的唯一出口，
    杜绝双引擎分叉。此处仅生成中文明细用于报告展示。
    """
    tech = ctx.tech or {}
    market = ctx.market or {}
    cp = ctx.latest_price or 0
    adx_state = getattr(ctx, 'adx_state', {}) or {}
    if isinstance(signal_name, list):
        ok = UnifiedEntryLogic._evaluate_tech_conditions(signal_name, tech, market, cp, adx_state)
        det = []
        for c in signal_name:
            if isinstance(c, dict) and c.get('indicator'):
                cur = UnifiedEntryLogic._eval_indicator(str(c['indicator']), tech)
                lab = UnifiedEntryLogic._NUM_IND_LABELS.get(str(c['indicator']), str(c['indicator']))
                op = c.get('op', '>='); val = c.get('value', '')
                det.append(f'{lab}{op}{val}' + ('' if cur is None else f'（现{cur:.2f}）'))
            elif isinstance(c, dict) and c.get('signal'):
                sub = _eval_one_tech_signal(str(c['signal']), ctx)
                det.append(_tech_signal_detail(str(c['signal']), ctx, sub[0]))
            else:
                det.append(str(c))
        return ok, (' ∧ '.join(det) if ok else '；'.join(f'{d}不满足' for d in det))
    ok = UnifiedEntryLogic._evaluate_tech_conditions(signal_name, tech, market, cp, adx_state)
    return ok, _tech_signal_detail(signal_name, ctx, ok)


def _tech_signal_detail(signal_name, ctx, ok):
    """单条技术信号的可读明细（仅展示用；布尔以 _evaluate_tech_signal 为准）。"""
    tech = ctx.tech or {}
    market = ctx.market or {}
    cp = ctx.latest_price or 0
    if signal_name in ('none', ''):
        return '无要求（该档未配置技术信号）'
    if signal_name == 'bull_align':
        return '多头排列' if ok else '当前非多头排列'
    _MA = {'above_ma5': ('MA5', 'sma_5'), 'above_ma20': ('MA20', 'sma_20'),
           'above_ma60': ('MA60', 'sma_60')}
    if signal_name in _MA:
        lab, key = _MA[signal_name]
        v = tech.get(key, 0) or 0
        if v <= 0:
            return '数据不足'
        return (f'当前价 {cp:.2f} 站上{lab} {v:.2f}' if ok
                else f'当前价 {cp:.2f} 未站上{lab} {v:.2f}')
    if signal_name == 'kdj_gold':
        return 'KDJ金叉' if ok else 'KDJ非金叉'
    if signal_name == 'kdj_dead':
        return 'KDJ死叉' if ok else 'KDJ非死叉'
    if signal_name == 'macd_gold':
        return 'MACD金叉' if ok else 'MACD非金叉'
    if signal_name == 'macd_dead':
        return 'MACD死叉' if ok else 'MACD非死叉'
    if signal_name == 'ma_gold':
        return 'MA5>MA10' if ok else 'MA5≤MA10'
    if signal_name == 'ma_dead':
        return 'MA5<MA10' if ok else 'MA5≥MA10'
    if signal_name == 'bb_breakout':
        return '布林上轨突破' if ok else '未触布林上轨'
    if signal_name == 'bb_breakdown':
        return '布林下轨突破' if ok else '未触布林下轨'
    if signal_name == 'adx_trend':
        adx = (getattr(ctx, 'adx_state', {}) or {}).get('adx', 0)
        return f'ADX={adx:.0f}'
    if signal_name == 'volume_up':
        return '放量上涨' if ok else f'当前{market.get("volume_price") or "量能未达"}'
    return f'未识别信号({signal_name})'


def _eval_one_veto(level_key, ctx):
    """单级否决项判定（委托 UnifiedEntryLogic.check_level_veto 唯一实现）。"""
    hit, _, has = UnifiedEntryLogic.check_level_veto(level_key, ctx.tech or {}, with_suffix=False)
    return hit, has


def _evaluate_strategic_entry_lines(ctx):
    """按用户配置 entry_conditions 逐级判定建仓规则。

    返回 [(level_key, level_name, threshold, tech_signal_label, sig_ok, sig_detail,
           veto_hit, verdict, reason, position), ...]，verdict ∈ {hit/wait/rebound/none}.
    """
    # 初级用法（因子休眠）：不再用「触发指标 + 统一比例」，而是复用 UnifiedEntryLogic.check_entry
    # 的 basic 分支——按「状态分界」技术条件从高到低定档（强→标→探→望），命中哪档用该档比例。
    # 结果已写入 ctx.entry_action / entry_position / entry_reason，这里直接呈现，保证 决策结论文案 /
    # 信号明细 / 报告文本 一致；命中动作是「关注建仓/观望仓」，未命中为「空仓观望」。
    if get_usage_mode() == 'basic':
        _action = getattr(ctx, 'entry_action', '') or ''
        _reason = getattr(ctx, 'entry_reason', '') or ''
        _hit = _action in ('关注建仓', '观望仓')
        return [{
            'level_key': 'basic', 'level_name': '技术条件建仓', 'threshold': None,
            'tech_signal_name': 'basic', 'tech_signal_label': '技术条件',
            'sig_ok': _hit, 'sig_detail': _reason,
            'veto_hit': [], 'veto_real': False, 'score_ok': _hit,
            'verdict': 'hit' if _hit else 'wait',
            'reason': _reason if _hit else (_reason or '未满足任一技术档条件，空仓观望'),
            'position': float(getattr(ctx, 'entry_position', 0) or 0),
        }]

    thresholds = get_thresholds()
    positions = get_entry_params().get('positions', {})
    fs = getattr(ctx, 'final_score', 0) or 0
    stock_score = getattr(ctx, 'stock_score', fs) or 0
    up_ratio = getattr(ctx, 'up_ratio', 0) or 0
    adx_state = getattr(ctx, 'adx_state', {}) or {}

    levels = [
        ('strong',   '强势线',  thresholds.get('strong', 22),  positions.get('strong', 0.20)),
        ('standard', '标准线',  thresholds.get('standard', 20), positions.get('standard', 0.15)),
        ('test',     '试探线',  thresholds.get('test', 10),    positions.get('test', 0.08)),
        ('pending',  '观望线',  thresholds.get('pending', -5), positions.get('observe', 0.03)),
    ]
    out = []
    _mt = getattr(ctx, 'market_type', 'stock') or 'stock'
    for lk, lname, thr, pos in levels:
        # 应用环境门控（参考系数），与自选诊断 TradingPipeline.apply_gate 口径一致，
        # 避免决策报告显示配置原始仓位、自选诊断显示门控后仓位（如恐慌 8%→7%）的矛盾。
        # 期货走中性门控（不套 A 股全市场上涨家数系数）。
        gpos = MarketGate.apply_gate(pos, up_ratio, _mt)[0]
        ec = get_entry_conditions().get(lk, {})
        sig_name = ec.get('tech_signal', 'none')
        # list（多条件 AND 组合）：label 用统一中文描述（A ∧ B），不直接用 list 对象
        sig_label = (UnifiedEntryLogic._tech_conditions_display(sig_name)
                     if isinstance(sig_name, list) else _TECH_SIGNAL_LABELS.get(sig_name, sig_name))
        sig_ok, sig_detail = _eval_one_tech_signal(sig_name, ctx)
        veto_hit, veto_real = _eval_one_veto(lk, ctx)
        score_ok = fs >= thr
        thr_txt = _thr_text(thr)
        if lk == 'pending':
            if score_ok and sig_ok and not veto_real:
                verdict, reason = 'wait', f'得分{fs:.0f}{thr_txt} 且 {sig_detail}（观望仓 {gpos*100:.0f}%）'
            else:
                reasons = []
                if not score_ok:
                    reasons.append(f'得分{fs:.0f}<{thr}')
                if sig_name != 'none' and not sig_ok:
                    reasons.append(sig_detail)
                if veto_real:
                    reasons.append('否决：' + '、'.join(veto_hit))
                verdict, reason = 'none', '；'.join(reasons) or '观望线要求未满足'
        else:
            if score_ok and sig_ok and not veto_real:
                verdict, reason = 'hit', f'得分{fs:.0f}{thr_txt} 且 {sig_detail}（按您的方案设定，触发时计划仓位：总资金的 {gpos*100:.0f}%）'
            else:
                reasons = []
                if not score_ok:
                    reasons.append(f'得分{fs:.0f}<{thr}')
                if sig_name != 'none' and not sig_ok:
                    reasons.append(f'{sig_label}不满足（{sig_detail}）')
                if veto_real:
                    reasons.append('否决：' + '、'.join(veto_hit))
                verdict, reason = 'wait', '；'.join(reasons) or f'{lname}要求未满足'
        out.append({
            'level_key': lk, 'level_name': lname, 'threshold': thr,
            'tech_signal_name': sig_name, 'tech_signal_label': sig_label,
            'sig_ok': sig_ok, 'sig_detail': sig_detail,
            'veto_hit': veto_hit, 'veto_real': veto_real,
            'score_ok': score_ok, 'verdict': verdict, 'reason': reason,
            'position': gpos,
        })

    # 博反弹 / 恐慌反转：调 UnifiedEntryLogic._check_rebound，复用与 StockClassifier 同一套规则。
    # 恐慌市不再强制「回避」：恐慌只通过环境门控系数下调建议仓位；
    # 仅「恐慌反转」(is_panic 且 can_rebound) 作为用户显式配置的左侧抄底档保留。
    # 空单模式（direction=short）：博反弹/恐慌反转是抄底做多逻辑，空单视角负分区=多头强势区，
    # 语义完全错误 → 整体禁用（与 check_entry short_mode 一致），改以「顶部回落」档（见下）。
    is_short_ctx = (getattr(ctx, 'direction', 'long') == 'short')
    if not is_short_ctx:
        try:
            rb = UnifiedEntryLogic._check_rebound(ctx.tech or {}, stock_score, up_ratio)
        except Exception:
            rb = {'can_rebound': False, 'is_panic': False, 'reason': '反弹判定不可用'}
        t_rebound = thresholds.get('rebound', -2)
        t_panic = thresholds.get('panic_rebound', -8)
        is_panic = rb.get('is_panic', False)
        can_rebound = rb.get('can_rebound', False)
        if can_rebound:
            # 满足抄底条件：恐慌市→恐慌反转(极小仓)；非恐慌→普通博反弹
            out.append({
                'level_key': 'rebound',
                'level_name': '恐慌反转线' if is_panic else '博反弹线',
                'threshold': t_rebound,
                'tech_signal_name': 'reversal_score', 'tech_signal_label': '反转信号加权',
                'sig_ok': True, 'sig_detail': rb.get('reason', ''),
                'veto_hit': [], 'veto_real': False, 'score_ok': stock_score <= t_rebound,
                'verdict': 'rebound', 'reason': rb.get('reason', ''),
                'position': MarketGate.apply_gate(positions.get('panic_rebound' if is_panic else 'rebound', 0.05), up_ratio, _mt)[0],
            })
        elif stock_score <= t_rebound:
            out.append({
                'level_key': 'rebound', 'level_name': '博反弹线', 'threshold': t_rebound,
                'tech_signal_name': 'reversal_score', 'tech_signal_label': '反转信号加权',
                'sig_ok': False, 'sig_detail': rb.get('reason', '反转信号未达阈值'),
                'veto_hit': [], 'veto_real': False, 'score_ok': stock_score <= t_rebound,
                'verdict': 'none', 'reason': rb.get('reason', '未触发博反弹条件'),
                'position': 0,
            })
    else:
        # 空单模式专属「顶部回落」（涨势猛/超买 + 顶部反转信号 → 做空），_check_rebound 的镜像。
        # 与 check_entry(short_mode=True) 同一规则源，保证 决策报告 == 自选诊断 == 监控触发。
        try:
            tr = UnifiedEntryLogic._check_top_reversal(ctx.tech or {}, stock_score, up_ratio)
        except Exception:
            tr = {'can_top_reversal': False, 'reason': '顶部回落判定不可用'}
        t_top = thresholds.get('top_reversal', -22)
        tr_veto_hit, tr_veto_real = _eval_one_veto('top_reversal', ctx)
        if tr.get('can_top_reversal') and not tr_veto_real:
            out.append({
                'level_key': 'top_reversal', 'level_name': '顶部回落', 'threshold': t_top,
                'tech_signal_name': 'reversal_score', 'tech_signal_label': '顶部反转信号加权',
                'sig_ok': True, 'sig_detail': tr.get('reason', ''),
                'veto_hit': tr_veto_hit, 'veto_real': tr_veto_real,
                'score_ok': stock_score <= t_top,
                'verdict': 'hit', 'reason': tr.get('reason', ''),
                'position': MarketGate.apply_gate(positions.get('top_reversal', 0.05), up_ratio, _mt)[0],
            })
        elif stock_score <= t_top:
            reasons = []
            if not tr.get('can_top_reversal'):
                reasons.append(tr.get('reason', '顶部反转信号未达阈值'))
            if tr_veto_real:
                reasons.append('否决：' + '、'.join(tr_veto_hit))
            out.append({
                'level_key': 'top_reversal', 'level_name': '顶部回落', 'threshold': t_top,
                'tech_signal_name': 'reversal_score', 'tech_signal_label': '顶部反转信号加权',
                'sig_ok': bool(tr.get('can_top_reversal')), 'sig_detail': tr.get('reason', ''),
                'veto_hit': tr_veto_hit, 'veto_real': tr_veto_real,
                'score_ok': stock_score <= t_top,
                'verdict': 'none', 'reason': '；'.join(reasons) or '未触发顶部回落条件',
                'position': 0,
            })
    return out


def _fmt_unhit_reason(lv, fs):
    """未命中行的理由文案：分数已达标但有否决项/技术信号不满足时，明确披露
    『得分已达标，但被否决项挡住』，避免『阈值≥X未命中』这种信息不足的写法。
    仅对主建仓四档（强/标/试/望）生效；博反弹/恐慌等返回原始理由。
    """
    if lv.get('level_key') in ('strong', 'standard', 'test', 'pending') and lv.get('score_ok'):
        parts = []
        if lv.get('veto_real'):
            parts.append('否决项' + '、'.join(f'"{v}"' for v in (lv.get('veto_hit') or [])))
        sig_lbl = lv.get('tech_signal_label') or ''
        if sig_lbl and sig_lbl != 'none' and not lv.get('sig_ok'):
            parts.append(f'否决项"{sig_lbl}"不满足（{lv.get("sig_detail", "")}）')
        if parts:
            return f"得分{fs:.0f}已达标，但{'；'.join(parts)}"
    return lv.get('reason', '')


def _evaluate_add_tiers_lines(ctx):
    """按用户配置 add_tiers（股票类型×三档）逐档判定加仓条件。

    直接复用 add_engine.suggest_add 的权威逐档判定（tier_details），
    不再自行重算——避免报告层判定与引擎分叉、以及对未识别触发类型
    默认判为满足的 bug（见 2026-07-30 持仓报告「加仓全触发」根因）。

    返回 {'stock_type_key', 'row_key', 'row_name', 'tiers': [t1, t2, t3]}，
    每档 {slot, trigger_label, ratio, is_met, detail}。
    """
    from engine.add_engine import AddEngine
    add = getattr(ctx, 'add_suggestion', None) or {}
    if not add.get('tier_details'):
        try:
            add = AddEngine.suggest_add(ctx)
        except Exception:
            # 防御性兜底：引擎异常时加仓板块按"未配置/未触发"安全渲染，不报错
            return {'stock_type_key': '', 'row_key': '', 'row_name': '未启用',
                    'tiers': [], 'note': '加仓引擎调用异常，板块跳过'}
    td = {d.get('slot'): d for d in add.get('tier_details', [])}
    tiers = []
    for slot in ('t1', 't2', 't3'):
        d = td.get(slot)
        if not d:
            tiers.append({'slot': slot, 'trigger_label': '未配置触发', 'ratio': 0,
                          'is_met': False, 'detail': '该档未配置触发条件'})
            continue
        fired = bool(d.get('fired'))
        label = d.get('label') or ''
        tiers.append({
            'slot': slot,
            'trigger_label': label or '未触发',
            'ratio': d.get('ratio', 0) or 0,
            'is_met': fired,
            'detail': label if fired else '未满足',
        })
    return {
        'stock_type_key': getattr(ctx, 'stock_type', '') or getattr(ctx, 'stock_type_key', ''),
        'row_key': '',
        'row_name': '加仓（按引擎逐档判定）',
        'tiers': tiers,
    }


class ReportBuilder:
    """报告构建器 v8.3

    持仓报告（7板块）：
    1. 今日操作 - 止损/减仓触发判断（硬约束优先）
    2. 持仓评估 - 状态（因子预期→状态分类，持仓阶段不暴露具体分数）
    3. 决策依据 - 因子贡献直白解释
    4. 多空信号 - 技术指标 + 筹码/斐波那契/枢轴
    5. 条件预案 - MA触发条件 + 止损档位
    6. 幽灵规则 - 时间+PnL验证
    7. 冲突裁决 - 止损vs幽灵冲突时自动说明

    空仓决策报告（6板块）：
    1. 今日操作 - 状态驱动决策（关注/观望/博反弹/回避）
    2. 因子预期分 - 能否建仓（状态）+ 因子预期强度
    3. 决策依据 - 因子贡献直白解释
    4. 多空信号 - 技术指标 + 筹码/斐波那契/枢轴
    5. 关键价位 - 止损/止盈/支撑/压力
    6. 建仓条件 - 什么情况下建仓更合理
    """

    @staticmethod
    def build(ctx):
        """构建完整报告"""
        has_position = ctx.has_position and ctx.entry_price > 0

        # 单一决策真相源：经流水线构造的 ctx 已带 decision；非流水线路径（如测试/回测）惰性兜底
        if getattr(ctx, 'decision', None) is None:
            ctx.decision = compute_decision(ctx)

        lines = []

        # ========== 报告头部 ==========
        lines.append("╔" + "═" * 78)
        stock_name = getattr(ctx, 'stock_name', '') or ctx.stock_code
        is_short = has_position and getattr(ctx, 'direction', 'long') == 'short'
        report_type = ("【持仓管理报告·空单】" if is_short else "【持仓管理报告】") if has_position else "【决策报告】"
        lines.append(f"║  📊 {stock_name}（{ctx.stock_code}）  {report_type}")
        fresh = ReportBuilder._data_freshness(ctx)
        if not getattr(ctx, 'breadth_fetched', False):
            lines.append(f"║  {ctx.query_time.strftime('%Y-%m-%d %H:%M')}  📡 {fresh}  ⚠️操作信号均按您自己在【模型配置】中设定的规则计算，结果仅供参考，是否操作由您自行决定 涨跌家数未获取，环境按中性默认（0.5），如需精确环境判定，请在顶部「涨家数/跌家数」输入框填写")
        else:
            lines.append(f"║  {ctx.query_time.strftime('%Y-%m-%d %H:%M')}  📡 {fresh}  操作信号均按您自己在【模型配置】中设定的规则计算，结果仅供参考，是否操作由您自行决定")
        lines.append("╠" + "═" * 78)

        # 顶部核心结论面板（状态徽章 / 风险看板）
        lines.extend(ReportBuilder._render_summary_board(ctx, has_position))

        if has_position:
            lines.extend(ReportBuilder._render_management(ctx))
        else:
            lines.extend(ReportBuilder._render_decision(ctx))

        # 动态仓位（连续状态机）结果展示
        lines.extend(ReportBuilder._render_dynamic_position(ctx))

        lines.append("")
        lines.append("╚" + "═" * 78)
        if not has_position:
            lines.append("")
            lines.append("  ────────────────────────────────")
            lines.append("  输入持仓价后，展开完整持仓管理报告")
            lines.append("  ────────────────────────────────")
        return "\n".join(lines)

    # ============================================================
    # 顶部核心结论面板（状态徽章 / 风险看板）
    # ============================================================
    @staticmethod
    def _data_freshness(ctx):
        """数据时效标签：根据查询时间判断盘中/盘后/休市/非交易日。

        优先判断周末（A 股仅周一至周五交易），再判断盘中时段。
        注：法定节假日未内置日历，周六/周日可准确识别，节假日需另行判定。
        """
        try:
            t = ctx.query_time
            if t.weekday() >= 5:  # 5=周六, 6=周日
                return "非交易日（休市）"
            hm = t.hour * 60 + t.minute
            if (9 * 60 + 30 <= hm <= 11 * 60 + 30) or (13 * 60 <= hm <= 15 * 60):
                return "盘中实时"
            if hm > 15 * 60 or hm < 9 * 60 + 30:
                return "盘后"
            return "午间休市"
        except Exception:
            return "实时"

    @staticmethod
    def _render_summary_board(ctx, has_position):
        """顶部核心结论面板（两份报告通用，用户 3 秒内知道该做什么）。

        - 空仓：建仓信号状态徽章，展示建仓条件当前触发状态。
        - 持仓：红黄绿风险看板，直观标识最紧急风险，并附建仓转换上下文。
        - 仓位上限统一读取模型配置「最大单只仓位」（max_single_position），不读内置环境档位常量。
        """
        lines = []
        env = ctx.env_config or {}
        env_label = env.get('label', '未知')
        lines.append("  ▌【核心结论】")

        if not has_position:
            # 核心结论直接读 ctx.decision（单一真相源，由 compute_decision 计算）
            lines.append(f"  {ctx.decision.badge_text}")
        else:
            rs = getattr(ctx, 'reduce_suggestion', None) or {}
            risk = get_risk_params()
            ghost = getattr(ctx, 'ghost_result', None) or {}
            bars = ghost.get('bars_held', 0) or 0
            pnl = getattr(ctx, 'pnl_pct', 0) or 0
            max_pnl = ghost.get('max_pnl_pct', 0) or 0
            tsd = risk.get('time_stop_days', 0) or 0
            tsmp = risk.get('time_stop_min_profit_pct', 0) or 0
            sml = risk.get('single_max_loss_pct', 0) or 0

            # 硬清仓条件（时间止损 / 单笔最大亏损）先于风险看板计算，确保看板与核心结论口径一致
            _hard_clear = (
                (tsd > 0 and bars >= tsd and pnl < tsmp * 100)   # 时间止损
                or (sml > 0 and pnl <= -sml * 100)               # 单笔最大亏损
            )
            _is_clear_tier = (rs.get('reduce_ratio', 0) or 0) >= 1.0  # 清仓档（reduce_total 已置 1.0）

            board = []
            # 时间止损（硬约束）
            if tsd > 0:
                if bars >= tsd and pnl < tsmp * 100:
                    board.append(('🔴', f'时间止损已触发（持{bars}天≥{tsd}天 且 收益{pnl:+.1f}%<{tsmp*100:.0f}%）'))
                elif bars >= tsd:
                    board.append(('🟡', f'时间止损观察期满（持{bars}天），收益达标未触发'))
                else:
                    board.append(('🟢', f'时间止损未到（持{bars}/{tsd}天）'))
            # 减仓 / 清仓：硬清仓 or 清仓档触发时，风险看板统一显示“清仓档已触发”，
            # 与核心结论“清仓”保持一致；减仓档位明细留在下方详细板块展示。
            rr = rs.get('reduce_ratio', 0) or 0
            triggered_tiers = rs.get('triggered_tiers', []) or []
            if _hard_clear or _is_clear_tier:
                board.append(('🔴', '清仓档已触发'))
            elif rr > 0 and triggered_tiers:
                # 与详细减仓板块同口径：按真实档位(tier1/tier2)比对 label，避免只用列表位置
                # 导致"仅 tier2 触发"时被误标成 减仓①（核心结论与详细信号矛盾，见 2026-07-28）。
                triggered_labels = {t.get('label', '') for t in triggered_tiers}
                _parts = []
                for _i, _tk in enumerate(('tier1', 'tier2'), start=1):
                    _lab = rs.get(_tk + '_label', '')
                    if _lab and _lab in triggered_labels:
                        _parts.append(f'减仓{CN_TIER_NUM.get(_i, str(_i))}档已触发 （{_lab}）')
                if not _parts:
                    # 触发档不在 tier1/tier2（如仅清仓档，但清仓已由上方分支处理），
                    # 退化为按比例汇总，避免与详细信号打架
                    _parts.append(f'减仓档已触发（触发比例 {rr*100:.0f}%，是否执行由您自行决定）')
                board.append(('🟠', '；'.join(_parts)))
            elif rr > 0:
                board.append(('🟠', f'减仓档已触发（触发比例 {rr*100:.0f}%，是否执行由您自行决定）'))
            else:
                board.append(('🟢', '减仓条件未触发'))
            # 单笔最大亏损
            if sml > 0:
                if pnl <= -sml * 100:
                    board.append(('🔴', f'单笔最大亏损已触发（浮亏{pnl:.1f}%≤-{sml*100:.0f}%）'))
                else:
                    board.append(('🟢' if pnl >= 0 else '🟡', f'单笔亏损未触限（{"浮盈" if pnl >= 0 else "浮亏"}{pnl:+.1f}%/-{sml*100:.0f}%）'))
            # 动作结论：根据触发的具体档位生成（清仓 / 减仓当前持仓的 X% / 加仓当前持仓的 X% / 正常持有）
            # 注意：reduce_engine 在 triggered_tiers 里把减仓档 action 写成「减仓25%」展示串、清仓档写成「清仓」，
            # 故减仓判断不能依赖 action=='reduce'，统一用 rr（reduce_ratio）区分：>=1.0 为清仓档，0<rr<1.0 为部分减仓。
            # 放在【核心结论】之后、风险看板之前；不输出"行动参考"标签，仅给动作结论。
            if _hard_clear or _is_clear_tier:
                action_text = '触发清仓参考档（清仓）：清仓当前持仓的 100%'
            elif rr > 0:
                _reduce_tiers = [t for t in triggered_tiers if t.get('action') != '清仓']
                _n = len(_reduce_tiers)
                _slots = '、'.join(f'减仓{CN_TIER_NUM.get(i, str(i))}' for i in range(1, _n + 1))
                action_text = f'触发减仓参考档（{_slots}）：减仓当前持仓的 {rr*100:.0f}%'
            else:
                # 无减/清触发：检查加仓档是否命中
                try:
                    _ae = _evaluate_add_tiers_lines(ctx)
                    _ah = [t for t in _ae.get('tiers', []) if t.get('is_met')]
                    if _ah:
                        _at = sum(t.get('ratio', 0) for t in _ah)
                        _slots = '、'.join(ADD_SLOT_LABEL.get(t.get('slot', ''), t.get('slot', '').upper())
                                          for t in _ah)
                        action_text = f'触发加仓参考档（{_slots}）：加仓当前持仓的 {_at*100:.0f}%'
                    else:
                        action_text = '正常持有'
                except Exception:
                    action_text = '正常持有'
            lines.append(f"  {action_text}")
            lines.append(f"  🚦 风险看板：")
            for icon, txt in board:
                lines.append(f"    {icon} {txt}")
            # 建仓转换上下文（持仓报告）
            entry = ctx.entry_price
            held = ghost.get('bars_held', getattr(ctx, 'bars_held', 0)) or 0
            lines.append(f"  📌 建仓价：{entry:.2f} ｜ 持有：{held} 天")

        # 仓位上限/环境已移到【因子预期分】（空仓）或【持仓评估】（持仓）板块展示，避免重复
        return lines

    # ============================================================
    # 动态仓位（连续状态机）结果展示
    # ============================================================
    @staticmethod
    def _render_dynamic_position(ctx):
        """渲染动态仓位管理的连续状态机输出（与离散建仓/加仓/减仓解耦）。

        正常路径 ctx.dynamic_position 为 step() 返回的 dict；
        异常路径为带 error 键的兜底 dict。
        """
        dp = getattr(ctx, 'dynamic_position', None)
        if not dp or not isinstance(dp, dict):
            return []

        lines = [""]
        lines.append("  【动态仓位（连续状态机）】")

        if dp.get('error'):
            lines.append(f"  ⚠️ 动态仓位未执行：{dp.get('error')}")
            lines.append(f"  区制：{dp.get('regime', 'NEUTRAL(中性)')}  "
                         f"目标权重：{float(dp.get('target_weight', 0.0))*100:.1f}%")
            return lines

        lines.append(f"  区制：{dp.get('regime', '-')}")
        lines.append(f"  目标权重 {float(dp.get('target_weight', 0))*100:.1f}%  "
                     f"→  步进权重 {float(dp.get('step_weight', 0))*100:.1f}%")
        lines.append(f"  信号强度 {float(dp.get('signal_strength', 0)):.2f} ｜ "
                     f"置信度 {float(dp.get('confidence', 0)):.2f}（{dp.get('confidence_source', '-')}）")
        lines.append(f"  三维调节：账户回撤 g={float(dp.get('g_dd', 0)):.2f} ｜ "
                     f"市场波动 h={float(dp.get('h_vol', 0)):.2f} ｜ "
                     f"信号质量 k={float(dp.get('k_ic', 0)):.2f}")
        # 自适应阈值行的「博反弹」档：空单模式语义为顶部回落（涨势猛/超买 → 做空），
        # 与配置面板/入场逻辑一致；多单/A股保持原「博反弹」。值优先取对应档阈值。
        is_short = getattr(ctx, 'direction', 'long') == 'short'
        if is_short:
            _reb_label = '顶部回落'
            _reb_val = float(dp.get('top_reversal_threshold', dp.get('rebound_threshold', 0)))
        else:
            _reb_label = '博反弹'
            _reb_val = float(dp.get('rebound_threshold', 0))
        lines.append(f"  自适应阈值：恐慌≤{float(dp.get('panic_threshold', 0)):.1f} ｜ "
                     f"弱势≤{float(dp.get('weak_threshold', 0)):.1f} ｜ "
                     f"{_reb_label}≤{_reb_val:.1f}")
        if dp.get('warmup'):
            lines.append("  （预热期：评分历史不足，阈值使用回退固定值）")
        return lines

    # ============================================================
    # 无持仓报告（决策报告）
    # ============================================================
    @staticmethod
    def _render_operation_reference(ctx):
        """统一的『操作参考』板块——按是否输入持仓价分两类：

        - 无持仓（决策报告）：只管理【建仓】——把实时行情与您配置的建仓条件比对，列出
          触发状态与价格；加仓/减仓/清仓/止损/止盈/风控 标注为持仓后生效的预案。
        - 有持仓（持仓报告）：管理【加仓 / 减仓 / 清仓 / 止损 / 止盈 / 风控】全部 live，
          按您配置的触发价与当前价逐一比对；建仓标 N/A。

        合规要点：条件/阈值均来自用户在【模型配置】设定的参数，工具仅比对播报触发状态
        与价格，不输出『建议买入/建议减仓/目标价』等买卖建议。
        """
        lines = [""]
        lines.append("")

        cp = ctx.latest_price
        has_pos = ctx.has_position and ctx.entry_price > 0

        # ===================== 决策报告：管理【建仓】 =====================
        if not has_pos:
            lines.append("  ▌当前信号触发情况")
            entry_levels = ctx.decision.entry_levels
            # 建仓判定完全按用户配置的 强/标/试/望/博反弹（恐慌反转）逐档；
            # 恐慌市只通过环境门控系数下调建议仓位（如 8%→6.8%），不再改写类型或硬阻断入场。
            # 高等级触发时覆盖低等级，仅展示命中档及其以上未命中档。
            hit_idx = None
            for i, lv in enumerate(entry_levels):
                if lv['verdict'] == 'hit':
                    hit_idx = i
                    break
            display_levels = entry_levels[:hit_idx + 1] if hit_idx is not None else entry_levels
            for lv in display_levels:
                if lv['verdict'] == 'hit':
                    lines.append(f"    ✅ 当前命中『{lv['level_name']}』：{lv['reason']}")
                elif lv['verdict'] == 'rebound':
                    lines.append(f"    ✅ {lv['level_name']}：{lv['reason']}（按您的方案设定，触发时计划仓位：总资金的 {lv['position']*100:.0f}%）")
                elif lv['verdict'] == 'wait':
                    if lv['level_key'] == 'pending' and lv['score_ok']:
                        lines.append(f"    ⏳ 当前满足『{lv['level_name']}』：{lv['reason']}")
                    else:
                        lines.append(f"    ⏳ {lv['level_name']}未命中：{_fmt_unhit_reason(lv, ctx.final_score)}")
                else:  # verdict ∈ {'none'}
                    lines.append(f"    ⏳ {lv['level_name']}未命中：{_fmt_unhit_reason(lv, ctx.final_score)}")
            # 大盘熔断暂停（据配置的基准指数当日涨跌幅判定，与回测口径一致）
            lines.extend(_circuit_breaker_lines(get_risk_params()))
            return lines

        # ============== 持仓报告：管理【加仓/减仓/清仓/止损/止盈/风控】 ==============
        rs = getattr(ctx, 'reduce_suggestion', None) or {}
        add = getattr(ctx, 'add_suggestion', None) or {}
        triggered_labels = {t.get('label', '') for t in rs.get('triggered_tiers', [])}
        risk = get_risk_params()
        entry = ctx.entry_price
        pnl = getattr(ctx, 'pnl_pct', 0) or 0
        is_short = getattr(ctx, 'direction', 'long') == 'short'
        ghost = getattr(ctx, 'ghost_result', None) or {}
        bars = ghost.get('bars_held', 0) or 0
        max_pnl = ghost.get('max_pnl_pct', 0) or 0

        # 读取减仓档比例（用户在【模型配置】设定），供对称展示用
        rp = get_reduce_params()
        if rp is None:
            tier_ratio = {'t1': 0.2, 't2': 0.2, 't3': 1.0}
        else:
            rrow = rp.get('type_to_row', {}).get(getattr(ctx, 'stock_type', ''), rp.get('default_row', 'strong_standard'))
            rcfg = rp.get('reduce_tiers', {}).get(rrow, {})
            tier_ratio = {tk: float((rcfg.get(tk) or {}).get('ratio', 0.2)) for tk in ('t1', 't2', 't3')}

        # 硬止损价：以系统在 trading_pipeline 设定的 ctx.stop_loss 为准（与【清仓】板块保持一致）
        # 空头反向：成本×(1+单笔最大亏损%)，涨破触发
        sml = risk.get('single_max_loss_pct', 0) or 0
        hard_sl = getattr(ctx, 'stop_loss', 0) or 0
        if hard_sl <= 0 and sml > 0 and entry > 0:
            hard_sl = entry * (1 + sml) if is_short else entry * (1 - sml)

        # ── 风控（硬约束，最优先；止损本质即减仓/清仓）──
        lines.append("  ▌风控（硬约束，最优先）")
        tsd = risk.get('time_stop_days', 0) or 0
        tsmp = risk.get('time_stop_min_profit_pct', 0) or 0
        if tsd > 0:
            if bars >= tsd:
                if pnl < tsmp * 100:
                    lines.append(f"    ✅ 时间止损：已持有 {bars} 天 ≥ {tsd} 天 且 收益 {pnl:+.1f}% < 目标 {tsmp*100:.0f}% → 清仓离场")
                else:
                    lines.append(f"    ⏳ 时间止损：已持有 {bars} 天 ≥ {tsd} 天，但收益 {pnl:+.1f}% ≥ 目标 {tsmp*100:.0f}%，继续持有")
            else:
                lines.append(f"    ⏳ 时间止损：已持有 {bars} 天 / 阈值 {tsd} 天（到阈值且收益未达目标即清仓）")
        if sml > 0:
            sl_txt = f"（硬止损 {hard_sl:.2f}）" if hard_sl > 0 else ""
            if pnl <= -sml * 100:
                lines.append(f"    ✅ 单笔最大亏损{sl_txt}：浮亏 {pnl:.1f}% ≤ -{sml*100:.0f}% → 触发即清仓")
            else:
                lines.append(f"    ⏳ 单笔最大亏损{sl_txt}：{'浮盈' if pnl >= 0 else '浮亏'} {pnl:.1f}% / 阈值 -{sml*100:.0f}%（未触及，触及即清仓）")
        # 注：持仓详细风控板块不展示「大盘熔断」行——熔断门控文案本就只针对"开新仓"，
        # 对已在手的持仓加仓/减仓/清仓无 gate 意义（见 2026-07-28 FINDING 3 方案①）。
        lines.append("")

        # 硬清仓条件（与核心结论 / 风险看板口径一致）
        tsd = risk.get('time_stop_days', 0) or 0
        tsmp = risk.get('time_stop_min_profit_pct', 0) or 0
        sml = risk.get('single_max_loss_pct', 0) or 0
        _hard_clear = (
            (tsd > 0 and bars >= tsd and pnl < tsmp * 100)   # 时间止损
            or (sml > 0 and pnl <= -sml * 100)               # 单笔最大亏损
        )
        _is_clear_tier = (rs.get('reduce_ratio', 0) or 0) >= 1.0  # 配置清仓档触发

        # ── 减仓（部分减仓）──
        # 硬清仓或配置清仓档触发时，跳过减仓明细，避免与"清仓"结论冲突。
        # 清仓板块会单独展示最终离场原因（配置清仓档 / 硬止损 / 时间止损 / 单笔最大亏损）。
        if not (_hard_clear or _is_clear_tier):
            # 直接从 triggered_tiers 渲染，避免把 action='清仓' 的档位误标成"减仓② 100%"，
            # 也避免 tier1/tier2 位置与真实触发档位错位。
            _reduce_tiers = [t for t in rs.get('triggered_tiers', []) if t.get('action') != '清仓']
            _reduce_lines = []
            for i, t in enumerate(_reduce_tiers, start=1):
                price = float(t.get('price', 0) or 0)
                label = t.get('label', '')
                clean = _clean_trigger_label(label)
                ratio = float(t.get('ratio', 0) or 0)
                cmp = f"（{price:.2f}→{cp:.2f}）" if price > 0 and cp > 0 else ""
                tier_label = f"减仓{CN_TIER_NUM.get(i, str(i))}"
                _reduce_lines.append(
                    f"    ✅ {tier_label}（{clean}）已触发{cmp} → 减仓当前持仓的 {ratio*100:.0f}%"
                )
            if _reduce_lines:
                lines.append("  ▌减仓")
                lines.extend(_reduce_lines)
            lines.append("")

        # ── 清仓（最终离场 = 触发即清仓；来源可能是配置清仓档 / 硬止损 / 时间止损 / 单笔最大亏损）──
        _clear_tiers = [t for t in rs.get('triggered_tiers', []) if t.get('action') == '清仓']
        _clear_lines = []
        for t in _clear_tiers:
            price = float(t.get('price', 0) or 0)
            label = t.get('label', '')
            clean = _clean_trigger_label(label)
            cmp = f"（{price:.2f}）" if price > 0 else ""
            _clear_lines.append(f"    ✅ 清仓（{clean}）已触发{cmp} → 清仓 100%")
        # 无配置清仓档触发时，再用硬止损/时间止损/单笔最大亏损兜底展示清仓原因
        if not _clear_tiers and hard_sl > 0:
            # 空头涨破硬止损触发，多头跌破触发
            if (cp >= hard_sl) if is_short else (cp <= hard_sl):
                _clear_lines.append(f"    ✅ 清仓（硬止损/单笔最大亏损）已触发（{hard_sl:.2f}） → 清仓 100%")
            elif tsd > 0 and bars >= tsd and pnl < tsmp * 100:
                _clear_lines.append(f"    ✅ 清仓（时间止损已触发）已持有 {bars} 天 ≥ {tsd} 天 且 收益 {pnl:+.1f}% < 目标 {tsmp*100:.0f}% → 清仓 100%")
            elif sml > 0 and pnl <= -sml * 100:
                _clear_lines.append(f"    ✅ 清仓（单笔最大亏损已触发）浮亏 {pnl:.1f}% ≤ -{sml*100:.0f}% → 清仓 100%")
            # 未触发时不展示（用户要求：未触发即不显示；硬止损安全网仍在「风控」板块展示）
        if _clear_lines:
            lines.append("  ▌清仓")
            lines.extend(_clear_lines)
        lines.append("")

        # ── 加仓（紧迫性最低；仅展示已命中档位，未触发即不展示）──
        add_eval = _evaluate_add_tiers_lines(ctx)
        hits = [t for t in add_eval['tiers'] if t.get('is_met')]
        if hits:
            lines.append("  ▌加仓（按您在【模型配置】设定的加仓档位）")
            for t in hits:
                lines.append(f"    ✅ {ADD_SLOT_LABEL.get(t['slot'], t['slot'].upper())}：{t['trigger_label']} 已满足（{t['detail']}）"
                             f" → 加仓 {t['ratio']*100:.0f}%")
        lines.append("")

        # ── 关键价位（持仓版：与决策报告对齐用支撑/压力；额外展示持仓→当前盈亏）──
        lines.append("  【关键价位】")
        cp = ctx.latest_price
        if ctx.support > 0 or ctx.resistance > 0:
            parts = [f"持仓 {entry:.2f} → 当前 {cp:.2f}（{pnl:+.1f}%）"]
            if ctx.resistance > 0:
                up = (ctx.resistance - cp) / cp * 100 if cp else 0
                parts.append(f"↑压力 {ctx.resistance:.2f}（{up:+.1f}%）")
            if ctx.support > 0:
                down = (ctx.support - cp) / cp * 100 if cp else 0
                parts.append(f"↓支撑 {ctx.support:.2f}（{down:+.1f}%）")
            lines.append("    " + " ｜ ".join(parts))
        else:
            lines.append(f"    持仓 {entry:.2f} → 当前 {cp:.2f}（{pnl:+.1f}%）")
            lines.append("    ℹ️ 无支撑/压力关键价位")
        lines.append("")
        return lines

    @staticmethod
    def _render_decision(ctx):
        """无持仓决策报告：按板块编排。"""
        lines = []
        lines.extend(ReportBuilder._render_operation_reference(ctx))
        lines.extend(ReportBuilder._rd_factor_score(ctx))
        lines.append("")
        # 初级用法（因子休眠）：因子决策依据/技术共振属因子体系，跳过，仅保留技术信号主体
        _basic = get_usage_mode() == 'basic'
        if not _basic:
            lines.extend(_build_factor_basis_lines(ctx))
            lines.extend(_build_tech_resonance_lines(ctx))
        ReportBuilder._rd_signals(ctx, lines)
        lines.extend(ReportBuilder._rd_key_prices(ctx))
        # 幽灵规则不再单独成板块（item 11）：空仓场景无持仓，核心结论已在持仓报告的「决策冲突」板块验证
        return lines

    @staticmethod
    def _rd_factor_score(ctx):
        lines = [""]
        env_label = ctx.env_config.get('label', '未知') if ctx.env_config else '未知'
        _ms_raw = get_risk_params().get('max_single_position')
        _ms_text = f"{_ms_raw*100:.0f}%" if isinstance(_ms_raw, (int, float)) else "未设定"
        # 初级用法（因子休眠）：不展示因子预期分/股票类型，仅保留环境与仓位上限（技术分析主体保留）
        if get_usage_mode() == 'basic':
            lines.append(f"  【技术信号参考】环境：{env_label} ｜ 仓位上限：{_ms_text}")
            return lines
        score = ctx.final_score
        type_label = ctx.type_label or ''
        # 单行：因子预期分（股票类型）｜ 环境 仓位上限
        head = "【因子预期分】"
        if getattr(ctx, 'direction', 'long') == 'short':
            head += f"{score:.0f}分（空单视角，{type_label}）｜ 环境：{env_label} ｜ 仓位上限：{_ms_text}"
        else:
            head += f"{score:.0f}分（{type_label}）｜ 环境：{env_label} ｜ 仓位上限：{_ms_text}"
        lines.append("  " + head)
        return lines

    @staticmethod
    def _rd_signals(ctx, lines):
        lines.append("")
        lines.append("  【多空信号】")
        lines.extend(_build_tech_signals(ctx))
        chip = ctx.chip_signal
        if chip:
            low = chip.get('dense_zone_low', 0)
            high = chip.get('dense_zone_high', 0)
            position = chip.get('position', '')
            if low > 0 and high > 0 and low < high:
                if position == '上方':
                    lines.append(f"  ✅ 筹码：多数人成本在{low:.2f}-{high:.2f}，抛压小，支撑有效")
                elif position == '下方':
                    lines.append(f"  ❌ 筹码：多数人成本在{low:.2f}-{high:.2f}，抛压重，涨到{high:.2f}附近会很难")
                else:
                    lines.append(f"  ✅ 筹码：多数人成本在{low:.2f}-{high:.2f}，震荡消化中")
        fib = ctx.fib_signal
        if fib:
            direction = fib.get('direction', '')
            level = fib.get('current_level', 0.5)
            high = fib.get('high', 0)
            low = fib.get('low', 0)
            if direction and high > 0 and low > 0:
                if direction == '上涨波段':
                    if level <= 0.236:
                        lines.append(f"  ✅ 斐波那契：从{low:.2f}涨到{high:.2f}，仅回调{level*100:.1f}%，极强势")
                    elif level <= 0.382:
                        lines.append(f"  ✅ 斐波那契：从{low:.2f}涨到{high:.2f}，回调到{level*100:.1f}%，正常回踩")
                    elif level <= 0.618:
                        lines.append(f"  ⚠️ 斐波那契：从{low:.2f}涨到{high:.2f}，回调到{level*100:.1f}%，关注支撑")
                    else:
                        lines.append(f"  ❌ 斐波那契：从{low:.2f}涨到{high:.2f}，深度回调到{level*100:.1f}%，趋势存疑")
                elif direction == '下跌波段':
                    if level >= 0.618:
                        lines.append(f"  ✅ 斐波那契：从{high:.2f}跌到{low:.2f}，反弹到{level*100:.1f}%，强势反弹")
                    elif level >= 0.382:
                        lines.append(f"  ⚠️ 斐波那契：从{high:.2f}跌到{low:.2f}，反弹到{level*100:.1f}%，关注压力")
                    elif level > 0:
                        lines.append(f"  ❌ 斐波那契：从{high:.2f}跌到{low:.2f}，仅反弹{level*100:.1f}%，多头很弱")
                    else:
                        lines.append(f"  ❌ 斐波那契：从{high:.2f}跌到{low:.2f}，几乎未反弹，多头极弱")
        pivot = ctx.pivot_signal
        if pivot:
            pivot_price = pivot.get('pivot', 0)
            is_bullish = pivot.get('is_bullish', False)
            s1 = pivot.get('s1', 0)
            r1 = pivot.get('r1', 0)
            if pivot_price > 0:
                if is_bullish:
                    lines.append(f"  ✅ 枢轴点：今日多空分界线在{pivot_price:.2f}，今天偏多，第一阻力在{r1:.2f}")
                else:
                    lines.append(f"  ❌ 枢轴点：今日多空分界线在{pivot_price:.2f}，今天偏空，第一支撑在{s1:.2f}")
        lines.append("")
        # 综合结论统一调用 _signals_conclusion（与 web_api 共用同一映射）；
        # verdict 取自 ctx.decision（单一真相源），与顶部核心结论天然一致。
        signal_text = chr(10).join(lines[-20:])
        bull_count = signal_text.count('✅')
        bear_count = signal_text.count('❌')
        icon, phrase = _signals_conclusion(ctx.decision.verdict, bull_count, bear_count, False, False,
                                          ctx.decision.hit_line_name, ctx.decision.position,
                                          direction=getattr(ctx, 'direction', 'long') or 'long')
        lines.append(f"  {icon} 综合：{phrase}")

    @staticmethod
    def _rd_key_prices(ctx):
        lines = [""]
        lines.append("  【关键价位】")
        # 展示当前价到支撑/压力的百分比距离，一眼看出空间
        cp = ctx.latest_price
        if ctx.support > 0 or ctx.resistance > 0:
            if cp > 0:
                parts = [f"当前 {cp:.2f}"]
                if ctx.resistance > 0:
                    up = (ctx.resistance - cp) / cp * 100
                    parts.append(f"↑压力 {ctx.resistance:.2f}（{up:+.1f}%）")
                if ctx.support > 0:
                    down = (ctx.support - cp) / cp * 100
                    parts.append(f"↓支撑 {ctx.support:.2f}（{down:+.1f}%）")
                lines.append("    " + " ｜ ".join(parts))
            else:
                if ctx.support > 0:
                    lines.append(f"    支撑：{ctx.support:.2f}（观察点）")
                if ctx.resistance > 0:
                    lines.append(f"    压力：{ctx.resistance:.2f}（突破关注）")
        else:
            lines.append("    ℹ️ 无关键价位")
        return lines

    @staticmethod
    def _render_management(ctx):
        """有持仓管理报告：按板块编排。triggered 由减仓建议推导后传给后续板块。"""
        lines = []
        rs = getattr(ctx, 'reduce_suggestion', None)
        triggered = rs.get('triggered_tiers', []) if rs else []
        lines.extend(ReportBuilder._rm_eval(ctx))
        lines.extend(ReportBuilder._render_operation_reference(ctx))
        lines.append("")
        _basic = get_usage_mode() == 'basic'
        # 初级用法（因子休眠）：因子决策依据/技术共振/因子分/股票类型均不展示，仅保留技术信号主体
        if not _basic:
            lines.extend(_build_factor_basis_lines(ctx))
            lines.extend(_build_tech_resonance_lines(ctx))
        ReportBuilder._rm_signals(ctx, lines)
        # 幽灵规则核心结论已合并进「决策冲突」板块（item 4/9/11）
        lines.extend(ReportBuilder._rm_decision_conflict(ctx, triggered))
        return lines

    @staticmethod
    def _rm_eval(ctx):
        lines = [""]
        lines.append("  【持仓评估】")
        # 仓位上限：读取模型配置「最大单只仓位」（risk_params.max_single_position）
        # 未配置风控参数时显示「未设定」，不回退工具内置默认（纯去默认）。
        _ms_raw = get_risk_params().get('max_single_position')
        _ms_text = f"{_ms_raw*100:.0f}%" if isinstance(_ms_raw, (int, float)) else "未设定"
        lines.append(f"  仓位上限：{_ms_text}")
        env_label = ctx.env_config.get('label', '未知') if ctx.env_config else '未知'
        lines.append(f"  环境：{env_label}")
        # 初级用法（因子休眠）：不展示因子预期分/股票类型，仅保留持有期纪律评估
        if get_usage_mode() == 'basic':
            return lines
        # 因子预期分 + 判定股票类型（由【模型配置】加仓档位映射得出，与加仓板块一致）
        lines.append(f"  因子预期分：{ctx.final_score:.0f}分")
        add_eval = _evaluate_add_tiers_lines(ctx)
        # 展示真实股票类型（type_label，如 强势股/标准股），而非加仓档位映射的合并行名
        _type_label = getattr(ctx, 'type_label', '') or ''
        lines.append(f"  股票类型：{_type_label or add_eval['row_name']}")
        return lines

    @staticmethod
    def _rm_signals(ctx, lines):
        lines.append("")
        lines.append("  【多空信号】")
        lines.extend(_build_tech_signals(ctx))
        chip = ctx.chip_signal
        if chip:
            low = chip.get('dense_zone_low', 0)
            high = chip.get('dense_zone_high', 0)
            position = chip.get('position', '')
            if low > 0 and high > 0 and low < high:
                if position == '上方':
                    lines.append(f"  ✅ 筹码：多数人成本在{low:.2f}-{high:.2f}，抛压小，支撑有效")
                elif position == '下方':
                    lines.append(f"  ❌ 筹码：多数人成本在{low:.2f}-{high:.2f}，抛压重，涨到{high:.2f}附近会很难")
                else:
                    lines.append(f"  ✅ 筹码：多数人成本在{low:.2f}-{high:.2f}，震荡消化中")
        fib = ctx.fib_signal
        if fib:
            direction = fib.get('direction', '')
            level = fib.get('current_level', 0.5)
            high = fib.get('high', 0)
            low = fib.get('low', 0)
            if direction and high > 0 and low > 0:
                if direction == '上涨波段':
                    if level <= 0.236:
                        lines.append(f"  ✅ 斐波那契：从{low:.2f}涨到{high:.2f}，仅回调{level*100:.1f}%，极强势")
                    elif level <= 0.382:
                        lines.append(f"  ✅ 斐波那契：从{low:.2f}涨到{high:.2f}，回调到{level*100:.1f}%，正常回踩")
                    elif level <= 0.618:
                        lines.append(f"  ⚠️ 斐波那契：从{low:.2f}涨到{high:.2f}，回调到{level*100:.1f}%，关注支撑")
                    else:
                        lines.append(f"  ❌ 斐波那契：从{low:.2f}涨到{high:.2f}，深度回调到{level*100:.1f}%，趋势存疑")
                elif direction == '下跌波段':
                    if level >= 0.618:
                        lines.append(f"  ✅ 斐波那契：从{high:.2f}跌到{low:.2f}，反弹到{level*100:.1f}%，强势反弹")
                    elif level >= 0.382:
                        lines.append(f"  ⚠️ 斐波那契：从{high:.2f}跌到{low:.2f}，反弹到{level*100:.1f}%，关注压力")
                    elif level > 0:
                        lines.append(f"  ❌ 斐波那契：从{high:.2f}跌到{low:.2f}，仅反弹{level*100:.1f}%，多头很弱")
                    else:
                        lines.append(f"  ❌ 斐波那契：从{high:.2f}跌到{low:.2f}，几乎未反弹，多头极弱")
        pivot = ctx.pivot_signal
        if pivot:
            pivot_price = pivot.get('pivot', 0)
            is_bullish = pivot.get('is_bullish', False)
            s1 = pivot.get('s1', 0)
            r1 = pivot.get('r1', 0)
            if pivot_price > 0:
                if is_bullish:
                    lines.append(f"  ✅ 枢轴点：今日多空分界线在{pivot_price:.2f}， 今天偏多，第一阻力在{r1:.2f}")
                else:
                    lines.append(f"  ❌ 枢轴点：今日多空分界线在{pivot_price:.2f}， 今天偏空，第一支撑在{s1:.2f}")
        lines.append("")
        signal_text = chr(10).join(lines[-20:])
        bull_count = signal_text.count('✅')
        bear_count = signal_text.count('❌')
        # 减仓触发状态须与减仓板块一致；综合结论统一调用 _signals_conclusion（与 web_api 共用）。
        _rs = getattr(ctx, 'reduce_suggestion', None) or {}
        _reduce_triggered = bool(_rs.get('triggered_tiers')) or (_rs.get('reduce_ratio', 0) or 0) > 0
        icon, phrase = _signals_conclusion(ctx.decision.verdict, bull_count, bear_count, _reduce_triggered, True,
                                          direction=getattr(ctx, 'direction', 'long') or 'long')
        lines.append(f"  {icon} 综合：{phrase}")

    @staticmethod
    def _rm_decision_conflict(ctx, triggered):
        """决策冲突：幽灵规则（长期趋势）vs 风控规则（短期风险），左右对比，明确以风控优先。

        合并原「条件预案」「幽灵规则」「信号冲突说明」三块（item 4/9/11）。
        """
        lines = [""]
        lines.append("  【决策冲突：幽灵规则 vs 风控规则】")
        ghost = ctx.ghost_result
        if not ghost:
            lines.append("  ⚠️ 无幽灵数据，无法比对（持仓逻辑校验未运行）")
            return lines

        # 左：幽灵规则（长期趋势跟踪）
        rule1 = ghost.get('rule1', {}) or {}
        rule2 = ghost.get('rule2', {}) or {}
        ghost_hold = bool(rule1.get('result', False))
        ghost_main = "✅ 持仓正确" if ghost_hold else "❌ 持仓逻辑存疑"
        if rule1.get('reason'):
            ghost_main += f"（{rule1.get('reason')}）"
        rule2_txt = "✅ 已触及加仓参考条件" if rule2.get('result') else "⏳ 等待（未达加仓参考条件）"

        # 右：风控规则（短期风险约束）
        if triggered:
            risk_main = "🔴 风险控制参考条件已触及（减仓/清仓档触发）"
        else:
            risk_main = "🟢 风险控制参考条件未触及"

        # 左右对比（避免 CJK 等宽对齐问题，用两行对照 + 结论行）
        lines.append(f"  · 幽灵规则（长期趋势）：{ghost_main}")
        lines.append(f"  · 风控规则（短期风险）：{risk_main}")
        lines.append(f"  · 幽灵规则（加码判断）：{rule2_txt}")
        if triggered and ghost_hold:
            lines.append("  ➡️ 风控优先：幽灵验证长期趋势逻辑，风控是短期风险约束；"
                         "待股价重新站上均线后再评估")
        elif triggered and not ghost_hold:
            lines.append("  ➡️ 两边一致：幽灵与风控均指向离场，果断执行")
        elif not triggered and ghost_hold:
            lines.append("  ➡️ 两边一致：持仓逻辑正确且风控未触及，可继续持有")
        else:
            lines.append("  ➡️ 信号暂未冲突：继续观察")
        return lines

def _circuit_breaker_lines(risk):
    """大盘熔断暂停状态行（实盘与回测共用同一基准指数口径）。返回 list[str]。"""
    mcp = risk.get('market_crash_pct', 0) or 0
    if mcp <= 0:
        return []
    idx = (risk.get('market_crash_index') or 'sh000300')
    try:
        from engine.data_layer import IndexFetcher
        disp_name, ret = IndexFetcher.get_daily_return(idx)
    except Exception:
        disp_name, ret = idx, None
    if ret is None:
        return [f"    ℹ️ 大盘熔断：基准指数「{disp_name}」单日跌超 {mcp*100:.0f}% 暂停开新仓"
                f"（实时行情未获取，需手动判定）"]
    triggered = ret <= -mcp
    if triggered:
        return [f"    🛑 大盘熔断已触发：基准指数「{disp_name}」今日 {ret*100:+.2f}%"
                f"（阈值 -{mcp*100:.0f}%）→ 今日暂停开新仓"]
    return [f"    ℹ️ 大盘熔断未触发：基准指数「{disp_name}」今日 {ret*100:+.2f}%"
            f"（阈值 -{mcp*100:.0f}%）→ 可正常开新仓"]
