# -*- coding: utf-8 -*-
"""风险偏好测评：十道题 → 四档风控参数。

设计原则（2026-09-27 用户拍板）
------------------------------
- **参数来自预设映射表，不由 AI 生成** ⇒ 从根上杜绝"AI 给出单票 90% 仓位"这类危险输出，
  不需要额外靠范围钳制兜底。
- 用户**答得出问题、答不出参数**：「单笔止损设几个百分点」没人答得上，
  「单笔亏多少你会睡不着」人人答得上。
- 映射是**纯函数**：同样答案永远得到同样档位，可回归、可复现、不依赖上游模型。

⛔ 两条自洽约束（本模块的核心，档位不能只改一个数）
--------------------------------------------------
1. `单票上限 × 最大持股数 ≤ 总仓位上限`——否则会推出"单票 20% × 8 只 = 160%"这种
   物理上不可能的组合。
2. `单笔最大亏损占总资金 = 单笔止损 × 单票上限`——**这个派生指标才是用户能理解的
   "档位含义"**（"连亏 5 次就 -10%"比"止损 12%"直观得多）。

⚠️ 取值为**通用经验值，未经本工具回测校准**；「稳健档」以用户现有方案 A 的实测值
（单票 10% / 止损 6% / 时间止损 8 日 / 总仓位 50%）为锚向外推。UI 与 AI 契约都必须
显式说明这一点，不得让用户误以为是本工具实测校准出的最优值。
"""

# ---------------------------------------------------------------- 四档参数
# 顺序即激进程度：conservative < balanced < active < aggressive
PROFILE_ORDER = ("conservative", "balanced", "active", "aggressive")

PROFILE_LABELS = {
    "conservative": "保守",
    "balanced": "稳健",
    "active": "积极",
    "aggressive": "激进",
}

# ⛔ 约束：max_single_position × suggested_max_holdings ≤ total_position_cap_pct
#   校验：5%×5=25%≤30% / 10%×5=50%=50% / 15%×5=75%=75% / 18%×5=90%=90%
PROFILES = {
    "conservative": {
        "label": "保守",
        "max_single_position": 0.05,
        "single_max_loss_pct": 0.04,
        "time_stop_days": 5,
        "time_stop_min_profit_pct": 0.06,
        "total_position_cap_pct": 0.30,
        "tech_resonance_threshold": 7,
        "market_crash_pct": 0.03,
        "suggested_max_holdings": 5,
        "desc": "先求不亏。单笔损失压到总资金的 0.2%，容错空间最大，但净值上升也最慢。",
    },
    "balanced": {
        "label": "稳健",
        "max_single_position": 0.10,
        "single_max_loss_pct": 0.06,
        "time_stop_days": 8,
        "time_stop_min_profit_pct": 0.05,
        "total_position_cap_pct": 0.50,
        "tech_resonance_threshold": 6,
        "market_crash_pct": 0.05,
        "suggested_max_holdings": 5,
        "desc": "攻守均衡。单笔损失占总资金 0.6%，是这个工具长期使用最常见的档位。",
    },
    "active": {
        "label": "积极",
        "max_single_position": 0.15,
        "single_max_loss_pct": 0.08,
        "time_stop_days": 12,
        "time_stop_min_profit_pct": 0.04,
        "total_position_cap_pct": 0.75,
        "tech_resonance_threshold": 5,
        "market_crash_pct": 0.06,
        "suggested_max_holdings": 5,
        "desc": "愿意承担波动换弹性。单笔损失占总资金 1.2%，需要能扛住连续回撤。",
    },
    "aggressive": {
        "label": "激进",
        "max_single_position": 0.18,
        "single_max_loss_pct": 0.12,
        "time_stop_days": 20,
        "time_stop_min_profit_pct": 0.03,
        "total_position_cap_pct": 0.90,
        "tech_resonance_threshold": 4,
        "market_crash_pct": 0.08,
        "suggested_max_holdings": 5,
        "desc": "单笔损失占总资金 2.16%——连亏 5 次账户就 -10%。只适合确认过自己扛得住的人。",
    },
}


