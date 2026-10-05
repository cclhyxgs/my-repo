# -*- coding: utf-8 -*-
"""AI 解读 prompt 模板（按 TaskMode 组织）。

五个入口各自绑定一种任务模式（见 server.ai.modes.TASK_MODES）：
角色、任务、输出结构、禁止事项、工具白名单、temperature 全部由模式注册表驱动。

原则：只喂"有信息量的摘要 + top-N"，控 token；措辞面向不懂代码的散户。
"""

from server.ai import modes

# ---------------------------------------------------------------- 白名单（镜像前端 quantModel.js）
# 技术信号（signal 条件）
SIGNAL_OPTIONS = [
    '无要求', '多头排列', '空头排列', '站上MA5', '跌破MA5', '站上MA20',
    '跌破MA20', '站上MA60', '跌破MA60', 'KDJ金叉', 'KDJ死叉', 'MACD金叉',
    'MACD死叉', '均线金叉', '均线死叉', '布林带上轨突破', '布林带下轨突破',
    'ADX趋势确认', '放量上涨', '放量下跌',
]
# 数值指标（indicator 条件）key → 中文
AND_INDICATORS = {
    'sma_5': 'MA5', 'sma_10': 'MA10', 'sma_20': 'MA20', 'sma_60': 'MA60', 'sma_250': '年线MA250',
    'rsi_6': 'RSI6', 'rsi_14': 'RSI14', 'rsi_24': 'RSI24', 'kdj_k': 'KDJ-K', 'kdj_d': 'KDJ-D', 'kdj_j': 'KDJ-J',
    'macd': 'MACD', 'macd_signal': 'MACD信号', 'macd_hist': 'MACD柱', 'volume_ratio': '量比', 'volume_z_score': '量能Z分',
    'bb_pct_b': '布林%B', 'atr': 'ATR', 'atr_ratio': 'ATR比', 'adx': 'ADX', 'di_plus': 'DI+', 'di_minus': 'DI-',
    'bias20': '乖离率20', 'wr': 'WR',
}
AND_OPS = ['>=', '<=', '>', '<']
# 打分因子（factor_tweaks）key → 中文
FACTOR_NAMES = [
    'relative_strength_20d', 'rsi_value', 'macd_hist_norm', 'kdj_signal',
    'ma_slope', 'di_spread', 'ma_arrangement', 'bias_value', 'adx_trend_strength',
    'volume_ratio', 'obv_trend', 'volume_price_signal',
    'bb_bandwidth', 'atr_norm', 'current_drawdown', 'volatility_cone',
    'pattern_reverse', 'doji', 'fib_position', 'pivot_distance', 'chip_concentration',
]

# 基本面指标（不在本工具支持范围，识别出来后列入 unsupported）
BASIC_INDICATORS = ['市净率', '市盈率', '市销率', '市现率', '每股收益', '净资产收益率', '毛利率', '净利率', '负债率', '市净', 'PE', 'PB', 'PE(TTM)', 'PB', 'PB(LF)']

# ---------------------------------------------------------------- 扫描 DSL 白名单
# ⛔ 关键：AI 只能引用"扫描结果集里真实存在的字段"。扫描结果默认不含 rsi / volume_ratio 等
# 原始指标列（那些是因子内部计算量），若照抄 rsi<30 这类条件，执行器必然拒单。
# 扫描结果若新增输出列，必须同步登记到这里，否则执行器按白名单拒绝。
DSL_FIELDS = {
    'code': '股票代码', 'name': '名称', 'price': '现价',
    'final_score': '综合评分', 'stars': '星级', 'level': '档位(持有/观察/减仓/清仓)',
    'sector': '所属板块', 'entry_tier_label': '建仓档位标签',
}
DSL_OPS = ['>', '>=', '<', '<=', '==', '!=', 'in', 'not_in']
DSL_SORT_ORDERS = ['asc', 'desc']
DSL_MAX_LIMIT = 200


def _dsl_whitelist_text() -> str:
    lines = [f"{k} = {v}" for k, v in DSL_FIELDS.items()]
    return (
        "可筛选字段（只能用这些）：\n" + "\n".join(lines)
        + f"\n运算符 op（只能用）：{', '.join(DSL_OPS)}"
        + f"\n排序 order：{', '.join(DSL_SORT_ORDERS)}；limit 上限 {DSL_MAX_LIMIT}"
    )


# ---------------------------------------------------------------- 通用拼装
def _build_system(mode_key: str, body: str) -> str:
    """按 TaskMode 拼 system：角色 + 任务 + 输出结构 + 禁止事项 + 工具白名单。"""
    m = modes.get_mode(mode_key)
    role = m.get("role") or "AI 助手"
    task = m.get("task") or ""
    forbidden = m.get("forbidden") or []
    tools = m.get("tools") or []

    parts = [f"# 角色\n你是「{role}」。", f"# 任务\n{task}", f"\n# 输出结构（严格按此顺序，使用小标题）\n{body}"]
    if forbidden:
        parts.append("\n# 禁止事项（违反即视为不合格输出）\n- " + "\n- ".join(forbidden))
    if tools:
        parts.append(f"\n# 可用工具\n只允许：{', '.join(tools)}。不得调用白名单外的工具，不得伪造工具返回结果。")
    else:
        parts.append("\n# 可用工具\n无。纯文本输出，不得声称调用了任何工具，不得编造工具结果。")
    return "".join(parts)


