# -*- coding: utf-8 -*-
"""AI 解读层 · 纯函数服务（供 FastAPI 端点与桌面 WebAPI 桥接共用）。

设计：所有函数只接收"已算好的结构化结果/字符串"，自身不触发任何引擎分析，
据此复用 DeepSeek 产出解读。FastAPI 的 server/api/ai.py 与桌面 ui/web_api.py
都只做"拿到引擎产物 → 调本层"的薄封装。

执行框架（TaskMode 驱动）：
  1. 每个场景绑定一个 mode（server.ai.modes.TASK_MODES），temperature / 禁止事项 /
     降级模板全部从 mode 读，不在本层散落常量；
  2. 上游超时/限流/解析失败 → 返回该模式的降级模板（degraded=True），不抛错给用户；
  3. 输出命中硬禁止词（绝对化承诺）→ 同样降级，不把违规内容推给用户。

错误约定：未配置 key 等"用户侧问题"仍抛 ApiError，由调用方按其运行形态收敛。
"""

import json

from server.ai import client, prompts, modes
from server.core.errors import ApiError

# 硬禁止词：可机械匹配、命中即降级。语义类禁止项（如"说教""直接荐股"）无法可靠匹配，
# 只进 system prompt 作软约束，不做硬拦截（避免误伤 AI 引用禁止词本身）。
_HARD_FORBIDDEN = ("必涨", "必跌", "稳赚", "包赚", "保证收益", "满仓干", "无风险")

# 这些上游错误走降级模板；其余（如未配置 key）继续抛错
_DEGRADE_CODES = {"AI_TIMEOUT", "AI_RATE_LIMIT", "AI_UPSTREAM_ERROR", "AI_PARSE_FAILED"}


# ---------------------------------------------------------------- 公共
def _require_configured() -> None:
    """未配置 key 时统一抛出。"""
    if not client.available():
        raise ApiError(
            "AI_NOT_CONFIGURED",
            "尚未配置 DeepSeek API Key，请到「模型配置」页填写后再试",
            status_code=400,
        )


def _call(mode_key: str, system: str, prompt: str, scene: str = "") -> tuple:
    """调用模型并做降级收口。返回 (text, degraded, reason)。

    scene: 写进日志的场景名。⛔ 必须传 —— mode_key 会被多个入口共用
    （如 stock_analysis 同时服务 ai_analyze / ai_diagnosis / ai_diagnosis_batch），
    不传就只能记到 mode_key 粒度，事后分不清是哪个按钮出的问题。
    """
    try:
        text = client.chat(prompt, system=system, temperature=modes.temperature_of(mode_key),
                           scene=scene or mode_key)
    except ApiError as exc:
        if exc.code in _DEGRADE_CODES:
            return modes.fallback_of(mode_key), True, f"{exc.code}: {exc.message}"
        raise
    if not text:
        return modes.fallback_of(mode_key), True, "AI_EMPTY_RESPONSE"
    hits = [w for w in _HARD_FORBIDDEN if w in text]
    if hits:
        return modes.fallback_of(mode_key), True, "输出命中绝对化承诺词：" + "、".join(hits)
    return text, False, ""


def _result(mode_key: str, scene: str, text: str, degraded: bool, reason: str, **extra) -> dict:
    out = {
        "ok": True,
        "text": text,
        "scene": scene,
        "mode": mode_key,
        "prompt_version": modes.get_mode(mode_key).get("version", ""),
        "degraded": degraded,
    }
    if reason:
        out["degrade_reason"] = reason
    out.update(extra)
    return out


# ---------------------------------------------------------------- ① 个股解盘师
def ai_analyze_result(report_data: dict, report_text: str = "", scheme_summary=None) -> dict:
    """个股分析报告 → 趋势定性 + 指标拆解 + 情景预案 + 证伪条件。"""
    _require_configured()
    system, prompt = prompts.analyze_prompt(
        report_data or {}, report_text or "", scheme_summary
    )
    text, degraded, reason = _call("stock_analysis", system, prompt, scene="ai_analyze")
    return _result("stock_analysis", "analyze", text, degraded, reason)