# ---------------------------------------------------------------- 十道题
# score 0~3 对应 保守→激进；weight 用于加权（止损承受最关键，权重最高）
QUESTIONS = (
    {
        "id": "q1_stop_loss",
        "group": "A 承受力",
        "text": "买入后一只股票亏多少，你会睡不着？",
        "options": (
            {"label": "3% 以内就受不了", "score": 0},
            {"label": "5% 左右", "score": 1},
            {"label": "8% 左右", "score": 2},
            {"label": "10% 以上也能扛住", "score": 3},
        ),
        "weight": 2.0,
        "drives": "单笔止损阈值",
    },
    {
        "id": "q2_drawdown",
        "group": "A 承受力",
        "text": "整个账户从高点回撤多少，你会开始坐不住、想减仓？",
        "options": (
            {"label": "5% 就难受", "score": 0},
            {"label": "10% 左右", "score": 1},
            {"label": "20% 左右", "score": 2},
            {"label": "30% 以上才考虑", "score": 3},
        ),
        "weight": 1.5,
        "drives": "总仓位上限（并作为档位的硬上限，见交叉校验）",
    },
    {
        "id": "q3_patience",
        "group": "A 承受力",
        "text": "一只票买了之后多久不涨，你会失去耐心想换掉？",
        "options": (
            {"label": "3 天以内", "score": 0},
            {"label": "一周左右", "score": 1},
            {"label": "两周左右", "score": 2},
            {"label": "一个月以上", "score": 3},
        ),
        "weight": 1.0,
        "drives": "时间止损天数",
    },
    {
        "id": "q4_single_position",
        "group": "A 承受力",
        "text": "你通常一只票占总资金多少？",
        "options": (
            {"label": "5% 以内", "score": 0, "ref_position": 0.03},
            {"label": "10% 左右", "score": 1, "ref_position": 0.08},
            {"label": "15% 左右", "score": 2, "ref_position": 0.12},
            {"label": "20% 以上", "score": 3, "ref_position": 0.18},
        ),
        "weight": 1.0,
        "drives": "单票仓位上限",
    },
    {
        "id": "q5_holdings",
        "group": "A 承受力",
        "text": "你通常同时持有几只票？",
        "options": (
            # 语义：越集中越激进，越分散越保守（买很多只往往是不敢集中）
            {"label": "1-2 只", "score": 3, "ref_count": 2},
            {"label": "3-5 只", "score": 2, "ref_count": 4},
            {"label": "6-8 只", "score": 1, "ref_count": 7},
            {"label": "9 只以上", "score": 0, "ref_count": 10},
        ),
        "weight": 1.0,
        "drives": "分散度（与单票仓位共同决定实际敞口）",
    },
    {
        "id": "q6_fomo_vs_loss",
        "group": "A 承受力",
        "text": "下面哪种情况让你更难受？",
        "options": (
            {"label": "买入一只跌了 30% 的票", "score": 0},
            {"label": "错过一只涨了 50% 的票", "score": 3},
            {"label": "两种一样难受", "score": 1},
            {"label": "都还好，按计划就行", "score": 2},
        ),
        "weight": 1.5,
        "drives": "风险偏好主轴（怕亏 → 保守；怕错过 → 激进）",
    },
    {
        "id": "q7_on_drop",
        "group": "B 行为倾向",
        "text": "一只票跌了，但你认为基本面没变，你会？",
        "options": (
            {"label": "立刻止损离场", "score": 0},
            {"label": "观望不动", "score": 1},
            {"label": "视情况小量加仓", "score": 2},
            {"label": "补仓摊低成本", "score": 3},
        ),
        "weight": 1.0,
        "drives": "是否允许对亏损仓位加仓（禁用摊平）",
    },
    {
        "id": "q8_on_profit",
        "group": "B 行为倾向",
        "text": "一只票浮盈 20%，你的第一反应是？",
        "options": (
            {"label": "卖一半锁定利润", "score": 0},
            {"label": "设移动止损，让利润奔跑", "score": 1},
            {"label": "继续持有看情况", "score": 2},
            {"label": "加仓，趋势还在", "score": 3},
        ),
        "weight": 1.0,
        "drives": "减仓与止盈风格",
    },
    {
        "id": "q9_decision_source",
        "group": "B 行为倾向",
        "text": "你的买卖决定主要靠什么？",
        "options": (
            {"label": "严格按系统信号执行", "score": 0},
            {"label": "信号为主，自己微调", "score": 1},
            {"label": "自己判断为主", "score": 2},
            {"label": "凭盘感与直觉", "score": 3},
        ),
        "weight": 1.0,
        "drives": "技术共振阈值（要求多少项确认才建仓）",
    },
    {
        "id": "q10_persistence",
        "group": "C 执行能力",
        "text": "一套策略连续 5 次小亏、但长期是赚钱的，你能坚持用下去吗？",
        "options": (
            {"label": "能，看长期结果", "score": 3},
            {"label": "有点动摇但能坚持", "score": 2},
            {"label": "会想换一套试试", "score": 1},
            {"label": "立刻放弃", "score": 0},
        ),
        "weight": 1.0,
        "drives": "是否适合使用趋势类方案（能否扛过回撤期）",
    },
)