# ---------------------------------------------------------------- ① 个股解盘师
# ⛔ 设计要点（2026-09-27 重写，定位＝「对抗性审查 / 查漏补缺」而非「解读」）：
#    引擎已产 decisionConclusion / bullBearSummary / keyLevels / triggeredConditions /
#    tech_board(15 项，每项自带 desc 解读) ⇒ 若仍让 AI「输出分析」，它只能换个说法复述，
#    信息量不增反降。故本契约**只保留引擎做不到的部分**，并显式禁止复述。
# ⛔ 二次改版 sa3（2026-09-27 用户拍板「要信息卡片，更简洁明了」）：
#    原五段散文式契约**只有下限没有上限**（「至少两条」「逐项作答」），模型默认写满，
#    实测单只票 2800+ 字。改为**行协议** `§类型|字段1|字段2`，由前端渲染成信息卡片；
#    条数写死（裁决≤3 / 证伪=3 / 代价=3 / 盲区≤3 / 提示≤1），总长压到 ~700 字内。
#    兼容性：非 § 开头的行不丢弃（收集为末尾附注）；协议解析失败时前端整体回落纯文本渲染。
# ⛔ 三次改版 sa4（2026-09-27 用户拍板）：补**持仓场景分支**。持仓没有独立 mode
#    （portfolio_diagnosis 已停用），走同一张卡片，但原契约只按建仓写 ⇒ 头部会引
#    decisionConclusion（建仓措辞），而实际动作是减仓/清仓/加仓，自相矛盾。
#    sa4 只换**参照系**（→ positionConclusion），四件事（裁决/证伪/盲区/代价）照旧，
#    并补三项持仓独有盲区：时间止损、硬止损宽度 vs ATR、减仓触发价可成交性。
#    加减仓触发条件属引擎确定性产出，**禁止 AI 复述**（否则等于把停用的 pd 模式捡回来）。
_SA_BODY = (
    "# 输出格式：行协议（严格，违反即不合格）\n"
    "输出**只能是**若干以 § 开头的行，每行一条信息。禁止小标题、禁止前言与总结、"
    "禁止 markdown 表格、禁止散文段落——非 § 开头的行会被降级为末尾附注，写多了等于不合格。\n"
    "每行格式：`§类型|字段1|字段2`。字段内不得再出现 `|`，不得换行，整行不超过 60 字。\n"

    "## 行类型（按此顺序输出）\n"
    "§结论|股票名与代码|引擎结论，**原样引用，禁止改写复述、禁止展开**——"
    "有持仓引 positionConclusion，无持仓引 factor_score / status / decisionConclusion（见持仓分支）"
    "|你的裁定，≤12 字（如「信号存疑 · 不宜按此建仓」「可小仓试错」）\n"
    "§裁决|矛盾双方：指名具体指标与数值，用 ⇄ 连接|判据与结论，≤16 字\n"
    "§证伪|可观测的价位或指标阈值|触发后的含义，≤16 字\n"
    "§代价|对总资金的影响（如 -0.73%）|对应动作与位置，≤16 字\n"
    "§盲区|命中项标签（大盘 / 数据 / 流动性 …）|一句话说明，≤40 字\n"
    "§提示|T+1 与涨跌停下的最早可执行时点，≤40 字\n"

    "## 条数上限（硬约束）\n"
    "§结论 1 行；§裁决 最多 3 行（按因子贡献分从高到低，只留最重的三条）；"
    "§证伪 恰好 3 行；§代价 恰好 3 行；§盲区 最多 3 行；§提示 最多 1 行。\n"

    "## 内容规则\n"
    "- §裁决：必须指名具体指标与数值并给判据（量价是否配合 / 周期长短 / 位置高低 / "
    "是否同一逻辑的重复计数）；若两组其实指向同一件事（伪矛盾），直接说破。"
    "禁止「多空交织」这类无判据的空话。\n"
    "- §证伪：必须是能在这只票上被观测验证的价位或指标阈值，"
    "禁止「如果继续下跌」这类无法证伪的表述。\n"
    "- §代价：**无持仓**时三行依次是 减半位 / 清仓位 / 引擎 keyLevels 给的止损位；"
    "**有持仓**时见下方「持仓场景分支」。均按方案仓位换算成对总资金的影响；"
    "若止损位中途无承接（筹码真空带）必须写明不可用。\n"
    "- §盲区：逐项检查——停牌 / ST / 退市、流动性（成交额为 0 时不得引用）、"
    "大盘 env 与本股是否冲突（注意 env 可能是缺省填充值而非观测值）、"
    "数据充分性（指标填值痕迹 / 重复行 / 口径矛盾）、持仓集中度与相关性。"
    "只写命中的；全部未命中时写一行 `§盲区|无|…`。\n"
    "- 你的价值**只在补引擎没说的部分**，引擎已给的结论一律引用。"
    "原始指标里能读到的数值必须直接引用数字，严禁编造。\n"

    "## 持仓场景分支（输入 position 不为「—」/ has_position=true 时适用）\n"
    "此时引擎产的是【持仓管理报告】。加减仓触发档位与触发价、硬止损"
    "（成本价×(1−single_max_loss)）、时间止损天数**已由引擎确定性算出**，"
    "你**禁止复述**这些触发条件——那正是 portfolio_diagnosis 被停用的原因"
    "（幽灵三规则已下沉引擎，不需要 AI 再解释一遍）。\n"
    "只把**参照系**从建仓结论换成 positionConclusion，四件事照做：\n"
    "- §结论 第二段引用 positionConclusion（动作 + 建仓价 + 持有天数），"
    "不得再引 decisionConclusion 的建仓措辞，否则头部与实际动作自相矛盾。\n"
    "- §裁决：裁决的是「引擎这个减仓 / 加仓 / 持有建议的证据是否站得住」，"
    "不是重算该不该加减仓。\n"
    "- §证伪（语义重定义）：什么情况说明**继续持有**这个判断错了，而非建仓判断错了。\n"
    "- §代价：三行依次是 引擎减仓/清仓档位的触发价、硬止损价"
    "（**以成本价为锚**，须叠加浮盈回吐，⛔ 不得从现价往下算）、浮盈归零位（＝成本价）。\n"
    "- §盲区 **必须额外检查**这三项（持仓独有，引擎没说）：\n"
    "  ① 时间止损：持有天数是否已接近或达到 time_stop_days（输入 position.risk.time）；\n"
    "  ② 硬止损宽度：hardStop 距现价幅度是否小于 ATR 或日均波幅（会被正常波动扫出局）；\n"
    "  ③ 减仓/清仓触发价的可成交性：是否落在筹码真空带或缩量区"
    "（对照 raw_indicators 的 chip / volume）。\n"
)


