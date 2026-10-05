# -*- coding: utf-8 -*-
"""方案管理适配层（F-601）—— 纯函数，不碰 DB / HTTP / engine。

口径来源（逐字对齐，勿自创）：
- **命名校验**：桌面 `ui/web_api.py:3875-3885`（非空 / `isprintable` / 禁 `-` / ≤50）。
- **导入 3 格式**：桌面 `ui/web_api.py:4033-4230`
  ① `{"schemes": {...}}` 策略库（同名覆盖，非法名进 `skipped`）；
  ② 单方案完整对象 `{label, desc, _meta, factor_profile, config}`（`export_scheme(mode='current')` 导出的对称格式）；
  ③ 单方案 flat config `{active_factors, factor_configs, ...}`（防呆：两键必填，按传入分类归属）。
- **导出格式**：`ui/web_api.py:3953-3996` 的单方案对称对象。

H5 差异（§3.4 硬约束 4「面向 H5 设计，非 1:1 平移」）：
- 导入的因子 profile **只做「不存在则建」，不静默覆盖已存在的共享 profile**——避免改一个导入
  而波及其他引用同一 profile 的方案（桌面版会写回 profile，H5 收敛为更安全的语义）。
"""

from __future__ import annotations

import json
from typing import Callable, Optional
from urllib.parse import quote

MAX_NAME_LEN = 50
_FACTOR_KEYS = ("active_factors", "factor_configs", "score_scale")


def validate_scheme_name(name) -> Optional[str]:
    """返回错误说明；None 表示合法。对齐桌面 `_validate_scheme_name`。"""
    if not name or not str(name).strip():
        return "请输入方案名称"
    text = str(name)
    if not text.isprintable():
        return "方案名称不能包含控制字符"
    if "-" in text:
        return "方案名称不能包含 '-' 字符"
    if len(text) > MAX_NAME_LEN:
        return "方案名称不能超过50个字符"
    return None


def parse_json_text(text) -> dict:
    """解析导入 JSON；非对象抛 ValueError。"""
    try:
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001 —— 统一转成业务可读错误
        raise ValueError(f"配置格式错误：JSON 解析失败（{exc}）") from exc
    if not isinstance(data, dict):
        raise ValueError("配置格式错误，应为 JSON 对象")
    return data


def _fallback_meta(market, direction, period) -> dict:
    mk_market = market or "stock"
    return {
        "market": mk_market,
        "direction": (direction or "long") if mk_market == "futures" else "long",
        "period": period or "日K",
    }


def _profile_body_from_config(config: dict) -> Optional[dict]:
    """从内联 config 抽因子段（引用 profile 的方案才有意义）。"""
    if not isinstance(config, dict):
        return None
    body = {k: config[k] for k in _FACTOR_KEYS if k in config}
    return body or None


def _auto_name(meta: dict, name_exists: Callable[[str], bool]) -> str:
    """自动生成唯一名（对齐桌面 `导入_{market}_{direction}_{period}`[:40] + `_i` 去重）。"""
    base = f"导入_{meta.get('market')}_{meta.get('direction')}_{meta.get('period')}"
    base = base.replace("/", "_").replace("\\", "_")[:40]
    if not name_exists(base):
        return base
    for i in range(1, 1000):
        candidate = f"{base}_{i}"
        if not name_exists(candidate):
            return candidate
    return base