# ---------------------------------------------------------------- ② 华尔街幽灵（单只）
def ai_diagnosis_result(item: dict) -> dict:
    """单只诊断结果 → 个股解盘师（完整六段）。

    2026-09-27 变更：原为「华尔街幽灵」分诊，因幽灵三规则已下沉到方案配置
    （engine/ghost_engine.py + scheme 的 config.ghost_rules），无需 AI 复述，
    统一改用个股解盘师，与个股分析入口同源。
    """
    _require_configured()
    if not item:
        raise ApiError("INVALID_ITEM", "缺少诊断结果 item", status_code=422)
    system, prompt = prompts.diagnosis_prompt(item)
    text, degraded, reason = _call("stock_analysis", system, prompt, scene="ai_diagnosis")
    return _result("stock_analysis", "diagnosis", text, degraded, reason)


# ---------------------------------------------------------------- ② 个股解盘师（自选批量）
def ai_diagnosis_batch_result(items: list) -> dict:
    """批量诊断（自选/持仓列表）→ 个股解盘师（逐只速览 + 重点个股完整六段）。"""
    _require_configured()
    items = items or []
    if not items:
        raise ApiError("EMPTY_ITEMS", "没有可解读的诊断结果", status_code=422)
    system, prompt = prompts.diagnosis_batch_prompt(items)
    text, degraded, reason = _call("stock_analysis", system, prompt, scene="ai_diagnosis_batch")
    return _result(
        "stock_analysis", "diagnosis_batch", text, degraded, reason,
        covered=min(len(items), 30),
    )


# ---------------------------------------------------------------- ③ 候选池审计师（扫描概览）
def ai_scan_result(items: list, filters: dict = None, market_context: dict = None) -> dict:
    """扫描结果集 → 分类统计呈现 + 结合市场整体情况的可信度审计。

    分类统计由 `engine.report_data.scan_stats` 在引擎侧算成确定数字（⛔ 不让 LLM 数数），
    market_context（涨跌家数/指数/环境）由调用方（桌面端 `_ai_market_context`）自取。
    """
    _require_configured()
    items = items or []
    if not items:
        raise ApiError("EMPTY_ITEMS", "没有可解读的扫描结果", status_code=422)
    system, prompt = prompts.scan_prompt(items, filters, market_context)
    text, degraded, reason = _call("scan_overview", system, prompt, scene="ai_scan")
    return _result(
        "scan_overview", "scan", text, degraded, reason,
        covered=min(len(items), 30),
    )


# ---------------------------------------------------------------- ③b 数据分析师：对话式筛选
def ai_scan_dsl(text: str, items: list = None) -> dict:
    """自然语言 → 筛选 DSL（AI 只出条件，不编列表）。"""
    _require_configured()
    text = (text or "").strip()
    if not text:
        raise ApiError("EMPTY_TEXT", "请输入筛选要求", status_code=422)
    system, prompt = prompts.scan_dsl_prompt(text, items)
    try:
        raw = client.chat(prompt, system=system, temperature=modes.temperature_of("scan_overview"),
                          scene="ai_scan_dsl")
    except ApiError as exc:
        if exc.code in _DEGRADE_CODES:
            return _result(
                "scan_overview", "scan_dsl", modes.fallback_of("scan_overview"), True,
                f"{exc.code}: {exc.message}", dsl=None, rejected=[],
            )
        raise
    parsed = _parse_json_relaxed(raw)
    if parsed is None:
        return _result(
            "scan_overview", "scan_dsl", modes.fallback_of("scan_overview"), True,
            "AI_PARSE_FAILED", dsl=None, rejected=[],
        )
    from server.ai import filter_engine
    normalized, rejected = filter_engine.validate_dsl(parsed)
    return _result(
        "scan_overview", "scan_dsl", raw, False, "",
        dsl=normalized, rejected=rejected,
    )


def ai_scan_filter(items: list, dsl: dict) -> dict:
    """在结果集上执行筛选 DSL（后端执行，AI 不参与）。"""
    items = items or []
    if not items:
        raise ApiError("EMPTY_ITEMS", "没有可筛选的结果集", status_code=422)
    from server.ai import filter_engine
    res = filter_engine.apply_dsl(items, dsl)
    return {"ok": True, "scene": "scan_filter", **res}