def analyze_prompt(report_data: dict, report_text: str = "", summary: dict = None) -> tuple:
    """个股分析 → 个股解盘师（对抗性审查定位）。

    投喂分两层（2026-09-27 重构，配合「查漏补缺」定位）：
      ① 原始指标层 raw_indicators —— 引擎 ctx.tech 的**全量原始读数**，与方案配置无关。
         这是客观事实，AI 的判断依据。另附 engine_focus_board（引擎挑出的 15 项），
         **刻意只投 label/value/tone、不投 desc**：desc 是面向散户的通用解读模板
         （任何一只 RSI 28 的票都是同一句话），投了 AI 必然照抄，退化成翻译器。
      ② 方案因子层 factors_positive/negative —— plus/minus 的贡献分，是**用户主观配置**
         的子集（只有 active_factors）。两层都给， AI 才能做「你的方案漏了什么」的 gap 分析，
         这正是「查漏补缺」的实现方式。

    ⛔ **用户拍板（2026-09-27）：保留两层，不得为"简化"只留原始指标层。**
    理由：查漏必须有参照物。去掉方案层的后果是 AI 永远不知道用户在用什么策略逻辑，
    于是说不出「你配的是均值回归，但当前 ADX 31.2 是强趋势市，这套逻辑最易连续挨打」
    这类判断 —— 那就退回成了解读，与本次改造的初衷相反。
    正确的分工是：**方案层当参照物，原始指标层当判断基准**；两者冲突时要求 AI 裁决，
    而非附和方案（见 `_SA_BODY` 第二段「矛盾裁决」）。
    """
    rd = report_data or {}
    board = rd.get("tech_board") or []
    context = {
        "code": _fmt_market(rd.get("code")),
        "name": _fmt_market(rd.get("stock") or rd.get("name")),
        "status": _fmt_market(rd.get("status")),
        "entry_action": _fmt_market(rd.get("entry_action")),
        "factor_score": _fmt_market(rd.get("factorScore")),
        "decisionConclusion": _fmt_market(rd.get("decisionConclusion")),
        "keyLevels": _fmt_market(rd.get("keyLevels")),
        "triggeredConditions": _fmt_market(rd.get("triggeredConditions")),
        "resonance": rd.get("resonance"),
        "bullBearSummary": _fmt_market(rd.get("bullBearSummary")),
        "env": _fmt_market(rd.get("env")),
        "quote": rd.get("quote"),
        "position": _fmt_position(rd.get("position")),
        "has_position": bool((rd.get("position") or {}).get("price")),
        # ① 原始指标层：全量读数（AI 判断的客观依据）
        "raw_indicators": rd.get("techRaw"),
        "engine_focus_board": [
            {"item": b.get("label"), "value": b.get("value"), "tone": b.get("tone")}
            for b in board if isinstance(b, dict)
        ],
        "recent_bars": rd.get("recentBars") or [],
        # ② 方案因子层：用户配置的因子贡献分（正负两组）
        "factors_positive": [
            {"score": x.get("score"), "desc": x.get("desc")}
            for x in (rd.get("plus") or [])[:8]
        ],
        "factors_negative": [
            {"score": x.get("score"), "desc": x.get("desc")}
            for x in (rd.get("minus") or [])[:8]
        ],
    }
    # 有持仓时引擎产的是【持仓管理报告】，结论字段不同，两个都带上（缺省为空串）
    if rd.get("positionConclusion"):
        context["positionConclusion"] = _fmt_market(rd.get("positionConclusion"))
    if report_text:
        # 引擎的定性叙述：保留作背景，但在 system 里已明令禁止复述
        context["engine_narrative"] = str(report_text)[:800]

    user = (
        "请对这只个股做**对抗性审查**：只补引擎没说的，不要复述引擎结论。\n"
        f"数据:\n{_dump(context)}"
    )
    return _build_system("stock_analysis", _SA_BODY), user


