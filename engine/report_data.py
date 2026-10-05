#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""reportData 组装（自 UI 层上移，2026-09-12，B24 方案 1）。

来源与用途
----------
本模块 7 个函数由 `ui/web_api.py`（原 :121-983，约 863 行）**逐字上移**而来：
  `_market_status_label` / `_explain_factor` / `_pct_chg` / `_stars_and_ratio` /
  `_parse_star_count` / `_build_tech_board` / `_build_report_data`

上移原因：H5 服务端按 §2.8「engine 可直接 import、不做服务化包装」设计，但
`reportData` 组装此前只存在于桌面 UI 层，导致 F-301/F-302/F-303 在服务端无法达成。

可行性已前置验证：该函数块对外部符号的依赖为 **11 个**，全部来自 `engine.*` 与
stdlib（`datetime`），**零 `ui.*` 依赖**、无模块级常量依赖、无同层其他函数依赖 →
是自洽块，可整体搬迁而不引入反向依赖。

⚠️ 行为保持声明
----------------
搬迁为**逐字复制**（未改一行逻辑），并以 AST 结构比对 + 冻结 hash（测试 FT0 同款手法）
自证等价。`ui/web_api.py` 中原名函数已退化为对本模块的薄委托，故两处调用方
（分析页 / 期货分析 / 诊断）行为不变。