# ---------------------------------------------------------------- ④ 劝导员（情绪复盘）
def ai_ledger_result(signals: list, summary=None, cooldown: dict = None,
                     emotion: dict = None) -> dict:
    """纪律账本 + 情绪账本 → 行为偏差归因 + 代价 + 可验证纪律规则。

    `emotion` = 情绪账本（用户自己记的「追涨/摊平/恐慌」等标签 + 操作价 + 现状 + 累计）。
    ⛔ 必须投喂：它是**用户自己做的行为归因**，比 AI 从信号推断更准，
    也让输出能用上用户自己的话，而不是单向说教。
    """
    _require_configured()
    system, prompt = prompts.ledger_prompt(signals or [], summary or {}, cooldown, emotion)
    text, degraded, reason = _call("ledger_review", system, prompt, scene="ai_ledger")
    emo_n = len((emotion or {}).get("records") or [])
    return _result(
        "ledger_review", "ledger", text, degraded, reason,
        covered=len(signals or []),
        emotion_covered=emo_n,
    )


# ---------------------------------------------------------------- ⑤ 量化策略师（方案配置）
def _clean_filters(raw) -> list:
    """清洗 AI 产出的 filters：只保留白名单内的合法条目。"""
    cleaned = []
    if not isinstance(raw, list):
        return cleaned
    for c in raw:
        if not isinstance(c, dict):
            continue
        if "signal" in c:
            if c["signal"] in prompts.SIGNAL_OPTIONS:
                cleaned.append({"signal": c["signal"]})
        elif "indicator" in c and "op" in c and "value" in c:
            ind = c["indicator"]
            op = c.get("op")
            try:
                value = float(c["value"])
            except (TypeError, ValueError):
                continue
            if ind in prompts.AND_INDICATORS and op in prompts.AND_OPS:
                cleaned.append({"indicator": ind, "op": op, "value": value})
    return cleaned


def _clean_factor_tweaks(raw) -> dict:
    """清洗 factor_tweaks：只保留白名单内的因子、值为 0~2 的幅度。"""
    tweaks = {}
    if not isinstance(raw, dict):
        return tweaks
    for name, val in raw.items():
        if name in prompts.FACTOR_NAMES:
            try:
                v = float(val)
            except (TypeError, ValueError):
                continue
            if 0.0 <= v <= 2.0:
                tweaks[name] = round(v, 3)
    return tweaks


def _clean_str_map(raw) -> dict:
    """清洗字符串型说明块（explain / risk）：只保留短字符串，控长度。"""
    out = {}
    if not isinstance(raw, dict):
        return out
    for k, v in list(raw.items())[:12]:
        if isinstance(v, str) and v.strip():
            out[str(k)] = v.strip()[:300]
        elif v is None:
            out[str(k)] = ""
    return out


def _clean_str_list(raw, limit: int = 10) -> list:
    if not isinstance(raw, list):
        return []
    return [str(x)[:200] for x in raw if str(x).strip()][:limit]


def _parse_json_relaxed(raw: str):
    """去除 markdown 围栏并取首个平衡 JSON 对象。"""
    parsed = None
    clean = (raw or "").strip()
    if clean.startswith("```"):
        clean = clean.strip("`")
        if clean.lower().startswith("json"):
            clean = clean[4:]
    try:
        parsed = json.loads(clean)
    except json.JSONDecodeError:
        try:
            start = clean.index("{")
            end = clean.rindex("}")
            parsed = json.loads(clean[start:end + 1])
        except (ValueError, json.JSONDecodeError):
            parsed = None
    return parsed if isinstance(parsed, dict) else None