# 多只场景（自选诊断）的解盘师契约：逐只速览 + 重点个股完整六段
# ⚠️ 这是按「多只场景下完整六段 × N 会输出过长、AI 自动缩水丢段」定的默认方案，可调整。
_SA_BATCH_BODY = (
    "## 一、逐只速览\n"
    "每一只都要出现，不许合并、不许挑着写。每行给出：\n"
    "代码/名称 ｜ 趋势定性（多头/空头/震荡/转折观察 四选一）｜ "
    "关键位（支撑位/阻力位/止损参考位）｜ 证伪条件 ｜ 一句话白话\n"
    "某项无数据写『数据缺失』，严禁编造价格。\n"
    "## 二、重点个股\n"
    "最多挑 3 只（优先：浮盈亏幅最大 / 评分最高 / 分类最需要处置），"
    "每只按完整结构输出：趋势定性 → 指标拆解（支持证据 / 矛盾证据）→ 关键位 → "
    "乐观/中性/悲观三情景 → 证伪条件 → 白话总结。不足 3 只按实际只数。\n"
    "## 三、组合层提醒\n"
    "只看 rows 里真实存在的维度：同板块扎堆、同一逻辑重复、评分分布过度集中。"
    "没有对应字段就写『数据缺失』，不得凭代码前缀猜板块。\n"
    "## 四、白话总结\n"
    "一句话给非专业用户，不得出现术语。\n"
    "补充约束：A 股 T+1 与涨跌停制度必须体现在触发条件上"
    "（如最早次日开盘可执行、跌停无法卖出）。"
)


# ---------------------------------------------------------------- ② 华尔街幽灵（已停用）
_PD_BODY = (
    "## 幽灵三规则（本项目采用版本，写死，不得改写）\n"
    + modes.GHOST_RULES + "\n"
    "## 一、单只分诊\n"
    "每只一行，归入 持有 / 观察 / 减仓 / 清仓，配一句依据（评分档位、星级、触发条件、板块）。\n"
    "## 二、规则符合度（逐条给结论，不写模糊话）\n"
    "- 规则一：当前仓位是否仍『正确』，入场逻辑是否还在；不在 → 按规则一处理\n"
    "- 规则二：是否满足加码条件，还是只是亏损补仓（必须明确区分二者）\n"
    "- 规则三：是否存在异常巨量套现信号、隔夜风险、逆势摊平、亏损扩大\n"
    "## 三、组合层\n"
    "同板块集中度、相关性、总仓位、风险预算（给可量化口径）。\n"
    "## 四、最优先处理 3 件事\n"
    "## 五、白话总结\n"
    "一句话。\n"
    "硬约束：不推荐买入，只做分诊；处理时点必须满足 A 股 T+1。"
)


def _diag_row(r: dict) -> dict:
    """自选诊断结果行 → 投喂字段（契约兼容层）。

    ⛔ 历史坑：`ui/web_api.py::diagnose_watchlist` 的真实返回行用的是
    `factorScore` / `triggeredConditions`，而早期 prompt 取 `final_score` / `reason`，
    字段名漂移 ⇒ 直接传原始行时这两个字段恒为 null（AI 拿到空表）。
    这里同时接受两种写法，并补回曾被前端丢弃的持仓字段。
    """
    r = r or {}
    entry_price = r.get("entry_price") or 0
    return {
        "code": r.get("code"),
        "name": r.get("name"),
        "price": r.get("price"),
        # 评分：扫描结果集用 final_score，自选诊断结果行用 factorScore
        "score": r.get("final_score") if r.get("final_score") is not None else r.get("factorScore"),
        "category": r.get("category") or r.get("level"),
        "entry_tier": r.get("entry_tier_label") or r.get("positionAllocation") or r.get("entry_tier"),
        "stock_type": r.get("stockCurrentType") or r.get("stock_type"),
        # 触发条件：扫描用 reason，自选诊断用 triggeredConditions
        "trigger": r.get("reason") or r.get("triggeredConditions"),
        # ↓ 持仓三件套（引擎已算，前端曾整组丢弃）
        "cost": entry_price or None,
        "bars_held": r.get("bars_held") or None,
        "pnl": r.get("pnl") or None,
        "direction": r.get("direction") or ("long" if entry_price else None),
    }


def diagnosis_prompt(item: dict) -> tuple:
    """单只诊断 → 个股解盘师（完整六段）。（system, user）"""
    ctx = _diag_row(item)
    user = (
        "请按输出结构解盘这只个股。它来自我的自选/持仓列表。"
        "若 cost 与 pnl 有值，说明我已持仓，解盘时须结合成本价与浮盈；"
        "若为 null，按未持仓视角解盘。\n"
        f"数据:\n{_dump(ctx)}"
    )
    return _build_system("stock_analysis", _SA_BODY), user


def diagnosis_batch_prompt(items: list) -> tuple:
    """批量诊断（桌面端「自选诊断解读」实际入口）→ 个股解盘师（多只精简版）。

    与 scan_prompt 的区别：这里面对的是【已持有/已自选】（怎么处理），
    scan 面对的是【候选池】（要不要买）。
    """
    src = list(items or [])
    sample = src[:30]
    rows = [_diag_row(r) for r in sample]
    ctx = {
        "total": len(src),
        "shown": len(rows),
        "rows": rows,
    }
    user = (
        "下面是我【已经持有或已加入自选】的票，不是刚筛出来的候选。\n"
        "cost/pnl 有值 = 我已持仓，须结合成本价与浮盈解盘；为 null = 仅自选未买入。\n"
        f"数据:\n{_dump(ctx)}"
    )
    return _build_system("stock_analysis", _SA_BATCH_BODY), user