def plan_import(
    raw: dict,
    *,
    market: Optional[str] = None,
    direction: Optional[str] = None,
    period: Optional[str] = None,
    scheme_name: Optional[str] = None,
    name_exists: Callable[[str], bool] = lambda _n: False,
) -> dict:
    """把 3 种导入输入归一为可落库的计划。

    返回 `{mode, entries[], skipped[], profiles{}, duplicate}`：
    - `entries[i]` = `{name, label, desc, meta, factor_profile, config, profile_body, overwrite}`
    - `profiles` = `{profile_name: body}`（顶层 `factor_profiles` + 单方案内联因子段）
    - `duplicate` = 显式命名且已存在时的方案名（调用方映射 409）；None 表示无冲突
    """
    if not isinstance(raw, dict):
        raise ValueError("配置格式错误，应为 JSON 对象")

    profiles: dict = {}

    # ── ① 多方案策略库 ──
    if isinstance(raw.get("schemes"), dict):
        for pname, body in (raw.get("factor_profiles") or {}).items():
            if isinstance(body, dict):
                profiles[pname] = body
        entries, skipped = [], []
        for sname, sdata in raw["schemes"].items():
            err = validate_scheme_name(sname)
            if err:
                skipped.append({"name": sname, "reason": err})
                continue
            if isinstance(sdata, dict):
                sconfig = sdata.get("config", sdata) if "config" in sdata else sdata
                smeta = sdata.get("_meta")
                sdesc = sdata.get("desc") or ""
                sfp = sdata.get("factor_profile")
                slabel = sdata.get("label")
            else:
                sconfig, smeta, sdesc, sfp, slabel = (sdata or {}), None, "", None, None
            if not (isinstance(smeta, dict) and smeta.get("market") in ("stock", "futures")):
                smeta = _fallback_meta(market, direction, period)
            entries.append({
                "name": sname,
                "label": slabel,
                "desc": sdesc,
                "meta": smeta,
                "factor_profile": sfp,
                "config": sconfig if isinstance(sconfig, dict) else {},
                "profile_body": _profile_body_from_config(sconfig) if sfp else None,
                "overwrite": True,
            })
        return {"mode": "multi", "entries": entries, "skipped": skipped,
                "profiles": profiles, "duplicate": None}

    # ── ②/③ 单方案 ──
    if "active_factors" in raw and "factor_configs" in raw:
        s_config, s_meta, s_desc, s_fp, s_label = raw, None, "", None, None
    else:
        # 完整对象：至少含 config 或 _meta 之一，否则判定格式非法
        if not ("config" in raw or "_meta" in raw):
            raise ValueError(
                "配置格式错误：单方案格式必须包含 active_factors 与 factor_configs 字段。"
                "若导入含多个方案的方案库，请用 {\"schemes\": {...}} 多方案格式。"
            )
        s_config = raw.get("config") if isinstance(raw.get("config"), dict) else {}
        s_meta = raw.get("_meta")
        s_desc = raw.get("desc") or ""
        s_fp = raw.get("factor_profile")
        s_label = raw.get("label")

    if not (isinstance(s_meta, dict) and s_meta.get("market") in ("stock", "futures")):
        s_meta = _fallback_meta(market, direction, period)

    body = _profile_body_from_config(s_config) if s_fp else None
    if s_fp and body:
        profiles[s_fp] = body

    duplicate = None
    if scheme_name:
        if validate_scheme_name(scheme_name):
            raise ValueError(validate_scheme_name(scheme_name))
        if name_exists(scheme_name):
            duplicate = scheme_name
        target_name = scheme_name
    else:
        target_name = _auto_name(s_meta, name_exists)

    entry = {
        "name": target_name,
        "label": s_label,
        "desc": s_desc,
        "meta": s_meta,
        "factor_profile": s_fp,
        "config": s_config if isinstance(s_config, dict) else {},
        "profile_body": body,
        "overwrite": False,
    }
    return {"mode": "single", "entries": [entry], "skipped": [],
            "profiles": profiles, "duplicate": duplicate}


def export_payload(scheme: dict) -> dict:
    """单方案导出对象（与导入格式②对称，额外带 `name` 便于往返）。"""
    meta = {k: scheme[k] for k in ("market", "direction", "period", "mode") if scheme.get(k)}
    return {
        "name": scheme.get("name"),
        "label": scheme.get("label") or "",
        "desc": scheme.get("desc") or "",
        "_meta": meta,
        "factor_profile": scheme.get("factor_profile"),
        "config": scheme.get("config") or {},
    }


def export_filename(name: str) -> str:
    """导出文件名（去掉路径分隔符等非法字符）。"""
    safe = str(name).replace("/", "_").replace("\\", "_").replace(":", "_").strip() or "scheme"
    return f"方案_{safe}.json"


def content_disposition(filename: str, ascii_fallback: str = "scheme.json") -> str:
    """RFC 5987：中文文件名必须走 `filename*=UTF-8''...`（latin-1 头直接放中文会报错）。"""
    quoted = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quoted}"