_QUESTION_INDEX = {q["id"]: q for q in QUESTIONS}


# ---------------------------------------------------------------- 纯函数映射
def compute_derived(profile_key: str) -> dict:
    """由档位参数推导"用户能理解的档位含义"。

    `single_trade_loss_pct_of_equity`：单笔最大亏损占总资金的百分比。
    `consecutive_losses_to_minus_10pct`：连亏几次账户会 -10%（不足以 -10% 时为 None）。
    """
    p = PROFILES.get(profile_key) or {}
    pos = float(p.get("max_single_position") or 0)
    stop = float(p.get("single_max_loss_pct") or 0)
    per_trade = pos * stop
    out = {
        "single_trade_loss_pct_of_equity": round(per_trade * 100, 2),
        "consecutive_losses_to_minus_10pct": None,
    }
    if per_trade > 0:
        n = int(round(0.10 / per_trade))
        # 只在"确实可能连亏到 -10%"时给出，避免给出 50 次这种无意义的数字
        if n <= 30:
            out["consecutive_losses_to_minus_10pct"] = n
    return out


def map_answers(answers: dict) -> dict:
    """答案 → 档位 + 参数 + 派生指标 + 警告。**纯函数**，无副作用。

    answers：`{question_id: option_index}`（option_index 从 0 开始）。
    返回：
      ok=True  → {ok, profile, label, params, derived, score, warnings}
      ok=False → {ok, error, missing}

    ⛔ 交叉校验（取更保守）：Q2（回撤容忍）单独定一个**硬上限档位**，
    若加权计分得到的档位比它更激进，则**降到 Q2 的档位**。
    理由：人在低估风险时的自我评估最不可靠，回撤容忍度是更硬的约束。
    """
    missing = []
    total = 0.0
    max_total = 0.0
    for q in QUESTIONS:
        w = float(q.get("weight", 1.0))
        max_total += 3.0 * w
        idx = answers.get(q["id"]) if isinstance(answers, dict) else None
        opts = q["options"]
        if not isinstance(idx, int) or isinstance(idx, bool) or not (0 <= idx < len(opts)):
            missing.append(q["id"])
            continue
        total += float(opts[idx]["score"]) * w
    if missing:
        return {
            "ok": False,
            "error": "有 %d 道题未作答或答案非法，无法评定档位" % len(missing),
            "missing": missing,
        }

    ratio = (total / max_total) if max_total else 0.0
    pos = min(int(ratio * len(PROFILE_ORDER)), len(PROFILE_ORDER) - 1)
    score_profile = PROFILE_ORDER[pos]

    warnings = []
    # 交叉校验：Q2 的回撤容忍度是硬上限
    q2 = _QUESTION_INDEX["q2_drawdown"]
    q2_score = q2["options"][answers["q2_drawdown"]]["score"]
    cap_profile = PROFILE_ORDER[min(int(q2_score), len(PROFILE_ORDER) - 1)]
    if PROFILE_ORDER.index(score_profile) > PROFILE_ORDER.index(cap_profile):
        warnings.append(
            "你的回撤容忍度只到「%s」，但其他回答偏「%s」——"
            "已按更保守的「%s」定档。理由：低估风险时的自我评估最不可靠。"
            % (PROFILE_LABELS[cap_profile], PROFILE_LABELS[score_profile],
               PROFILE_LABELS[cap_profile])
        )
        final_profile = cap_profile
    else:
        final_profile = score_profile

    # 实际敞口提示：Q4 单票习惯 × Q5 持股数
    q4_opt = _QUESTION_INDEX["q4_single_position"]["options"][answers["q4_single_position"]]
    q5_opt = _QUESTION_INDEX["q5_holdings"]["options"][answers["q5_holdings"]]
    ref_exposure = float(q4_opt.get("ref_position", 0)) * float(q5_opt.get("ref_count", 0))
    cap = float(PROFILES[final_profile]["total_position_cap_pct"])
    if ref_exposure > cap * 1.2:
        warnings.append(
            "按你自述的持股习惯（单票约 %.0f%% × %.0f 只 ≈ %.0f%% 敞口），"
            "实际用满会超过「%s」档的总仓位上限 %.0f%%——"
            "建议要么减少持股数，要么降低单票比例。"
            % (float(q4_opt.get("ref_position", 0)) * 100, float(q5_opt.get("ref_count", 0)),
               ref_exposure * 100, PROFILE_LABELS[final_profile], cap * 100)
        )

    params = dict(PROFILES[final_profile])
    return {
        "ok": True,
        "profile": final_profile,
        "label": params.pop("label"),
        "desc": params.pop("desc"),
        "params": params,
        "derived": compute_derived(final_profile),
        "score": round(total, 2),
        "score_max": round(max_total, 2),
        "score_profile": score_profile,
        "warnings": warnings,
        "disclaimer": "档位为通用经验值，未经本工具回测校准，请结合自身情况判断。",
    }