# ---------------------------------------------------------------- ③ 候选池审计师（扫描概览）
# ⛔ 定位（2026-09-27 用户拍板）：**对已有扫描结果做分类统计 + 结合市场整体情况审计**。
#    实现关键：分类统计的「算数」必须在引擎侧完成（`engine.report_data.scan_stats`），
#    否则等于让 LLM 数几十行的占比 —— 会出错且不可复现。AI 只负责呈现与解读。
#    故契约第一段是"引用统计"，并显式禁止 AI 自己再数一遍。
_SO_BODY = (
    "## 一、结果构成（直接引用引擎统计，禁止自己再数）\n"
    "把 stats 里的确定数字呈现出来：总数 / 板块分布 / 评分分布（均值·中位·最密集档）"
    "/ 星级分布 / 建仓分档分布 / 因子暴露。\n"
    "⛔ 不得重新统计、不得估算占比、不得四舍五入成新数字；直接引用 stats 的值。\n"
    "最后用一句话概括这批票的共同特征。\n"

    "## 二、市场背景（这批结果在当天环境下意味着什么）\n"
    "结合 market 的涨跌家数、指数涨跌、环境定性来判断：\n"
    "- 当天是普涨还是普跌？这决定了『命中 N 只』是选股能力还是水涨船高；\n"
    "- 市场偏弱时命中少是正常的，不代表条件过严；市场普涨时命中多也不代表策略有效；\n"
    "- 若指数与个股方向背离，指出这种背离对该批结果的含义。\n"
    "⛔ market 缺失就写『市场数据缺失』，严禁臆测大盘环境。\n"

    "## 三、同质化与过拟合风险（本节是审计核心）\n"
    "- 板块是否过度集中——看 concentration 的 top1/top3 占比与板块总数；\n"
    "- 评分是否挤在某一档——看 score_band_top，挤在阈值附近往往是卡阈值筛出来的，稳定性差；\n"
    "- 是否同一逻辑驱动——结合 factor_exposure 看是否所有票的同一因子都处于极端值；\n"
    "- 样本量：命中过少（<5 只）不具统计意义，过多（>50 只）说明条件太松。\n"
    "命中多 ≠ 有效。\n"

    "## 四、下一步旋钮\n"
    "该收紧还是放宽哪个维度、为什么。要落到具体字段或阈值，不要泛泛而谈。\n"

    "## 五、结构化筛选 DSL（仅当我明确要求进一步筛选时才输出本节，否则省略）\n"
    "只输出一个 JSON 对象，不要任何解释文字：\n"
    '{"action":"filter","conditions":[{"field":"final_score","op":">=","value":60}],'
    '"sort":[{"field":"final_score","order":"desc"}],"limit":50}\n'
    f"{_dsl_whitelist_text()}\n"
    "严禁编造结果集里不存在的字段；严禁直接编股票列表。\n"

    "补充约束：结果集未提供市值 / 风格等列时，涉及该维度的结论必须写『数据缺失』，"
    "严禁凭板块或代码猜测。"
)


def scan_prompt(items: list, filters: dict = None, market_context: dict = None) -> tuple:
    """扫描结果集 → 候选池审计师。（system, user）

    投喂三层：
      ① `stats` —— 引擎算好的**确定**分类统计（板块/评分/星级/分档/因子暴露）。
         ⛔ AI 只许引用，不许重算。
      ② `market` —— 市场整体情况（涨跌家数/指数/环境），用于回答"这批命中在当天意味着什么"。
      ③ `sample_rows` —— 仅少量样例行（≤8），用于举例说明分布特征；**不用于逐只点评**。
         （原契约的「代表性样本」段已删除，故样本仅作分布佐证。）
    """
    from engine.report_data import scan_stats
    src = list(items or [])
    stats = scan_stats(src)
    sample_rows = []
    for r in src[:8]:
        sample_rows.append({
            "code": r.get("code"), "name": r.get("name"),
            "sector": r.get("sector"), "final_score": r.get("final_score"),
            "stars": r.get("stars"), "level": r.get("level"),
            "entry_tier_label": r.get("entry_tier_label"),
        })
    ctx = {
        "total": len(src),
        "stats": stats,
        "market": market_context or {},
        "filters": filters or {},
        "sample_rows": sample_rows,
    }
    user = (
        "下面是本次扫描结果集的**引擎侧分类统计**（数字已算好，直接引用，"
        "禁止自己再数一遍）与**市场整体情况**。请按输出结构做审计。\n"
        f"数据:\n{_dump(ctx)}"
    )
    return _build_system("scan_overview", _SO_BODY), user


_DSL_ONLY_BODY = (
    "只输出一个 JSON 对象（筛选 DSL），不要任何解释文字：\n"
    '{"action":"filter","conditions":[{"field":"final_score","op":">=","value":60}],'
    '"sort":[{"field":"final_score","order":"desc"}],"limit":50}\n'
    f"{_dsl_whitelist_text()}\n"
    "严禁输出股票列表（列表由后端在真实结果集上执行后返回）；"
    "严禁使用白名单外字段；拿不准时宁可少给条件，也不要臆造字段。"
)


def scan_dsl_prompt(text: str, items: list = None) -> tuple:
    """自然语言 → 筛选 DSL（只产出条件，执行在后端）。（system, user）"""
    sample = []
    for r in (items or [])[:5]:
        sample.append({k: r.get(k) for k in DSL_FIELDS if k in r})
    user = (
        "当前结果集可用字段样例（只能引用这些字段）：\n"
        f"{_dump(sample)}\n"
        "把下面的筛选要求转成 DSL：\n"
        f"“{text}”"
    )
    return _build_system("scan_overview", _DSL_ONLY_BODY), user