公开别名：`build_report_data`（服务端消费入口）。
"""

from datetime import datetime

from engine import quant_config
from engine.factor_registry import factor_explanation, get_factor, parse_raw_value
from engine.indicators import MATechnical
from engine.report_builder import (
    ReportBuilder,
    _circuit_breaker_lines,
    _evaluate_add_tiers_lines,
    _signals_conclusion,
    compute_decision,
)


def _market_status_label(t=None):
    t = t or datetime.now()
    if t.weekday() >= 5:
        return '非交易日（休市）'
    hm = t.hour * 60 + t.minute
    if 570 <= hm <= 690 or 780 <= hm <= 900:  # 9:30-11:30 / 13:00-15:00
        return '盘中实时'
    if hm > 900 or hm < 570:
        return '盘后'
    return '午间休市'


def _explain_factor(entry_line):
    """从 report_builder 复用的因子解释逻辑。"""
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
        return expl, contrib
    if '=' not in entry_line or '->' not in entry_line:
        return None
    name = entry_line.split(':')[0].strip()
    try:
        # 格式: rsi_value: 32.0000 -> z=1.20 x w=0.500 = +0.6 [方向=看跌]
        # 用正则找所有 `= 数值` 匹配，取最后一个作为贡献值
        # 避免被 `z=`、`x w=` 或 `[方向=` 干扰
        import re as _re
        matches = _re.findall(r'=\s*([+-]?\d+\.?\d*)', entry_line)
        if matches:
            contrib = float(matches[-1])
        else:
            return None
    except (ValueError, IndexError):
        return None
    fdef = get_factor(name)
    if fdef is None:
        return None
    pair = getattr(fdef, 'explanations', None)
    if pair is None:
        # 无方向性描述时，用因子标签兜底，避免加减分明细为空
        fallback = f"{fdef.label}贡献{contrib:+.1f}分"
        return fallback, contrib
    # 按因子 raw 档位/值域选文本（多档位/非单调因子修复，2026-08-19）；
    # 单调因子回退按贡献正负选二元文本（原逻辑，行为不变）
    explanation = factor_explanation(name, parse_raw_value(entry_line), contrib, pair)
    return explanation, contrib


def _pct_chg(price, ref):
    if not ref or ref == 0:
        return '—'
    return f"{((price - ref) / ref * 100):+.1f}%"


def _stars_and_ratio(tech_strength, bands):
    """根据技术共振占比和切点返回星级字符串。"""
    if tech_strength is None or not bands or len(bands) != 4:
        return '⭐ 无', '0%'
    ratio = tech_strength
    if ratio >= bands[3]:
        stars, level = '⭐⭐⭐⭐⭐', '极强'
    elif ratio >= bands[2]:
        stars, level = '⭐⭐⭐⭐', '强'
    elif ratio >= bands[1]:
        stars, level = '⭐⭐⭐', '中等'
    elif ratio >= bands[0]:
        stars, level = '⭐⭐', '弱'
    else:
        stars, level = '⭐', '极弱'
    return f"{stars} {level}", f"{int(ratio * 100)}%"


def _parse_star_count(s):
    """从星级字符串中解析数字，如 '5星 极强' → 5, '无' → 0。"""
    s = (s or '').strip()
    if s and s[0].isdigit():
        return int(s[0])
    return 0


def _json_safe(v, depth=0, max_list=6, max_depth=2):
    """把引擎值递归压成 JSON 安全结构。

    默认参数（`max_list=6, max_depth=2`）服务于 `reportData['techRaw']`：指标序列只保留
    末尾几项、深层嵌套降级为短字符串，以控体积。

    ⛔ 但**方案配置审查**（`server.ai.prompts._trim_scheme_config`）必须传更大的
    `max_list/max_depth`：方案里 `entry_conditions.strong` 这类是 depth=2 的 dict、
    `veto_enabled` 有 8+ 个键，用默认值会被**降级成截断字符串**
    （2026-09-27 实际踩到：`strong` 变成 `"{'tech_signal': [...], 'veto_on': True, ..."` 的字符串）
    ⇒ AI 拿到的是残缺文本，无法审查。

    ⛔ 为什么必须做：`ctx.tech` 混有 numpy 标量、NaN/Inf、嵌套 dict、长序列 list。
    直接塞进 reportData 会让 `json.dumps` 产出**非法 JSON**（`NaN` 不是合法 JSON 字面量），
    前端 `JSON.parse` 整包失败 ⇒ 分析页白屏。这是「AI 增强搞坏主流程」的典型路径。
    规则：
      - bool 先于 int 判定（bool 是 int 子类）
      - float 必须 isfinite，否则丢弃
      - dict 递归，深度 >=max_depth 降级为短字符串（避免无界膨胀）
      - list 只留末尾 max_list 项（指标序列里最近的最有意义）
      - 其余（含 numpy 标量）尝试 float()，失败则丢弃
    丢弃的键**直接不出现**，好过留 null——null 会被 AI 误读成「该项为 0」。
    """
    from math import isfinite
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        try:
            return round(v, 4) if isfinite(v) else None
        except (TypeError, ValueError):
            return None
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        if depth >= max_depth:
            return str(v)[:120]
        out = {}
        for k, sub in v.items():
            sv = _json_safe(sub, depth + 1, max_list, max_depth)
            if sv is not None:
                out[str(k)] = sv
        return out or None
    if isinstance(v, (list, tuple)):
        tail = list(v)[-max_list:]
        out = [x for x in (_json_safe(i, depth + 1, max_list, max_depth) for i in tail)
               if x is not None]
        return out or None
    try:
        f = float(v)
        return round(f, 4) if isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _recent_bars(data_list, n=30):
    """最近 n 根 K 线摘要（OHLCV），补上平滑指标抹掉的形态信息。

    指标都是平滑值，"昨天放量长上影""连续三天缩量""圆弧底"这类形态它表达不出来，
    而近端 K 线恰恰是「最近发生了什么」的直接证据。
    n 默认 30：够看出约一个月的形态节奏（≈750 token，成本可控）。
    再长收益递减——远端信息已被指标压缩过，且会稀释其他投喂内容的注意力。
    """
    out = []
    for bar in (data_list or [])[-n:]:
        if not isinstance(bar, dict):
            continue
        try:
            close = float(bar.get('close'))
        except (TypeError, ValueError):
            continue
        if not close:
            continue
        date = bar.get('date') or bar.get('datetime') or bar.get('day') or ''
        row = {'date': str(date)[:10]}
        for k in ('open', 'high', 'low', 'close'):
            try:
                row[k] = round(float(bar.get(k)), 2)
            except (TypeError, ValueError):
                pass
        try:
            row['volume'] = round(float(bar.get('volume') or 0))
        except (TypeError, ValueError):
            pass
        out.append(row)
    return out


def _build_tech_board(ctx):
    """构建「技术指标」看板（逐项列 KDJ/RSI/均线/MACD/量能/筹码/斐波/枢轴/ADX/乖离）。

    返回 items 列表，每项 {label, value, tone, desc}：
      - label：指标名
      - value：指标数值 / 状态（原始读数）
      - tone：up/down/neutral，供前端着色
      - desc：对该指标的「分析解读」——它意味着什么、该怎么应对（面向不懂代码的散户）
    数据全部来自 ctx.tech / ctx.market（analyze 走 scoring_core.build_tech + build_market_dict）。
    用 get 兜底，任一指标缺失/无效时优雅降级为「—」，不阻塞整卡渲染。
    """
    from math import isfinite
    tech = ctx.tech or {}
    market = ctx.market or {}
    lp = ctx.latest_price
    items = []
    tone_of = lambda x: 'up' if x else ('down' if x is False else 'neutral')
    num = lambda k, d=0.0: (tech.get(k) if isinstance(tech.get(k), (int, float)) and isfinite(tech.get(k)) else d)
    def append(label, value, tone, desc):
        items.append({'label': label, 'value': value, 'tone': tone, 'desc': desc})

    # 均线排列（市场级，多空语义）
    ma_arr = market.get('ma_arrangement', '') or ''
    if ma_arr:
        m_up = ('多' in ma_arr) or ('张' in ma_arr)
        m_down = ('空' in ma_arr) or ('散' in ma_arr and '空头' in ma_arr)
        if '黏合' in ma_arr or '交织' in ma_arr or '变盘' in ma_arr:
            desc = '均线纠缠在一起，说明多空分歧大、方向未定，是变盘前兆——先别重仓，等它选好方向再跟。'
        elif m_up:
            desc = '短周期均线在长周期之上逐级向上，属于上升趋势，回调时均线附近容易形成支撑，是持股/逢低加仓的顺风环境。'
        else:
            desc = '短周期均线在长周期之下逐级向下，属于下降趋势，反弹到均线附近容易遇阻，趋势转好前不宜满仓抄底。'
        append('均线排列', ma_arr, tone_of(m_up), desc)

    # MA5 / MA10 / MA20 / MA60：现价 vs 均线（±%）
    # 各周期含义不同，必须分而化之：5日=极短线情绪线，10日=短线强弱分水岭，
    # 20日=月线/中期趋势，60日=季线/牛熊分界。绝不能用同一套模板。
    for p in (5, 10, 20, 60):
        ma = num(f'sma_{p}')
        if not ma:
            continue
        diff = (lp - ma) / ma * 100 if ma else 0
        pos = f"现价{(lp - ma):+.2f}（{diff:+.1f}%）" if isinstance(lp, (int, float)) else f"{ma:.2f}"
        if p == 5:
            if diff >= 0:
                desc = ('现价远超5日线，短线情绪过热、乖离偏大，追高容易吃面，回踩5日线再谈介入。'
                        if diff > 5 else '5日线是极短线情绪线，现价站上它说明短线多头占优，回踩5日线不破可持有。')
            else:
                desc = ('现价大幅跌破5日线，短线已走坏，弱反弹到5日线附近往往是离场/减仓的机会。'
                        if diff < -5 else '现价刚跌破5日线，短线在转弱，站回5日线之前谨慎追，反弹先看能否收复。')
        elif p == 10:
            if diff >= 0:
                desc = ('现价明显高于10日线，短线乖离较大，不宜追涨，等回踩10日线企稳再谈。'
                        if diff > 5 else '10日线是短线强弱分水岭，现价站上它，短线偏强且有延续动能。')
            else:
                desc = ('现价深度跌破10日线，短线走弱明显，反弹到10日线附近先看能否收复，否则逢反弹减仓。'
                        if diff < -5 else '现价跌破10日线，短线转弱，站回10日线之前以控仓为主。')
        elif p == 20:
            if diff >= 0:
                desc = ('现价高于20日线较多，短线乖离偏大，忌追高，等待回踩20日线。'
                        if diff > 5 else '20日线代表中期趋势，现价在其上方运行，中期偏多，回调到20日线附近可视为支撑。')
            else:
                desc = ('现价大幅低于20日线，中期走坏，弱反弹到20日线附近宜减仓不宜补仓。'
                        if diff < -5 else '现价跌破20日线，中期趋势转弱，反弹到20日线常是压力。')
        else:  # p == 60
            if diff >= 0:
                desc = ('现价远高于60日线，中长线乖离较大、短线过热，等回档到60日线附近更安全。'
                        if diff > 5 else '60日线常被视为牛熊分界，现价站上它说明中长期方向偏上，回调更接近逢低机会。')
            else:
                desc = ('现价深度低于60日线，属中长期弱势，需等它重新收复60日线再考虑趋势转多。'
                        if diff < -5 else '现价跌破60日线，中长期偏弱，反弹到60日线附近是重要压力，趋势未转强前不宜重仓。')
        append(f'MA{p}', f'{ma:.2f} · {pos}', 'up' if diff >= 0 else 'down', desc)

    # KDJ
    if any(k in tech for k in ('kdj_k', 'kdj_signal')):
        k_ = num('kdj_k'); d_ = num('kdj_d'); j_ = num('kdj_j')
        sig = tech.get('kdj_signal', '')
        if k_ or d_ or j_ or sig:
            val = f"K{k_:.1f} D{d_:.1f} J{j_:.1f} · {sig or '—'}"
            if sig == '金叉':
                desc = 'K线向上穿过D线形成金叉，是短线上涨动能启动的信号，可留意轻仓试探，但也要确认放量方能捂到底。'
            elif sig == '死叉':
                desc = 'K线跌破D线形成死叉，短线动能转弱，持有者应警惕回调，宜收紧止盈/止损位。'
            else:
                high = (k_ >= 80) or (j_ >= 100)
                low = (k_ <= 20) or (j_ <= 0)
                if high:
                    desc = f'KDJ当前处于高位（K{k_:.0f}/J{j_:.0f}），短线涨幅已多、动能易透支，追高需谨慎，谨防高位回落。'
                elif low:
                    desc = f'KDJ当前处于低位（K{k_:.0f}/J{j_:.0f}），短线超跌、动能趋稳，容易引发技术性反弹，可留意企稳信号。'
                else:
                    desc = f'KDJ处于中位（K{k_:.0f}/J{j_:.0f}），无明显超买超卖，情绪中性，按纪律操作即可。'
            append('KDJ', val, tone_of(sig == '金叉'), desc)

    # RSI
    rsi = num('rsi', 50)
    rsi_p = int(tech.get('rsi_period', 14) or 14)
    if rsi >= 0:
        if rsi >= 70:
            st = '超买'; tone = 'down'
            desc = f'RSI高达{rsi:.0f}，市场情绪过热、短线涨幅已多，风险收益比变差——别在这里追高，反该分批兑现。'
        elif rsi <= 30:
            st = '超卖'; tone = 'up'
            desc = f'RSI低至{rsi:.0f}，短线抛压宣泄较充分，性价比开始显现，但需等企稳信号，别急着满仓接刀。'
        else:
            st = '中性'; tone = 'neutral'
            desc = f'RSI{rsi:.0f}在中性区间，既不超买也不超卖，情绪面平稳，按既定纪律执行即可。'
        append(f'RSI({rsi_p})', f'{rsi:.1f} · {st}', tone, desc)

    # 布林带（BOLL）
    b_up = tech.get('bb_upper'); b_mid = tech.get('bb_mid'); b_low = tech.get('bb_lower')
    if b_up is not None and b_mid is not None and b_low is not None:
        pct_b = num('bb_pct_b', 0.5)
        sq = tech.get('bb_squeeze', '') or ''
        val = f"上{b_up:.2f} 中{b_mid:.2f} 下{b_low:.2f}" + (f' · {sq}' if sq else '')
        if pct_b >= 1:
            tone = 'down'
            desc = f'价格冲出布林上轨（%B约{pct_b*100:.0f}%），短线过热、乖离过大，追高风险高，谨防向中轨回归。'
        elif pct_b <= 0:
            tone = 'up'
            desc = '价格跌破布林下轨，短线超跌，易触发超卖反弹，可留意企稳，但趋势未稳不要重仓接刀。'
        elif '收缩' in sq or '变盘' in sq:
            tone = 'neutral'
            desc = '布林带持续收口，波动收敛到极致，历史多是变盘前兆、方向未明，宜轻仓等待突破方向。'
        elif '扩张' in sq:
            tone = 'neutral'
            desc = '布林带张开口子、波动放大，趋势在延续，顺势更有含金量，但注意别追在极端乖离位。'
        else:
            tone = 'neutral'
            desc = '价格沿布林带中轨附近波动，属常态区间，按既定纪律操作即可。'
        append('布林带', val, tone, desc)

    # 威廉指标 WR（超买超卖动量）
    wrv = num('wr', None)
    if wrv is not None:
        wst = tech.get('wr_state', '') or ''
        if wst == '超卖':
            tone = 'up'
            desc = f'WR{wrv:.1f}进入超卖区，短线抛压释放较充分，易现技术性反弹，可留意企稳信号，但别急着接刀。'
        elif wst == '超买':
            tone = 'down'
            desc = f'WR{wrv:.1f}进入超买区，短线已过热，回落风险上升，逢高可考虑减仓/止盈。'
        else:
            tone = 'neutral'
            desc = f'WR{wrv:.1f}在常态区域，无明显超买超卖，按纪律操作即可。'
        append('WR(威廉)', f'{wrv:.1f} · {wst or "—"}', tone, desc)

    # MACD（DIF/DEA/柱 + 金叉死叉 或 动能状态）
    dif = num('macd'); dea = num('macd_signal'); hist = num('macd_hist')
    if tech.get('macd_status'):
        ms = tech['macd_status']
    elif dif and dea:
        ms = '金叉' if dif >= dea else '死叉'
    else:
        ms = tech.get('macd_accel_status', '')
    if dif or dea or ms:
        val = f"DIF{dif:+.3f} DEA{dea:+.3f}" if dif or dea else ''
        val = (val + ' · ' if val else '') + (str(ms) or '—')
        if ms in ('金叉', '动能向上'):
            desc = 'DIF上穿DEA形成金叉，中期动能偏多，多头占优——持仓可坚定一些，回踩不破前可持有看涨。'
        elif ms in ('死叉', '动能向下'):
            desc = 'DIF下穿DEA形成死叉，中期动能偏空——别逆势扛单，反弹到压力位优先降低仓位控制回撤。'
        else:
            desc = 'MACD动能走平、多空力量接近均衡，方向未明，此时耐心等待是更好的选择，不宜加仓。'
        append('MACD', val, tone_of(ms in ('金叉', '动能向上')), desc)

    # 量能
    vratio = num('volume_ratio')
    vstatus = tech.get('volume_status', '') or '正常'
    if vratio:
        if vstatus in ('放量上涨',):
            desc = f'量比{vratio:.2f}放量上攻，资金积极进场，上涨有成交量背书，含金量更高，可顺势持有。'
        elif vstatus in ('放量下跌',):
            desc = f'量比{vratio:.2f}放量下杀，抛压明显、资金在离场——这种下跌需警惕，反弹时优先减仓。'
        elif vstatus in ('缩量','缩量回调','缩量整理'):
            desc = f'量比{vratio:.2f}属缩量，观望气氛浓；缩量回踩往往健康，缩量大涨则要提防无量虚涨。'
        else:
            desc = f'量比{vratio:.2f}量能处于正常水平，多空按既定纪律操作即可。'
        append('量能', f"量比{vratio:.2f} · {vstatus}", tone_of(vstatus not in ('放量下跌',)), desc)

    # 筹码
    ch = tech.get('chip_concentration') or {}
    if isinstance(ch, dict) and ch.get('signal'):
        if ch.get('position') == '上方':
            cls_ = 'up'
            desc = '股价已突破筹码密集区往上，套牢盘被消化、抛压小，上方相对清爽，上涨阻力较轻。'
        elif ch.get('position') == '下方':
            cls_ = 'down'
            desc = '股价跌破筹码密集区，上方堆积了大量套牢盘，反弹到密集区附近会遇到较大抛压。'
        else:
            cls_ = 'neutral'
            desc = '股价正处于筹码密集区内反复震荡，多空在此换手充分，等放量选择方向再作决断。'
        append('筹码', f"{ch.get('signal')}（密集区 {ch.get('dense_zone_low')}–{ch.get('dense_zone_high')}）", cls_, desc)

    # 斐波那契
    fb = tech.get('fibonacci') or {}
    if isinstance(fb, dict) and fb.get('signal'):
        dir_up = '上涨' in str(fb.get('direction', ''))
        if dir_up:
            desc = f"当前处于{fb.get('current_level_desc')}回撤位，属强势反弹结构；回撤不破关键位有利于继续上行，可在支撑位附近分批承接。"
        else:
            desc = f"当前处于{fb.get('current_level_desc')}反弹位，属弱势结构；反弹到关键压力位更易遇阻回落，宜减少追高。"
        append('斐波那契', f"{fb.get('current_level_desc')}·{fb.get('signal')}",
               'up' if dir_up else 'neutral', desc)

    # 枢轴点
    pp = tech.get('pivot_points') or {}
    if isinstance(pp, dict) and pp.get('pivot'):
        desc = '枢轴点是当日多空分水岭：价在其上方偏多、下方偏空；R1阻力、S1支撑，价格贴近其一即为短线转折的关键观察位。'
        append('枢轴点', f"枢轴{pp.get('pivot')} · R1 {pp.get('r1', '—')} / S1 {pp.get('s1', '—')}",
               'neutral', desc)

    # ADX 趋势强度
    adx = num('adx')
    if adx:
        a_state = tech.get('adx_state', '')
        if adx >= 25:
            desc = f'ADX{adx:.1f}说明趋势较强、方向明确；+DI在上表明多势，顺着趋势做，别凭感觉逆势抢反弹。'
        else:
            desc = f'ADX{adx:.1f}偏低，市场处于震荡盘整、趋势不明，趋势策略易反复挨打，宜轻仓或观望。'
        append('ADX趋势', f"ADX{adx:.1f} · {a_state or '—'}",
               'up' if tech.get('di_plus', 0) >= tech.get('di_minus', 0) else 'neutral', desc)

    # 乖离（BiasAnalyzer）
    ba = tech.get('bias_analysis') or {}
    if isinstance(ba, dict):
        alerts = '；'.join(ba.get('alerts') or []) if ba.get('alerts') else ''
        direction = str(ba.get('direction', '—'))
        if '强势' in direction:
            desc = '股价明显偏离均线、短线涨幅过大，随时可能向均线回归——追高的风险在上升，可考虑兑现部分利润。'
        elif '弱势' in direction:
            desc = '股价明显下挫偏离均线，短线超跌，可能出现技术性修复反弹，但趋势未稳前不轻易重仓。'
        else:
            desc = '股价与均线贴合，乖离适中，短线情绪健康，按纪律正常操作即可。'
        append('乖离率', f"{direction}" + (f'（{alerts}）' if alerts else '') or '—',
               'down' if '强势' in direction else ('up' if '弱势' in direction else 'neutral'), desc)

    # ATR（波动率 → 止损距离）
    # ⛔ 此前看板缺 ATR，而「止损参考位」恰恰最该以它为据——用支撑位当止损是另一回事
    #（支撑位是「价格结构」，ATR 是「这只票日常能晃多大幅度」）。
    atr_v = num('atr')
    atr_ratio = num('atr_ratio')
    if atr_v and atr_ratio:
        pct = atr_ratio * 100
        if pct >= 5:
            desc = (f'日均真实波幅 {pct:.1f}%，属高波动——止损设窄了会被日常波动直接扫出局，'
                    f'建议按 2 倍 ATR（约 {pct * 2:.1f}%）留空间，同时仓位要相应放小。')
        elif pct >= 2.5:
            desc = (f'日均真实波幅 {pct:.1f}%，波动中等——止损距离可参考 2 倍 ATR'
                    f'（约 {pct * 2:.1f}%），比单看支撑位更贴近这只票的实际脾气。')
        else:
            desc = (f'日均真实波幅 {pct:.1f}%，波动偏小——止损可以设紧一些，'
                    f'2 倍 ATR 约 {pct * 2:.1f}%。')
        append('ATR', f'{atr_v:.2f} · 日均波幅{pct:.1f}%', 'neutral', desc)

    return items


def _scan_stats(results):
    """扫描结果集的分类统计（供「候选池审计师」呈现与解读）。

    ⛔ 为什么必须放引擎算：
      让 LLM 自己数几十行的板块占比 = 让它做算术。LLM 数数会出错，且**不可复现**
      （同一批数据两次调用可能给出不同占比）。所以所有「分类计数 / 占比 / 分位」
      必须在引擎侧算成**确定数字**，AI 只负责呈现与解读含义。
      这与「LLM 不得从 OHLCV 自算指标」是同一条原则。

    ⛔ 只用结果集**真实存在**的列（`engine/market_scan_core.py:183-196`）：
      sector / final_score / stars / level / entry_tier_label / factor_values。
      **结果集没有涨跌字段**（无 change / change_pct），故不做「涨跌分布」。
      也**没有**市值 / 风格列 —— 需要这两项的结论必须显式写「数据缺失」。

    返回：计数与占比均为引擎确定值；占比按 total 计算（可能因字段缺失不等于 100%）。
    """
    import statistics
    from collections import Counter

    rows = [r for r in (results or []) if isinstance(r, dict)]
    total = len(rows)
    out = {"total": total}
    if not total:
        return out

    def _pct(n):
        return round(n / total * 100, 1)

    def _tally(keyfn):
        c = Counter()
        missing = 0
        for r in rows:
            k = keyfn(r)
            if k is None or str(k).strip() == "":
                missing += 1
                continue
            c[str(k).strip()] += 1
        return c, missing

    def _ordered(c):
        return [{"name": k, "count": v, "pct": _pct(v)} for k, v in c.most_common()]

    # ── 板块分布 ──
    c, miss = _tally(lambda r: r.get("sector"))
    items = _ordered(c)
    out["n_sectors"] = len(items)
    if miss:
        out["sector_missing"] = miss
    top = items[:15]
    if len(items) > 15:
        rest = sum(x["count"] for x in items[15:])
        top = top + [{"name": "（其余 %d 个板块合计）" % (len(items) - 15),
                      "count": rest, "pct": _pct(rest)}]
    out["top_sectors"] = top
    out["concentration"] = {
        "top1_sector_pct": items[0]["pct"] if items else 0.0,
        "top3_sector_pct": round(sum(x["pct"] for x in items[:3]), 1),
        "n_sectors": len(items),
    }

    # ── 评分分布（含直方图与集中度）──
    scores = [r.get("final_score") for r in rows
              if isinstance(r.get("final_score"), (int, float))
              and not isinstance(r.get("final_score"), bool)]
    if scores:
        out["score_stats"] = {
            "n": len(scores),
            "min": round(min(scores), 1),
            "max": round(max(scores), 1),
            "mean": round(statistics.fmean(scores), 1),
            "median": round(statistics.median(scores), 1),
        }
        bands = [(60, None, "≥60"), (50, 60, "50-60"), (40, 50, "40-50"),
                 (30, 40, "30-40"), (None, 30, "<30")]
        hist = []
        for lo, hi, label in bands:
            if lo is None:
                sel = [s for s in scores if s < hi]
            elif hi is None:
                sel = [s for s in scores if s >= lo]
            else:
                sel = [s for s in scores if lo <= s < hi]
            hist.append({"band": label, "count": len(sel),
                         "pct": round(len(sel) / len(scores) * 100, 1)})
        out["score_bands"] = hist
        # 集中度：最密集的一档占比（高 = 可能是卡阈值筛出来的）
        out["score_band_top"] = max(hist, key=lambda x: x["count"])

    # ── 星级分布 ──
    c, miss = _tally(lambda r: r.get("stars"))
    if c:
        out["by_stars"] = _ordered(c)

    # ── 建仓分档分布（entry_tier_label 优先，回退 level）──
    c, miss = _tally(lambda r: r.get("entry_tier_label") or r.get("level"))
    if c:
        out["by_tier"] = _ordered(c)

    # ── 因子暴露（方案启用因子的原始值分布）──
    # 这是原契约承诺但结果集缺失的「因子暴露」维度的真实替代物：
    # factor_values 是当前方案各启用因子在这批票上的原始读数。
    fv_keys = set()
    for r in rows:
        fv = r.get("factor_values")
        if isinstance(fv, dict):
            fv_keys.update(fv.keys())
    exposure = {}
    for k in sorted(fv_keys):
        vals = []
        for r in rows:
            fv = r.get("factor_values")
            if isinstance(fv, dict):
                v = fv.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    vals.append(float(v))
        # 样本太少不给统计（避免 1~2 个值被当成"分布"）
        if len(vals) >= max(3, total // 4):
            exposure[k] = {
                "n": len(vals),
                "mean": round(statistics.fmean(vals), 3),
                "median": round(statistics.median(vals), 3),
                "min": round(min(vals), 3),
                "max": round(max(vals), 3),
            }
    if exposure:
        out["factor_exposure"] = exposure

    return out


def _build_report_data(ctx, stock_name):
    """从 TradingContext 构建前端 reportData 结构。"""
    tech = ctx.tech or {}
    market = ctx.market or {}
    final_score = getattr(ctx, 'final_score', 0) or 0
    env_config = getattr(ctx, 'env_config', {}) or {}
    stock_type = getattr(ctx, 'type_label', '') or getattr(ctx, 'status_label', '') or getattr(ctx, 'stock_type', '')
    latest = float(ctx.latest_price) if ctx.latest_price else 0

    # 决策依据
    plus, minus = [], []
    for entry in getattr(ctx, 'score_detail', []) or []:
        result = _explain_factor(entry)
        if result is None:
            continue
        expl, contrib = result
        if contrib > 0:
            plus.append({'score': round(contrib, 1), 'desc': expl})
        elif contrib < 0:
            minus.append({'score': round(contrib, 1), 'desc': expl})
    plus.sort(key=lambda x: -abs(x['score']))
    minus.sort(key=lambda x: -abs(x['score']))
    raw_total = sum(x['score'] for x in plus) + sum(x['score'] for x in minus)

    # 技术共振（threshold 为 None 或 bands 无效时视为未启用，整段隐藏；
    # 与 ui 侧「技术共振明细」板块的显隐口径一致，见 optional-report-section 约定）
    sr = getattr(ctx, 'signal_rating', {}) or {}
    ts = sr.get('tech_strength')
    bands = sr.get('tech_strength_bands')
    threshold = sr.get('tech_resonance_threshold')
    tech_resonance_enabled = ts is not None and threshold is not None \
        and isinstance(bands, (list, tuple)) and len(bands) == 4
    if tech_resonance_enabled:
        strength_str, ratio_str = _stars_and_ratio(ts, bands)
    else:
        strength_str, ratio_str = '', ''
    sig_plus = []
    sig_minus = []
    if tech_resonance_enabled:
        for sf in (sr.get('significant_factors') or []):
            try:
                n, c = sf[0], sf[1]
            except (TypeError, IndexError):
                continue
            fdef = get_factor(n)
            label = fdef.label if fdef else n
            # c > 0 进加分，c < 0 进减分，c == 0 不进任何列表
            if c > 0:
                sig_plus.append({'score': round(c, 1), 'desc': label})
            elif c < 0:
                sig_minus.append({'score': round(c, 1), 'desc': label})
        # 按 |贡献| 降序
        sig_plus.sort(key=lambda x: -abs(x['score']))
        sig_minus.sort(key=lambda x: -abs(x['score']))

    # 多空信号
    bull_bear = []
    ma_arr = market.get('ma_arrangement', '')
    _ma_level = MATechnical.arrangement_level(ma_arr)
    if _ma_level >= 2.0:
        bull_bear.append({'ok': True, 'label': '均线趋势',
            'desc': '短期均线在长期均线之上，多头排列，趋势向上有支撑'})
    elif _ma_level == 1.0:
        bull_bear.append({'ok': True, 'label': '均线趋势',
            'desc': '短期均线初步上穿，多头初期，趋势待确认'})
    elif _ma_level <= -2.0:
        bull_bear.append({'ok': False, 'label': '均线趋势',
            'desc': '短期均线在长期均线之下，空头排列，上方压力较重'})
    elif _ma_level == -1.0:
        bull_bear.append({'ok': False, 'label': '均线趋势',
            'desc': '短期均线初步下穿，空头初期，趋势转弱'})
    else:
        bull_bear.append({'ok': False, 'label': '均线趋势',
            'desc': '均线交织缠绕，方向不明，等待趋势明朗'})
    macd_status = tech.get('macd_status', '')
    bull_bear.append({
        'ok': '金叉' in macd_status,
        'label': 'MACD动能',
        'desc': '金叉状态，短期动能向上，多头占优' if '金叉' in macd_status else
                ('死叉状态，短期动能向下，空头占优' if '死叉' in macd_status else '动能走平，多空力量均衡')
    })
    rsi = tech.get('rsi', tech.get('rsi_14', 50))
    if rsi > 70:
        rsi_desc = f"RSI {rsi:.0f}，处于超买区域，短期追高风险较大"
        rsi_ok = False
    elif rsi < 30:
        rsi_desc = f"RSI {rsi:.0f}，处于超卖区域，反弹概率较高"
        rsi_ok = True
    else:
        rsi_desc = f"RSI {rsi:.0f}，中性区间，既未超买也未超卖"
        rsi_ok = True
    bull_bear.append({'ok': rsi_ok, 'label': '短期情绪', 'desc': rsi_desc})
    vol_price = market.get('volume_price', '')
    vp_map = {
        '放量上涨': (True, '放量上涨，资金积极介入，上涨有成交量确认'),
        '放量下跌': (False, '放量下跌，抛压明显，空方主导'),
        '缩量上涨': (False, '缩量上涨，参与资金不足，上涨持续性存疑'),
        '缩量下跌': (True, '缩量下跌，抛压衰竭，可能接近底部'),
    }
    vp_ok, vp_desc = vp_map.get(vol_price, (True, '成交量正常，无异常信号'))
    bull_bear.append({'ok': vp_ok, 'label': '量价关系', 'desc': vp_desc})

    # 筹码 / 斐波那契 / 枢轴点（字段对齐 indicators_advanced.py 实际返回）
    chip = tech.get('chip_concentration', {}) or {}
    if chip:
        chip_pos = chip.get('position', '')
        chip_low = chip.get('dense_zone_low', 0)
        chip_high = chip.get('dense_zone_high', 0)
        if chip_pos == '上方':
            bull_bear.append({'ok': True, 'label': '筹码',
                'desc': f"多数人成本在{chip_low:.2f}-{chip_high:.2f}，抛压小，支撑有效"})
        elif chip_pos == '下方':
            bull_bear.append({'ok': False, 'label': '筹码',
                'desc': f"多数人成本在{chip_low:.2f}-{chip_high:.2f}，抛压重，涨到{chip_high:.2f}附近会很难"})
        else:
            bull_bear.append({'ok': True, 'label': '筹码',
                'desc': f"多数人成本在{chip_low:.2f}-{chip_high:.2f}，震荡消化中"})
    fib = tech.get('fibonacci', {}) or {}
    if fib:
        fib_dir = fib.get('direction', '')
        fib_level = fib.get('current_level', 0.5)
        fib_high = fib.get('high', 0)
        fib_low = fib.get('low', 0)
        if fib_dir == '上涨波段':
            if fib_level <= 0.236:
                fib_ok, fib_desc = True, f"从{fib_low:.2f}涨到{fib_high:.2f}，仅回调{fib_level*100:.1f}%，极强势"
            elif fib_level <= 0.382:
                fib_ok, fib_desc = True, f"从{fib_low:.2f}涨到{fib_high:.2f}，回调到{fib_level*100:.1f}%，正常回踩"
            elif fib_level <= 0.618:
                fib_ok, fib_desc = True, f"从{fib_low:.2f}涨到{fib_high:.2f}，回调到{fib_level*100:.1f}%，关注支撑"
            else:
                fib_ok, fib_desc = False, f"从{fib_low:.2f}涨到{fib_high:.2f}，深度回调到{fib_level*100:.1f}%，趋势存疑"
        elif fib_dir == '下跌波段':
            if fib_level >= 0.618:
                fib_ok, fib_desc = True, f"从{fib_high:.2f}跌到{fib_low:.2f}，反弹到{fib_level*100:.1f}%，强势反弹"
            elif fib_level >= 0.382:
                fib_ok, fib_desc = True, f"从{fib_high:.2f}跌到{fib_low:.2f}，反弹到{fib_level*100:.1f}%，关注压力"
            elif fib_level > 0:
                fib_ok, fib_desc = False, f"从{fib_high:.2f}跌到{fib_low:.2f}，仅反弹{fib_level*100:.1f}%，多头很弱"
            else:
                fib_ok, fib_desc = False, f"从{fib_high:.2f}跌到{fib_low:.2f}，几乎未反弹，多头极弱"
        else:
            fib_ok, fib_desc = True, '当前处于斐波那契回撤区'
        bull_bear.append({'ok': fib_ok, 'label': '斐波那契', 'desc': fib_desc})
    pivot = tech.get('pivot_points', {}) or {}
    if pivot:
        pivot_price = pivot.get('pivot', 0)
        is_bullish = pivot.get('is_bullish', False)
        s1 = pivot.get('s1', 0)
        r1 = pivot.get('r1', 0)
        if pivot_price > 0:
            if is_bullish:
                bull_bear.append({'ok': True, 'label': '枢轴点',
                    'desc': f"今日多空分界线在{pivot_price:.2f}，今天偏多，第一阻力在{r1:.2f}"})
            else:
                bull_bear.append({'ok': False, 'label': '枢轴点',
                    'desc': f"今日多空分界线在{pivot_price:.2f}，今天偏空，第一支撑在{s1:.2f}"})

    # 持仓数据（提前计算，供多空综合结论引用）
    has_position = bool(getattr(ctx, 'has_position', False)) and (getattr(ctx, 'entry_price', 0) or 0) > 0
    entry_price = float(ctx.entry_price) if has_position else 0
    bars = int(getattr(ctx, 'bars_held', 0) or 0)
    # 浮盈：多头 (现价-成本)/成本；空头反向 (成本-现价)/成本（下跌盈利）
    direction = getattr(ctx, 'direction', 'long') or 'long'
    if has_position and entry_price > 0:
        pnl_pct = ((entry_price - latest) / entry_price * 100) if direction == 'short' else ((latest - entry_price) / entry_price * 100)
    else:
        pnl_pct = 0
    add_sug = getattr(ctx, 'add_suggestion', {}) or {}
    reduce_sug = getattr(ctx, 'reduce_suggestion', {}) or {}

    # 空单视角：bull_bear 各项 ok 均为"对多头有利"语义，空单下取反
    # （RSI>70 超买对空头是做空良机而非警示，筹码/斐波/枢轴同理），
    # 使 ok_count/结论文案随方向正确，避免空单报告误标 ✕
    if getattr(ctx, 'direction', 'long') == 'short':
        for x in bull_bear:
            x['ok'] = not x['ok']
    ok_count = sum(1 for x in bull_bear if x['ok'])
    bear_count = len(bull_bear) - ok_count
    if ok_count > bear_count:
        bb_summary = '多数信号偏多'
    elif bear_count > ok_count:
        bb_summary = '多数信号偏空'
    else:
        bb_summary = '信号分歧，多看少动'
    # 多空综合结论：统一调用 report_builder._signals_conclusion（唯一映射，全仓只此一处）
    # 不再在 web_api 内重算 verdict→文案，从 ctx.decision 读取权威裁决。
    _decision = getattr(ctx, 'decision', None)
    if _decision is None:
        _decision = compute_decision(ctx)
    _verdict = _decision.verdict
    _reduce_triggered = bool(reduce_sug.get('triggered_tiers')) or (reduce_sug.get('reduce_ratio', 0) or 0) > 0
    _, bull_bear_summary = _signals_conclusion(_verdict, ok_count, bear_count, False, False,
                                             _decision.hit_line_name, _decision.position, direction)
    _, position_bull_bear_summary = _signals_conclusion(_verdict, ok_count, bear_count, _reduce_triggered, True,
                                                      _decision.hit_line_name, _decision.position, direction)

    # 关键价位
    resistance = getattr(ctx, 'resistance', 0) or latest * 1.05
    support = getattr(ctx, 'support', 0) or latest * 0.95

    # 信号触发情况
    # 持仓报告不展示大盘熔断行（熔断只针对开新仓，对持仓无意义）
    signals = {'hit': [], 'miss': [], 'pending': [], 'ordered': [], 'circuit': ''}

    ghost = getattr(ctx, 'ghost_result', {}) or {}
    risk_params = quant_config.get_risk_params() or {}

    # 填充建仓条件触发明细（仅空仓场景）
    if not has_position:
        entry_levels = _decision.entry_levels
        # 高等级触发时覆盖低等级，仅展示命中档及其以上未命中档（对齐 report_builder）
        hit_idx = None
        for i, lv in enumerate(entry_levels):
            if lv['verdict'] == 'hit':
                hit_idx = i
                break
        display_levels = entry_levels[:hit_idx + 1] if hit_idx is not None else entry_levels
        for lv in display_levels:
            if lv['verdict'] == 'hit':
                item = {'type': 'hit', 'tier': lv['level_name'],
                        'desc': lv['reason'], 'action': f"仓位 {lv['position']*100:.0f}%"}
                signals['hit'].append(item)
                signals['ordered'].append(item)
            elif lv['verdict'] == 'rebound':
                item = {'type': 'hit', 'tier': lv['level_name'],
                        'desc': lv['reason'], 'action': f"仓位 {lv['position']*100:.0f}%"}
                signals['hit'].append(item)
                signals['ordered'].append(item)
            elif lv['verdict'] == 'wait':
                if lv['level_key'] == 'pending' and lv['score_ok']:
                    item = {'type': 'pending', 'tier': lv['level_name'],
                            'desc': lv['reason'], 'pos': f"仓位 {lv['position']*100:.0f}%"}
                    signals['pending'].append(item)
                    signals['ordered'].append(item)
                else:
                    item = {'type': 'miss', 'tier': lv['level_name'], 'desc': lv['reason']}
                    signals['miss'].append(item)
                    signals['ordered'].append(item)
            else:
                item = {'type': 'miss', 'tier': lv['level_name'], 'desc': lv['reason']}
                signals['miss'].append(item)
                signals['ordered'].append(item)
        # 大盘熔断状态
        cb_lines = _circuit_breaker_lines(risk_params)
        if cb_lines:
            signals['circuit'] = cb_lines[0].strip().lstrip('ℹ️🛑 ').strip()
        else:
            signals['circuit'] = '大盘熔断未启用'

    # 核心结论：统一从 ctx.decision 单一真相源读取（与 report_builder 完全对齐，杜绝前后文不一致）
    decision_conclusion = _decision.badge_text
    sml = float(risk_params.get('single_max_loss_pct', 0) or 0)
    # 硬止损价：多头 成本×(1-loss)；空头反向 成本×(1+loss)（价格涨破触发）
    if has_position and sml > 0:
        hard_stop = entry_price * (1 + sml) if direction == 'short' else entry_price * (1 - sml)
    else:
        hard_stop = getattr(ctx, 'stop_loss', 0)
    hard_clear = False  # 预初始化，避免无持仓时引用未定义变量
    tsd = int(risk_params.get('time_stop_days', 0) or 0)
    tsmp = float(risk_params.get('time_stop_min_profit_pct', 0) or 0)

    if has_position:
        # 持仓结论：硬约束（时间止损 / 单笔最大亏损）优先级高于减仓档位
        rr = reduce_sug.get('reduce_ratio', 0) or 0
        hard_clear = (
            (tsd > 0 and bars >= tsd and pnl_pct < tsmp * 100)
            or (sml > 0 and pnl_pct <= -sml * 100)
        )
        if hard_clear or rr >= 1.0:
            position_conclusion = '🔴 清仓'
        elif rr > 0:
            # 标注合计档位
            _reduce_count = sum(1 for t in (reduce_sug.get('triggered_tiers', []) or []) if t.get('action') != '清仓')
            _tier_txt = f'（减仓①~减仓{_reduce_count}档合计）' if _reduce_count > 1 else ''
            position_conclusion = f"🔴 减仓当前持仓的 {rr * 100:.0f}%{_tier_txt}"
        elif add_sug.get('can_add') and add_sug.get('add_ratio', 0) > 0:
            position_conclusion = f"🟢 触发加仓 {add_sug['add_ratio'] * 100:.0f}%"
        else:
            position_conclusion = '✅ 正常持有'
        # 末尾展示建仓上下文
        position_conclusion += f" ｜ 📌 建仓价：{entry_price:.2f} ｜ 持有：{bars} 天"
    else:
        position_conclusion = decision_conclusion

    # 减仓/清仓档位明细
    from engine.report_builder import _clean_trigger_label, CN_TIER_NUM
    triggered_labels = {t.get('label', '') for t in (reduce_sug.get('triggered_tiers', []) or [])}
    reduce_tiers = []
    # 1) 硬清仓或清仓档触发时跳过减仓明细，清仓单独展示
    # 2) 减仓只展示 action!='清仓' 的已触发档，未触发档不展示
    # 3) 清仓板块：配置清仓档优先，无配置档时用硬止损/时间止损/单笔最大亏损兜底
    _is_clear_tier = (reduce_sug.get('reduce_ratio', 0) or 0) >= 1.0
    if hard_clear or _is_clear_tier:
        reduce_tiers = []  # 清仓场景跳过减仓明细
    else:
        for i, t in enumerate(reduce_sug.get('triggered_tiers', []) or [], 1):
            raw_action = t.get('action', '减仓')
            if raw_action == '清仓':
                continue  # 清仓档在 clear_tiers 中展示
            ratio = float(t.get('ratio', 0))
            price = float(t.get('price', 0))
            label = t.get('label', '')
            reduce_tiers.append({
                'tier': CN_TIER_NUM.get(i, str(i)),
                'label': _clean_trigger_label(label),
                'price': round(price, 2) if price else None,
                'action': '减仓',  # 归一化：reduce_engine 写入的 action 为「减仓15%」展示串，这里统一为「减仓」
                'ratio': f"{ratio * 100:.0f}%",
                'triggered': True,
                'drop_pct': None,
            })

    # 清仓档（配置清仓档优先，无配置档时用硬约束兜底）
    clear_tiers = []
    for t in (reduce_sug.get('triggered_tiers', []) or []):
        if t.get('action') == '清仓':
            price = float(t.get('price', 0))
            label = t.get('label', '')
            clear_tiers.append({
                'label': _clean_trigger_label(label),
                'price': round(price, 2) if price else None,
                'triggered': True,
                'reason': '配置清仓档',
                'ratio': '100%',  # 清仓档固定全仓，供前端明细展示（修复 #2）
            })
    if not clear_tiers and hard_stop > 0:
        # 无配置清仓档时，用硬止损/时间止损/单笔最大亏损兜底
        # 触发方向：多头 现价跌破硬止损；空头 现价涨破硬止损
        if direction == 'short':
            _sl_hit = latest > 0 and latest >= hard_stop
            _sl_reason = f'当前价{latest:.2f}≥硬止损{hard_stop:.2f}'
        else:
            _sl_hit = latest > 0 and latest <= hard_stop
            _sl_reason = f'当前价{latest:.2f}≤硬止损{hard_stop:.2f}'
        if _sl_hit:
            clear_tiers.append({
                'label': '硬止损/单笔最大亏损', 'price': round(hard_stop, 2),
                'triggered': True, 'reason': _sl_reason, 'ratio': '100%',
            })
        elif tsd > 0 and bars >= tsd and pnl_pct < tsmp * 100:
            clear_tiers.append({
                'label': '时间止损', 'price': None,
                'triggered': True, 'reason': f'持有{bars}天≥{tsd}天且收益{pnl_pct:+.1f}%<{tsmp*100:.0f}%',
                'ratio': '100%',
            })
        elif sml > 0 and pnl_pct <= -sml * 100:
            clear_tiers.append({
                'label': '单笔最大亏损', 'price': None,
                'triggered': True, 'reason': f'浮亏{pnl_pct:.1f}%≤-{sml*100:.0f}%',
                'ratio': '100%',
            })

    # 加仓档：逐档判定，展示命中档
    add_tiers_detail = []
    try:
        from engine.report_builder import _evaluate_add_tiers_lines, ADD_SLOT_LABEL
        add_eval = _evaluate_add_tiers_lines(ctx)
        for t in (add_eval.get('tiers') or []):
            if t.get('is_met'):
                add_tiers_detail.append({
                    'slot': ADD_SLOT_LABEL.get(t['slot'], t['slot']),
                    'trigger_label': t.get('trigger_label', ''),
                    'detail': t.get('detail', ''),
                    'ratio': f"{float(t.get('ratio', 0)) * 100:.0f}%",
                })
    except Exception:
        pass

    position_block = {
        'direction': direction,
        'cost': round(entry_price, 2),
        'days': bars,
        'current': round(latest, 2),
        'pnl': f"{pnl_pct:+.1f}%",
        'add': {
            'tier': '①',
            'desc': add_sug.get('reason', '未触发'),
            'ratio': f"{add_sug.get('add_ratio', 0) * 100:.0f}%",
            'tiers': add_tiers_detail,
        },
        'reduce': {
            'triggered': any(t.get('triggered') for t in reduce_tiers),
            'desc': reduce_sug.get('action', '正常持有') if reduce_sug else '正常持有',
            'tiers': reduce_tiers,
        },
        'clear': {
            'triggered': bool(clear_tiers),
            'tiers': clear_tiers,
        },
        'risk': {
            'time': {
                'held': bars,
                'threshold': int(risk_params.get('time_stop_days', 0) or 0),
                'min_profit': f"{float(risk_params.get('time_stop_min_profit_pct', 0) or 0) * 100:.0f}%",
                'triggered': tsd > 0 and bars >= tsd and pnl_pct < tsmp * 100,
            },
            'hardStop': {
                'price': round(hard_stop, 2),
                'pnl': f"{pnl_pct:+.1f}%",
                'threshold': f"-{sml * 100:.0f}%" if sml > 0 else '未启用',
                'triggered': sml > 0 and pnl_pct <= -sml * 100,
            },
        },
    }

    ghost_block = {
        'hold': {
            'ok': bool((ghost.get('rule1') or {}).get('result', False)) if ghost else True,
            'desc': (ghost.get('rule1') or {}).get('reason', '幽灵规则未启用或持仓正确') if ghost else '幽灵规则未启用或持仓正确',
            'action': (ghost.get('rule1') or {}).get('action', '') if ghost else '',
        },
        'risk': {
            'ok': not bool(reduce_sug.get('triggered_tiers')),
            'desc': '风险控制参考条件已触及（减仓/清仓档触发）' if reduce_sug.get('triggered_tiers') else '风险控制参考条件未触及',
        },
        'add': {
            'ok': bool((ghost.get('rule2') or {}).get('result', False)) if ghost else bool(add_sug.get('can_add')),
            'desc': (ghost.get('rule2') or {}).get('reason', add_sug.get('reason', '未触发')) if ghost else add_sug.get('reason', '未触发'),
        },
        'summary': ghost.get('summary', '') if ghost else '',
    }

    # 已触发条件 / 仓位 / 类型（自选股诊断表格专用）
    # 使用 _evaluate_add_tiers_lines 逐档判定（#016）
    triggered_conditions = ''
    if has_position:
        conds = []
        # 加仓档：逐档判定（含未触发档的触发条件说明）
        try:
            add_eval = _evaluate_add_tiers_lines(ctx)
            for t in (add_eval.get('tiers') or []):
                ratio = float(t.get('ratio', 0))
                if t.get('is_met'):
                    conds.append(f"加仓{t.get('slot','')}档（{t.get('trigger_label','')}）→ 加仓{int(ratio * 100)}%")
                # 未触发档不显示在"已触发条件"列，避免噪音
        except Exception:
            # 兜底：回退到原始 triggered_tiers
            if add_sug.get('can_add') and add_sug.get('triggered_tiers'):
                for i, t in enumerate(add_sug['triggered_tiers'], 1):
                    ratio = float(t.get('ratio', 0))
                    conds.append(f"加仓{i}档 → 加仓{int(ratio * 100)}%")
        if reduce_sug.get('triggered_tiers'):
            for i, t in enumerate(reduce_sug['triggered_tiers'], 1):
                raw_action = t.get('action', '减仓')
                ratio = float(t.get('ratio', 0))
                label = _clean_trigger_label(t.get('label', ''))
                price = t.get('price', None)
                # reduce_engine 把减仓档 action 写成「减仓15%」展示串、清仓档写成「清仓」，
                # 需归一化：清仓档 ratio>=1.0 → 清仓；否则统一为「减仓」
                is_clear = raw_action == '清仓' or ratio >= 1.0
                # 对齐加仓格式：减仓1档（触发条件）→ 减仓15%
                price_txt = f" {price:.2f}" if price else ''
                cond_label = f"（{label}{price_txt}）" if label else (f"（{price_txt.strip()}）" if price else '')
                conds.append(f"{'清仓' if is_clear else f'减仓{CN_TIER_NUM.get(i, str(i))}档'}{cond_label} → {'清仓 100%' if is_clear else f'减仓 {int(ratio * 100)}%'}")
        triggered_conditions = '；'.join(conds) if conds else '正常持有'
    else:
        entry_action = getattr(ctx, 'entry_action', '')
        # 触发集需含空单专属「顶部回落」（涨势猛+顶部信号→开空），漏掉会被误判"未触及"，
        # 且 L3 监控据此提取触发信号会漏推空单顶部回落
        if entry_action in ('关注建仓', '博反弹', '恐慌反转', '顶部回落'):
            triggered_conditions = '已触及建仓参考条件'
        elif getattr(ctx, 'status', '') == '回避':
            triggered_conditions = '回避'
        else:
            triggered_conditions = '未触及建仓参考条件'

    # 最新 K 线行情（收盘/非交易时段兜底）
    data_list = getattr(ctx, 'data_list', []) or []
    last = data_list[-1] if data_list else {}
    prev = data_list[-2] if len(data_list) > 1 else last
    close = float(last.get('close', latest)) if last else latest
    prev_close = float(prev.get('close', close)) if prev else close
    open_ = float(last.get('open', prev_close)) if last else prev_close
    high = float(last.get('high', close)) if last else close
    low = float(last.get('low', close)) if last else close
    volume = float(last.get('volume', 0)) if last else 0
    amount = float(last.get('amount', 0)) if last else 0
    change = close - prev_close if close and prev_close else 0
    change_pct = (change / prev_close * 100) if prev_close else 0

    return {
        'stock': stock_name,
        'code': ctx.stock_code,
        'datetime': ctx.query_time.strftime('%Y-%m-%d %H:%M'),
        'marketStatus': _market_status_label(ctx.query_time),
        'factorScore': round(final_score, 1),
        'viewDirection': getattr(ctx, 'direction', 'long'),
        'scoreLong': round(getattr(ctx, 'final_score_long', final_score) or 0, 1),
        'env': env_config.get('label', '中性'),
        'posLimit': f"{(risk_params.get('max_single_position') or 0) * 100:.0f}%" if risk_params.get('max_single_position') else '未设定',
        'stockType': stock_type,
        'status': getattr(ctx, 'status', '') or getattr(ctx, 'status_label', '') or stock_type,
        'entry_action': getattr(ctx, 'entry_action', ''),
        'has_position': has_position,
        'entry_price': entry_price,
        'decisionConclusion': decision_conclusion,
        'positionConclusion': position_conclusion,
        'signals': signals,
        'plus': plus,
        'minus': minus,
        'total': f"{raw_total:+.0f}",
        'truncated': round(final_score, 1),
        'resonance': {
            'enabled': tech_resonance_enabled,
            'strength': strength_str if strength_str else '—',
            'ratio': ratio_str if ratio_str else '—',
            'threshold': threshold if tech_resonance_enabled else None,
            'plus': sig_plus,
            'minus': sig_minus,
        },
        'bullBear': bull_bear,
        'bullBearSummary': bull_bear_summary,
        'positionBullBearSummary': position_bull_bear_summary,
        'tech_board': _build_tech_board(ctx),
        # ↓ 供 AI「查漏补缺」用的两层原始数据（前端不消费，H5 也不读，纯 AI 输入）
        #   techRaw：ctx.tech 全量原始指标读数（与方案无关、客观），
        #            区别于 plus/minus（方案选中因子的贡献分，是二手且被裁剪的子集）
        #   recentBars：近 30 根 K 线，补上平滑指标抹掉的形态信息
        'techRaw': _json_safe(tech),
        'recentBars': _recent_bars(data_list, 30),
        'keyLevels': {
            'current': round(latest, 2),
            'resistance': {'price': round(resistance, 2), 'chg': _pct_chg(resistance, latest)},
            'support': {'price': round(support, 2), 'chg': _pct_chg(support, latest)},
        },
        'quote': {
            'open': round(open_, 2), 'high': round(high, 2), 'low': round(low, 2),
            'close': round(close, 2), 'prev_close': round(prev_close, 2),
            'volume': volume, 'amount': amount,
            'change': round(change, 2), 'change_pct': round(change_pct, 2),
        },
        'position': position_block,
        'ghost': ghost_block,
        'triggeredConditions': triggered_conditions,
        'positionAllocation': f"{getattr(ctx, 'entry_position', 0) * 100:.0f}%",
        'stockCurrentType': stock_type,
        'dynamicPosition': getattr(ctx, 'dynamic_position', None),  # 预留动态仓位渲染（#029）
        'report_text': ReportBuilder.build(ctx),
    }

# 服务端消费入口（服务端不依赖下划线私有名）
build_report_data = _build_report_data
scan_stats = _scan_stats
