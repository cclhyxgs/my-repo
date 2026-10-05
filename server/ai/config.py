# -*- coding: utf-8 -*-
"""AI（DeepSeek）解读层配置。

读写 `config/ai_config.json`：
  {"api_key": str, "model": str, "enabled": bool}

安全约定：`api_key` 只落盘、不通过配置读取端点回传明文。
"""

import json
import os
import sys

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
# 项目根 = server/ai 的上一级（server）再上一级（项目根）→ config/
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_MODULE_DIR))

_APP_NAME = "M-Bull"


def _config_dir() -> str:
    """解析配置目录，与 `engine.config.get_app_dir()` 保持同一口径（不 import engine，守分层）。

    ⛔ frozen 态绝不能用 `__file__` 反推项目根：PyInstaller onefile 下 `__file__`
      落在 `sys._MEIPASS`（%TEMP%\\_MEIxxxxx），进程退出即被清理 —— 用户填的 API Key
      当时能用、重启后就没了，表现为「每次打开都要重新填 Key / AI 提示不可用」。
    """
    env_dir = os.environ.get("QUANT_SYSTEM_DIR")
    if env_dir:
        return os.path.join(os.path.abspath(env_dir), "config")
    exe = os.path.basename(sys.executable).lower()
    # 冻结态（PyInstaller/Nuitka）或非 python 解释器启动 → 落 AppData 持久化
    if getattr(sys, "frozen", False) or not (exe.startswith("python") or exe == "py.exe"):
        return os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
            _APP_NAME, "config",
        )
    # 开发态：项目根 config/（历史行为，与 EXE 配置互不干扰）
    return os.path.join(_PROJECT_ROOT, "config")


CONFIG_PATH = os.path.join(_config_dir(), "ai_config.json")

DEFAULT_MODEL = "deepseek-flash"
VALID_MODELS = ("deepseek-flash", "deepseek-v4-pro")


def _read() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {"api_key": "", "model": DEFAULT_MODEL, "enabled": False}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"api_key": "", "model": DEFAULT_MODEL, "enabled": False}
    if not isinstance(data, dict):
        return {"api_key": "", "model": DEFAULT_MODEL, "enabled": False}
    model = data.get("model") if data.get("model") in VALID_MODELS else DEFAULT_MODEL
    api_key = str(data.get("api_key") or "")
    return {"api_key": api_key, "model": model, "enabled": bool(api_key)}


def get_ai_config() -> dict:
    """服务端内部读（含 key，仅服务内用，勿直接回传前端）。"""
    return _read()


def public_ai_config() -> dict:
    """前端配置快照：不透出 api_key 明文。"""
    cfg = _read()
    return {"configured": cfg["enabled"], "model": cfg["model"], "enabled": cfg["enabled"]}


def save_ai_config(api_key: str = "", model: str = None) -> dict:
    """保存配置。api_key 为空时保留原 key；model 为空时保持原值。"""
    cur = _read()
    api_key = (api_key or "").strip()
    model = model if model in VALID_MODELS else cur["model"]
    data = {
        "api_key": api_key if api_key else cur["api_key"],
        "model": model,
        "enabled": bool(api_key if api_key else cur["api_key"]),
    }
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data