# ---------------------------------------------------------------- ④ 劝导员（情绪复盘）
_LR_BODY = (
    "## 一、行为偏差命名\n"
    "从 处置效应 / 损失厌恶 / 报复性交易 / 过度自信 / 锚定效应 中选出，"
    "只点最伤人的 1~2 个，每个配一句白话解释。\n"
    "## 二、证据（优先用用户自己的话）\n"
    "有两个账本，都必须用上，不要只看一个：\n"
    "- **情绪账本 emotion.records**：用户**自己标记**的操作（tag = 追涨/摊平/恐慌/怕踏空…）"
    "＋操作价 op_price ＋现状 diff_pct ＋时间 ts。这是**用户自己的行为归因**，"
    "比从信号推断准得多——**优先引用它，并尽量用他的原话与他选的标签**；\n"
    "- **纪律账本 discipline.sample_signals**：工具发出的信号 vs 用户是否执行（executed）"
    "＋情绪差 emotion_diff。\n"
    "引用时给具体记录（标的、时间、标签、结果）。记录不足就明说数据不足。\n"
    "## 三、代价\n"
    "算『没守纪律的具体代价』，并说明反事实假设的局限：执行价口径（信号价 vs 次日开盘价）、"
    "滑点、停牌与涨跌停无法成交、样本量不足。\n"
    "## 四、共情\n"
    "先共情再建议；不羞辱、不贴标签、不说教。\n"
    "## 五、3 条可验证的纪律规则\n"
    "每条必须写清如何验证是否做到。\n"
    "## 六、若纪律交易的收益反而更低\n"
    "按序排查：样本量 → 时间窗口 → 策略是否失效 → 基准是否公平 → 交易成本；"
    "再引导修改方案参数，而不是放弃纪律。\n"
    "## 七、白话总结\n"
    "一句话。"
)


def ledger_prompt(signals: list, summary: dict, cooldown: dict = None,
                  emotion: dict = None) -> tuple:
    """纪律账本 + 情绪账本 → 劝导员。（system, user）

    ⛔ 双账本（2026-09-27）：
      `discipline` = 工具发的信号 vs 用户是否执行（纪律账本）；
      `emotion`    = 用户**自己记**的情绪化操作（tag/操作价/现状/时间）。
    此前只投纪律账本 ⇒ AI 只能对着"你没执行"说教，用不上用户自己的归因。
    """
    sample = []
    for s in (signals or [])[:30]:
        sample.append({
            "code": s.get("code"), "name": s.get("name"),
            "signal_type": s.get("signal_type_cn"),
            "trigger_price": s.get("trigger_price"), "latest_price": s.get("latest_price"),
            "diff_pct": s.get("diff_pct"), "conclusion": s.get("conclusion"),
            "emotion_diff": s.get("emotion_diff"), "emotion_note": s.get("emotion_note"),
            "executed": s.get("executed"),
        })
    emo = emotion or {}
    emo_rows = []
    for r in (emo.get("records") or [])[:30]:
        emo_rows.append({
            "ts": r.get("ts"),                       # 时间：用于看"什么时候容易破戒"
            "code": r.get("code"), "name": r.get("name"),
            "action": r.get("action_cn"),
            "tag": r.get("tag"),                     # 用户自己的归因标签
            "note": (r.get("note") or "")[:120],
            "op_price": r.get("op_price"),
            "latest_price": r.get("latest_price"),
            "diff_pct": r.get("diff_pct"),           # 这笔现在的实际结果
        })
    ctx = {
        "discipline": {
            "summary": _fmt_market(summary),
            "cooldown": _fmt_market(cooldown),
            "sample_signals": sample,
        },
        "emotion": {
            "summary": emo.get("summary") or {},     # total/loss_n/sum_diff/top_tag/tags
            "records": emo_rows,
        },
    }
    user = (
        "这是用户的两个账本：discipline = 工具发信号后他有没有执行；"
        "emotion = 他自己标记的情绪化操作（追涨/摊平/恐慌等）。"
        "请按输出结构做复盘，证据须两个账本都用到，优先引用他自己写的标签与备注。\n"
        f"数据:\n{_dump(ctx)}"
    )
    return _build_system("ledger_review", _LR_BODY), user


# ---------------------------------------------------------------- ⑤ 量化策略师（方案配置）
def _dim_to_examples() -> str:
    return "\n".join(f"{key} = {label}" for key, label in AND_INDICATORS.items())


