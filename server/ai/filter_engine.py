# -*- coding: utf-8 -*-
"""扫描结果筛选引擎（DSL 执行器）。

职责边界：**AI 只产出 DSL，执行在后端**。AI 不得直接编股票列表——它看不到全量数据，
逐只推荐必然是幻觉；让它输出条件、由后端在真实结果集上执行，结果才可信。

字段白名单来自 prompts.DSL_FIELDS：扫描结果默认不含 rsi / volume_ratio 等原始指标列，
照抄这类条件会被执行器拒绝并回传 rejected，绝不静默丢弃。
"""

from server.ai import prompts

# 数值型字段（其余视为字符串型）
_NUMERIC_FIELDS = {'price', 'final_score', 'stars'}


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_dsl(dsl):
    """校验并归一化 DSL。

    返回 (normalized: dict|None, rejected: list[str])。
    normalized 为 None 表示整个 DSL 不可用。
    """
    rejected = []
    if not isinstance(dsl, dict):
        return None, ["DSL 必须是对象"]

    action = str(dsl.get("action") or "filter")
    if action != "filter":
        rejected.append(f"不支持的 action：{action}（当前只支持 filter）")

    conds = []
    raw_conds = dsl.get("conditions")
    if raw_conds in (None, []):
        pass
    elif not isinstance(raw_conds, list):
        rejected.append("conditions 必须是数组")
    else:
        for i, c in enumerate(raw_conds[:20]):
            if not isinstance(c, dict):
                rejected.append(f"conditions[{i}] 不是对象")
                continue
            field = c.get("field")
            op = c.get("op")
            value = c.get("value")
            if field not in prompts.DSL_FIELDS:
                rejected.append(
                    f"conditions[{i}] 字段 '{field}' 不在白名单"
                    f"（可用：{', '.join(prompts.DSL_FIELDS)}）"
                )
                continue
            if op not in prompts.DSL_OPS:
                rejected.append(f"conditions[{i}] 运算符 '{op}' 不合法")
                continue
            if op in ("in", "not_in") and not isinstance(value, list):
                rejected.append(f"conditions[{i}] op={op} 时 value 必须是数组")
                continue
            if op not in ("in", "not_in"):
                if field in _NUMERIC_FIELDS:
                    if not _is_num(value):
                        rejected.append(f"conditions[{i}] 数值字段 '{field}' 需要数字 value")
                        continue
                elif not isinstance(value, str):
                    rejected.append(f"conditions[{i}] 文本字段 '{field}' 需要字符串 value")
                    continue
            conds.append({"field": field, "op": op, "value": value})

    sorts = []
    raw_sort = dsl.get("sort")
    if isinstance(raw_sort, list):
        for i, s in enumerate(raw_sort[:5]):
            if not isinstance(s, dict):
                rejected.append(f"sort[{i}] 不是对象")
                continue
            f = s.get("field")
            order = str(s.get("order") or "desc").lower()
            if f not in prompts.DSL_FIELDS:
                rejected.append(f"sort[{i}] 字段 '{f}' 不在白名单")
                continue
            if order not in prompts.DSL_SORT_ORDERS:
                rejected.append(f"sort[{i}] order '{order}' 不合法，已按 desc 处理")
                order = "desc"
            sorts.append({"field": f, "order": order})

    try:
        limit = int(dsl.get("limit") or 50)
    except (TypeError, ValueError):
        rejected.append("limit 必须是整数，已按 50 处理")
        limit = 50
    if limit < 1:
        rejected.append("limit 必须 ≥1，已按 1 处理")
        limit = 1
    if limit > prompts.DSL_MAX_LIMIT:
        rejected.append(f"limit 超过上限 {prompts.DSL_MAX_LIMIT}，已截断")
        limit = prompts.DSL_MAX_LIMIT

    return {"action": "filter", "conditions": conds, "sort": sorts, "limit": limit}, rejected


def _match(row, cond):
    """单条件匹配。字段缺失一律视为不满足（不猜、不填默认值）。"""
    field, op, value = cond["field"], cond["op"], cond["value"]
    if field not in row:
        return False
    actual = row.get(field)
    if op in ("in", "not_in"):
        hit = actual in value
        return hit if op == "in" else not hit
    if field in _NUMERIC_FIELDS:
        if not _is_num(actual):
            return False
        if op == ">":
            return actual > value
        if op == ">=":
            return actual >= value
        if op == "<":
            return actual < value
        if op == "<=":
            return actual <= value
        if op == "==":
            return actual == value
        if op == "!=":
            return actual != value
        return False
    if not isinstance(actual, str):
        actual = str(actual)
    if op == "==":
        return actual == value
    if op == "!=":
        return actual != value
    if op == ">":
        return actual > value
    if op == ">=":
        return actual >= value
    if op == "<":
        return actual < value
    if op == "<=":
        return actual <= value
    return False


def apply_dsl(items, dsl):
    """在结果集上执行 DSL。返回 {rows, total_in, total_out, applied, rejected}。"""
    items = items or []
    normalized, rejected = validate_dsl(dsl)
    if normalized is None:
        return {
            "rows": [], "total_in": len(items), "total_out": 0,
            "applied": {}, "rejected": rejected,
        }

    rows = list(items)
    for cond in normalized["conditions"]:
        rows = [r for r in rows if _match(r, cond)]

    for s in reversed(normalized["sort"]):
        rows.sort(
            key=lambda r: (r.get(s["field"]) is None, r.get(s["field"])),
            reverse=(s["order"] == "desc"),
        )

    total_out_all = len(rows)
    rows = rows[:normalized["limit"]]
    return {
        "rows": rows,
        "total_in": len(items),
        "total_out": len(rows),
        "matched_before_limit": total_out_all,
        "applied": normalized,
        "rejected": rejected,
    }