def ai_scheme_review_result(scheme: dict, question: str = None,
                            today: str = None) -> dict:
    """当前方案 → 设计审查（缺陷清单 + 修正建议）。

    ⛔ 不依赖回测：审查的是**设计自洽性**（因子冗余/目标冲突/参数错配/过拟合/风控缺口/
    环境错配/逻辑不一致），全是逻辑问题，凭领域知识即可判定，故不投喂任何回测数字。
    ⛔ 也**不投喂市场行情**（`today` 只传日期）：策略是长期使用的，第⑥类「与环境错配」
    由 AI 用自身知识判断这段时间的市场环境，并须声明知识依据与不确定性——
    拿"某一天"的涨跌家数去判断长期策略是错的。
    """
    _require_configured()
    cfg = (scheme or {}).get("config")
    if not isinstance(cfg, dict) or not cfg:
        raise ApiError("EMPTY_SCHEME", "没有可审查的方案，请先在「模型配置」里保存一个方案",
                       status_code=422)
    system, prompt = prompts.scheme_review_prompt(scheme, question, today)
    text, degraded, reason = _call("scheme_review", system, prompt, scene="scheme_review")
    return _result(
        "scheme_review", "scheme_review", text, degraded, reason,
        scheme_name=(scheme or {}).get("name"),
    )


def ai_scheme_conditions_text(text: str, scheme: dict = None) -> dict:
    """自然语言 → 方案配置（含字段解释、校验结果、风险约束、夏普四层分离）。

    `scheme` = 当前方案（作为调整基线）；相对说法（"更看重X"/"收紧Y"）必须基于它计算。
    """
    _require_configured()
    text = (text or "").strip()
    if not text:
        raise ApiError("EMPTY_TEXT", "请输入要配置的自然语言描述", status_code=422)

    system, prompt = prompts.scheme_conditions_prompt(text, scheme)
    try:
        raw = client.chat(prompt, system=system, temperature=modes.temperature_of("scheme_config"),
                          scene="scheme_config")
    except ApiError as exc:
        if exc.code in _DEGRADE_CODES:
            return _result(
                "scheme_config", "scheme", modes.fallback_of("scheme_config"), True,
                f"{exc.code}: {exc.message}",
                filters=[], factor_tweaks={}, unsupported=[],
            )
        raise

    parsed = _parse_json_relaxed(raw)
    if parsed is None:
        return _result(
            "scheme_config", "scheme", modes.fallback_of("scheme_config"), True,
            "AI_PARSE_FAILED", filters=[], factor_tweaks={}, unsupported=[],
        )

    # 兼容两种形态：新版 config 嵌套 / 旧版顶层直给
    cfg = parsed.get("config") if isinstance(parsed.get("config"), dict) else parsed
    filters = _clean_filters(cfg.get("filters"))
    tweaks = _clean_factor_tweaks(cfg.get("factor_tweaks"))
    validation = parsed.get("validation") if isinstance(parsed.get("validation"), dict) else {}
    sharpe_raw = parsed.get("sharpe") if isinstance(parsed.get("sharpe"), dict) else {}

    # ⛔ 硬收口：本层没有回测数据，backtest 一律置 null（AI 声称值另存 ai_claimed 备查）
    sharpe = {
        "user_target": sharpe_raw.get("user_target") if isinstance(
            sharpe_raw.get("user_target"), (int, float)) else 2.0,
        "constraint": str(sharpe_raw.get("constraint") or "")[:300],
        "backtest": None,
        "oos_expectation": str(sharpe_raw.get("oos_expectation") or "")[:300],
    }
    if sharpe_raw.get("backtest") not in (None, "", "null"):
        sharpe["ai_claimed_backtest"] = sharpe_raw.get("backtest")

    validation_out = {
        "applied": _clean_str_list(validation.get("applied")),
        "conflicts": _clean_str_list(validation.get("conflicts")),
        "unsupported": _clean_str_list(
            validation.get("unsupported") or parsed.get("unsupported")
        ),
    }
    return _result(
        "scheme_config", "scheme", raw, False, "",
        filters=filters,
        factor_tweaks=tweaks,
        explain=_clean_str_map(parsed.get("explain")),
        risk=_clean_str_map(parsed.get("risk")),
        validation=validation_out,
        # ⚠️ 顶层 unsupported 是**既有契约，不可删**：桌面端 ui_mockup/index.html
        #    （:5371「暂不支持」提示、:5382 toast）与 H5 web/src/api/ai.js 都直读
        #    `r.unsupported`。TaskMode 改版把它并进 validation 后必须同步保留顶层，
        #    否则 UI 静默显示「（无）」，用户以为 AI 什么都能配。
        unsupported=validation_out["unsupported"],
        sharpe=sharpe,
    )