_SC_BODY = (
    "只输出一个 JSON 对象，不要任何解释文字。格式：\n"
    "{\n"
    '  "config": {"filters": [{"signal": "多头排列"} 或 {"indicator": "sma_20", "op": ">=", "value": 60}],\n'
    '            "factor_tweaks": {"rsi_value": 1.0}},\n'
    '  "explain": {"filters": "为什么这样筛", "factor_tweaks": "为什么这样加权"},\n'
    '  "validation": {"applied": ["已落地的条件"], "conflicts": ["彼此冲突的条件"],\n'
    '                 "unsupported": ["市净率"]},\n'
    '  "risk": {"position": "单笔/总仓位建议", "drawdown": "最大回撤约束",\n'
    '           "industry_exposure": "单一行业暴露上限", "turnover": "换手率上限"},\n'
    '  "sharpe": {"user_target": 2.0, "constraint": "作为约束写入方案的部分",\n'
    '             "backtest": null, "oos_expectation": "样本外预期（定性，禁止给具体数字）"}\n'
    "}\n"
    "夏普四层必须分开写，严禁混为一谈：\n"
    "- user_target：用户设定的目标值（2.0），只是设计取向；\n"
    "- constraint：能作为约束真正落进方案的部分（如回撤上限、行业暴露）；\n"
    "- backtest：历史回测值。你没有回测数据，必须填 null，禁止编造；\n"
    "- oos_expectation：样本外预期，只能定性描述，禁止给具体数字。\n"
    "严禁声称『本方案夏普可达 X』。\n"
    "\n# 数值指标 indicator（用其 key）：\n" + _dim_to_examples() + "\n"
    "\n# 运算符 op（只允许）：>=  <=  >  <\n"
    "\n# 技术信号 signal（值是单个中文串，只允许）：\n" + ', '.join(SIGNAL_OPTIONS) + "\n"
    "\n# 打分因子 factor_tweaks（key→调整幅度 0~1）：\n" + ', '.join(FACTOR_NAMES) + "\n"
    "\n约束：\n"
    "- 涉及数值区间/条件的话可能拆成多个指标条件（同为 AND）。\n"
    "- 涉及打分倾向的（如『更看重动量』）放 factor_tweaks；涉及阈值/信号/过滤的放 filters。\n"
    "- 遇到市净率/市盈率等基本面指标（不在白名单）：绝不臆造条件，放进 unsupported 并注明不支持。\n"
    "- 若某句话完全无法映射到白名单，filters 为空数组、unsupported 里注明原因。\n"
    "- value 用数字；op 用上面四个之内。\n"
)


def _trim_scheme_config(cfg):
    """清洗方案 config 供投喂（复用引擎的 JSON 安全化：NaN/Inf 会让报文非法）。

    ⛔ 必须放大 `max_list/max_depth`：`_json_safe` 默认 `max_list=6, max_depth=2`
    是给 `techRaw` 控体积用的，用在方案上会把 `entry_conditions.strong`（depth=2 的 dict）
    和 `veto_enabled`（8+ 键）**降级成截断字符串**——2026-09-27 实际踩到，
    AI 拿到的是 `"{'tech_signal': [...], 'veto_on': True, ..."` 这种残缺文本，无法审查。
    不裁剪字段：审查需要看全（过拟合痕迹、风控缺口都在细节里）。
    """
    if not isinstance(cfg, dict):
        return {}
    try:
        from engine.report_data import _json_safe
        return _json_safe(cfg, max_list=64, max_depth=8) or {}
    except Exception:
        return cfg


def _today_str() -> str:
    """当前日期（注入审查报文，供 AI 校准自身知识时效）。

    ⛔ 只给日期、**不给行情**：策略是长期使用的，市场环境应由 AI 用自己的知识判断；
    工具只需告诉它「现在是几号」，让它在知识未覆盖时能主动声明，而不是拿过期认知
    自信地当"当前环境"陈述。
    """
    try:
        from server.core import time as srv_time
        return srv_time.today().isoformat()
    except Exception:
        import datetime
        return datetime.date.today().isoformat()


def scheme_conditions_prompt(text: str, scheme: dict = None) -> tuple:
    """自然语言 → 方案配置（量化策略师·翻译官）。（system, user）

    `scheme` = {"name": 方案名, "config": 当前方案 config}。
    ⛔ 必须投喂基线：否则用户说「更看重动量」「收紧止损」这类**相对**说法时，
    AI 只能凭空给绝对值（不知道当前是 0.8 还是 1.5），改出来是错的。
    """
    system = _build_system("scheme_config", _SC_BODY)
    sc = scheme or {}
    baseline = {
        "scheme_name": sc.get("name") or "（未命名）",
        "config": _trim_scheme_config(sc.get("config")),
    }
    user = (
        "用户的当前方案（作为调整基线；相对说法必须基于它计算目标值）：\n"
        f"{_dump(baseline)}\n\n"
        f"把下面这段话转成合法配置：\n“{text}”"
    )
    return system, user


