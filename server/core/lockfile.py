# -*- coding: utf-8 -*-
"""依赖锁清单校验（§2.13-1）。

锁清单的单一真相源 = requirements-server.txt。本模块不内置任何期望版本号副本，
避免"代码里写死的 3.0.3"与"文件里写的"发生漂移——T3a 断言的就是二者一致。

T3 为硬断言：版本不符时 /api/health 必须返回 503，不允许降级为日志。
"""

import importlib
import re
from pathlib import Path
from typing import Optional

from server import settings

# 需要参与运行时校验的包名（锁清单里其余条目按注释/列表处理，不校验）
VERIFIABLE = ("pandas", "numpy", "matplotlib", "cryptography", "fastapi", "uvicorn", "pydantic")
# 安装名 → 导入名差异（本骨架内同名，保留映射以便后续扩展）
MODULE_ALIASES = {
    "uvicorn": "uvicorn",
    "pyyaml": "yaml",
    # F-601：multipart 表单/文件解析器（FastAPI 的 File/Form 依赖）
    "python-multipart": "multipart",
}

_PIN_RE = re.compile(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]+\])?==([0-9][^\s;]*)")


def read_pins(path: Optional[Path] = None) -> dict:
    """解析 `name==version` 行；`name[extra]==version` 取裸名；忽略注释行。"""
    target = Path(path) if path else settings.LOCK_FILE
    pins: dict = {}
    if not target.is_file():
        return pins
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _PIN_RE.match(line)
        if m:
            pins[m.group(1).lower()] = m.group(2)
    return pins


def installed_version(name: str) -> Optional[str]:
    module_name = MODULE_ALIASES.get(name, name)
    try:
        module = importlib.import_module(module_name)
    except Exception:
        return None
    return getattr(module, "__version__", None)


def installed_versions(names=None) -> dict:
    names = names or VERIFIABLE
    return {name: installed_version(name) for name in names}


def verify_locked_deps() -> dict:
    """返回 {ok, pins, installed, reasons}。reasons 非空即视为不合格（health → 503）。"""
    pins = read_pins()
    reasons = []

    if not pins:
        reasons.append(f"锁清单缺失或不可解析：{settings.LOCK_FILE}")

    installed = installed_versions(pins.keys() or None)

    for name, expected in pins.items():
        actual = installed.get(name)
        if actual is None:
            reasons.append(f"依赖未安装：{name}（锁定 {expected}）")
        elif actual != expected:
            reasons.append(f"依赖版本不符：{name} 运行时 {actual} != 锁定 {expected}")

    return {"ok": not reasons, "pins": pins, "installed": installed, "reasons": reasons}
