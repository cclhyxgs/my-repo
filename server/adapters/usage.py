# -*- coding: utf-8 -*-
"""用法模式（初/高级）适配层 —— F-604（含 F-1601 只读子集）。

权威读取 `config/usage.json` 的 `usage_mode` 字段（与桌面 / engine 同一持久化文件，
见 `engine/quant_config.py:1234 get_usage_mode()`）。服务端**不修改 engine 任何代码**，
只按同一文件读取。

默认 **basic**（F-604/F-1601 明文：H5 新户起步初级）：仅当文件缺失或字段非法时生效；
文件里显式写 `advanced` 即返回 advanced。

⚠️ 与 engine 默认值（advanced，保护存量因子数据）的差异说明：
engine 的 advanced 默认只在「无 usage.json 且有存量高级配置」的桌面场景兜底保护数据；
H5 面向新户且该文件缺失时本就是空配置，按计划应起步 basic → 二者不冲突。
"""

import json
import os

from server import settings

VALID = ("basic", "advanced")


def _usage_path() -> str:
    """`QUANT_SYSTEM_DIR/config/usage.json`（与 engine CONFIG_DIR 同路径）。"""
    config_dir = settings.resolve_quant_system_dir() / "config"
    os.makedirs(config_dir, exist_ok=True)
    return str(config_dir / "usage.json")


def get_usage() -> str:
    """返回 'basic' | 'advanced'。文件缺失 / 字段非法 → 默认 'basic'。"""
    try:
        with open(_usage_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        mode = data.get("usage_mode")
        if mode in VALID:
            return mode
    except Exception:  # noqa: BLE001 —— 读取失败按缺省 basic 兜底，不阻断任何接口
        pass
    return "basic"


def is_basic() -> bool:
    return get_usage() == "basic"


def set_usage(mode: str) -> str:
    """显式写用法模式（F-1601 切换；仅接受 basic/advanced，非法值 422 由 API 层拦截）。"""
    if mode not in VALID:
        raise ValueError(f"非法用法模式：{mode}（可选 {'/'.join(VALID)}）")
    with open(_usage_path(), "w", encoding="utf-8") as f:
        json.dump({"usage_mode": mode}, f, ensure_ascii=False)
    return mode