# ---------------------------------------------------------------- ⑤b 量化策略师（审查官）
# ⛔ 与「生成」的区别：生成＝把用户想好的话翻译成配置；审查＝找出用户**没想到的**设计缺陷。
#    审查**不依赖回测数据**（用户明确"工具的回测可能不准"）——七类缺陷里绝大多数是
#    逻辑问题，凭领域知识即可判定；因此这里也**不投喂任何回测数字**（避免 AI 拿它当依据）。
_SR_BODY = (
    "## 一、方案概览（一句话）\n"
    "说清这个方案在赌什么：什么假设、什么风格、赚的是哪一类钱。不要复述字段。\n"

    "## 二、设计缺陷（本节是核心，逐类检查）\n"
    "按下面七类逐项检查。**命中的才写，没命中的不要写**；每条必须指名到具体字段或因子，"
    "格式为『问题 → 依据 → 后果』：\n"
    "1. **因子冗余**：所选因子是否在测同一件事（同为量价维度、同为波动率维度）"
    "⇒ 等于同一个假设重复押注，应下调其中至少一个的权重；\n"
    "2. **目标冲突**：是否存在互斥诉求（高弹性 vs 低回撤、高换手 vs 低交易摩擦）；\n"
    "3. **参数与风格错配**：止损/时间止损与该风格的实际波动不匹配"
    "（止损比日常波动还窄会被扫出局）；\n"
    "4. **过拟合痕迹**：参数取值过于精细或不对称（如周期 17 而非 14）、条件堆叠过多、"
    "启用维度远超样本可支撑；\n"
    "5. **风控缺口**：缺单笔上限 / 总仓位上限 / 时间止损 / 回撤约束；\n"
    "6. **与环境错配**：方案隐含的市场假设是否与**当前这段时间的市场环境**冲突"
    "（如均值回归逻辑遇上趋势市，最易连续挨打）。\n"
    "⛔ 这一条**靠你自己的知识判断**：输入只给当前日期 `today`，**不给逐步行情数据**。"
    "理由是策略**长期使用**，用『某一天』的涨跌家数去判断它合不合适是错的。\n"
    "你必须：① 说明你判断所依据的**时间范围与知识来源**"
    "（如「据我所知 2026 年上半年 A 股处于…」）；"
    "② 若你的知识**未覆盖到 `today`**，必须**明确声明不确定**并说明原因，"
    "严禁把过期认知当作当前环境自信陈述；\n"
    "7. **逻辑不一致**：筛选条件与因子权重指向相反的假设"
    "（如筛选用趋势类、权重给反转类）。\n"

    "## 三、修正建议\n"
    "针对上面**命中的**问题给具体改法：改哪个字段、往哪个方向改。"
    "给方向与量级即可，**不要编造精确数字**。\n"

    "## 四、这套方案没覆盖的风险\n"
    "设计层面未考虑的风险（**设计风险**，不是行情涨跌风险）。\n"

    "## 五、关于回测与夏普（仅当用户提到时才写）\n"
    "必须指出这类结果的常见偏差来源：前视偏差、幸存者偏差、样本期只覆盖单一市场环境、"
    "参数在样本内反复调优；并说明**没有样本外验证时不能作为依据**。\n"
    "⛔ 你没有拿到任何回测数据，**不得编造任何数值**。\n"

    "补充约束：\n"
    "- ⛔ **只审设计，不预测收益**。严禁说『这个方案能赚多少』『夏普大概能到 X』。\n"
    "- 你的依据是**通用量化常识**，不是这段行情/这只票的具体结论；"
    "凡涉及数值，必须说明是『通用经验值』而非实测结果。\n"
    "- ⛔ **没发现问题是合法且体面的结论**：直接写『未发现明显设计缺陷』即可。"
    "严禁为了有产出而编造问题。\n"
    "- 不要客套话，直接进入检查。\n"
)


def scheme_review_prompt(scheme: dict, question: str = None,
                         today: str = None) -> tuple:
    """当前方案 → 量化策略师（审查官）。（system, user）

    `scheme` = {"name": 方案名, "config": 当前方案 config}。
    `question`：用户可选的追问（如"回撤会不会太大"），无则做完整七类检查。
    `today`：当前日期。**只注入日期、不注入市场行情**——因为策略是长期使用的，
    用"某一天"的涨跌家数判断它合不合适是错的；第⑥类「与环境错配」要求 AI
    用**自身知识**判断这一段时间的市场环境，并声明知识依据与不确定性。
    """
    sc = scheme or {}
    ctx = {
        "scheme_name": sc.get("name") or "（未命名）",
        "config": _trim_scheme_config(sc.get("config")),
        "today": today or _today_str(),
    }
    ask = (question or "").strip()
    if ask:
        user = (
            f"这是用户当前的方案：\n{_dump(ctx)}\n\n"
            f"他额外问了一句：\n“{ask}”\n\n"
            "请按输出结构审查（先回答他的问题，再做完整七类检查）。"
        )
    else:
        user = (
            f"这是用户当前的方案：\n{_dump(ctx)}\n\n"
            "请按输出结构审查这套方案的设计。"
        )
    return _build_system("scheme_review", _SR_BODY), user


# ---------------------------------------------------------------- 工具
def _fmt_market(v):
    """标量字段兜底：None/空 → '—'。

    ⛔ 不要再对 dict/list 做 json.dumps+截断：本函数曾被用来包裹**整个** context，
    于是 `json.dumps(context)[:500]` 把全部投喂数据砍到 500 字符
    （2026-09-27 实测发现的隐藏 bug——AI「答非所问」的真因之一）。
    结构字段一律原样返回，交由 `_dump()` 整体序列化。
    """
    if v is None or v == "":
        return "—"
    if isinstance(v, (dict, list)):
        return v
    return str(v)


def _dump(ctx) -> str:
    """把投喂 context 序列化为**完整** JSON（不截断）。

    20000 字符上限只为防异常数据撑爆请求；正常 context 在 3~6K。
    `default=str` 兜住 numpy 等非原生类型（traceRaw 已由引擎 `_json_safe` 清洗过，
    这里只是二道保险，绝不能让序列化失败静默丢数据）。
    """
    import json
    try:
        s = json.dumps(ctx, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(ctx)
    return s[:20000]


def _fmt_position(p):
    """持仓字段精简：只留解盘用得上的四项，避免把嵌套 risk/clear/reduce 整坨塞给模型。

    无持仓返回 '—'（模型据此知道这是未持仓视角的解盘）。
    """
    p = p or {}
    if not p.get("price"):
        return "—"
    out = {
        "direction": p.get("direction") or "long",
        "cost": p.get("price"),
        "pnl": p.get("pnl"),
    }
    risk = p.get("risk")
    if isinstance(risk, dict) and risk:
        # 只取硬止损/时间止损两个阈值，其余（clear/reduce 明细）交给引擎不进 prompt
        sub = {}
        for k in ("hardStop", "time"):
            if risk.get(k):
                sub[k] = risk[k]
        if sub:
            out["risk"] = sub
    return out


# 兼容旧引用
_SYSTEM = _build_system("stock_analysis", _SA_BODY)