def public_questions() -> list:
    """下发给前端的题目（不含分值，避免用户按"正确答案"作答）。"""
    out = []
    for q in QUESTIONS:
        out.append({
            "id": q["id"],
            "group": q["group"],
            "text": q["text"],
            "drives": q["drives"],
            "options": [{"label": o["label"]} for o in q["options"]],
        })
    return out


def build_config_patch(profile_key: str, config: dict = None) -> dict:
    """把档位参数合并进方案 config 的 `entry_params` / `risk_params`（只改这两块）。

    ⛔ 最小侵入：不改因子、不改筛选条件、不改幽灵规则，其余字段原样保留。
    """
    p = PROFILES.get(profile_key)
    if not p:
        return {}
    cfg = dict(config or {})
    risk = dict(cfg.get("risk_params") or {})
    risk.update({
        "max_single_position": p["max_single_position"],
        "single_max_loss_pct": p["single_max_loss_pct"],
        "time_stop_days": p["time_stop_days"],
        "time_stop_min_profit_pct": p["time_stop_min_profit_pct"],
        "total_position_cap_pct": p["total_position_cap_pct"],
        "market_crash_pct": p["market_crash_pct"],
    })
    entry = dict(cfg.get("entry_params") or {})
    positions = dict(entry.get("positions") or {})
    # 建仓档位按比例缩放：以现状"强势档"为基准，其余按档位倾向同比缩小
    side = p["max_single_position"]
    positions.update({
        "strong": round(side, 4),
        "standard": round(side * 0.7, 4),
        "test": round(side * 0.4, 4),
        "top_reversal": round(side * 0.4, 4),
    })
    entry["positions"] = positions
    res = dict(cfg.get("tech_resonance") or {})
    res["threshold"] = p["tech_resonance_threshold"]
    cfg["risk_params"] = risk
    cfg["entry_params"] = entry
    cfg["tech_resonance"] = res
    return cfg
