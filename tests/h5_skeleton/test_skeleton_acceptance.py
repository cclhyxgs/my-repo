# -*- coding: utf-8 -*-
"""M-Bull H5 后端骨架 —— 验收测试 T1~T9。

断言来源（B2 已确认，不得自行发明）：
  = migration-plan.md §3.1「后端依赖」4 条 + app-architecture.md §2.13 三条硬约束
  = migration-plan §七 第 1~7 步的执行前置

硬断言纪律（B2）：T3 / T4 不允许降级为「日志输出」——不合格即 503 / 即报错。
"""

import asyncio
import importlib
import json
import re
import time as _time
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

ENGINE_LOCKED = ["pandas", "numpy", "matplotlib", "cryptography"]
WEB_PINNED = ["fastapi", "uvicorn", "pydantic"]
LOCKED_VALUES = {
    "pandas": "3.0.3",
    "numpy": "2.5.1",
    "matplotlib": "3.11.0",
    "cryptography": "49.0.0",
}
EXPECTED_FACTOR_COUNT = 21  # 依据 完整规格_合并版.md:695「因子注册表（factor_registry.py，21 因子）」
SSE_EVENTS = {"progress", "log", "stage", "done", "error"}
SSE_LOCATION_NEEDLE = "location /api/_probe/"


# ---------------------------------------------------------------- helpers
def _read(path: Path) -> str:
    assert path.is_file(), f"缺少文件：{path}"
    return path.read_text(encoding="utf-8")


def _extract_block(text: str, needle: str) -> str:
    """取出包含 needle 的『花括号配平』区块（用于 nginx location / compose service）。"""
    idx = text.find(needle)
    assert idx >= 0, f"未找到区块标记：{needle}"
    start = text.find("{", idx)
    assert start >= 0, f"{needle} 后无花括号"
    depth, i = 0, start
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    raise AssertionError(f"{needle} 区块花括号不配平")


def _parse_pins(text: str) -> dict:
    """解析 `name==version` 行；列表式依赖（uvicorn[standard]）取裸名。"""
    pins = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]+\])?==([0-9][^\s;]*)", line)
        if m:
            pins[m.group(1).lower()] = m.group(2)
    return pins


def _installed(module_name: str):
    try:
        return getattr(importlib.import_module(module_name), "__version__", None)
    except Exception:
        return None


# ---------------------------------------------------------------- T1
def test_t1_container_and_pg_timezone(deploy_dir):
    """T1 / §2.13-2：容器 TZ=Asia/Shanghai；PG timezone='Asia/Shanghai'。"""
    compose = yaml.safe_load(_read(deploy_dir / "docker-compose.yml"))
    services = compose["services"]

    api_env = services["api"].get("environment") or {}
    if isinstance(api_env, list):
        api_env = dict(kv.split("=", 1) for kv in api_env)
    assert api_env.get("TZ") == "Asia/Shanghai", "api 服务缺 TZ=Asia/Shanghai"

    assert "postgres" in services, "compose 缺 postgres 服务定义（可 profiles 门控）"
    pg_block = yaml.safe_dump(services["postgres"], allow_unicode=True)
    assert "Asia/Shanghai" in pg_block, "postgres 服务未声明 timezone='Asia/Shanghai'"

    assert services["postgres"].get("profiles") == ["p1"], (
        "PG 必须 profiles:[p1] 门控，否则本次会拉起半配置服务导致 compose 报错（B4）"
    )


# ---------------------------------------------------------------- T2
def test_t2_nginx_sse_no_buffering(deploy_dir):
    """T2 / §2.13-3 + R-12：SSE location 关缓冲、长超时、声明 X-Accel-Buffering、不 gzip。"""
    conf = _read(deploy_dir / "nginx.conf")
    block = _extract_block(conf, SSE_LOCATION_NEEDLE)

    assert re.search(r"proxy_buffering\s+off\s*;", block), "SSE location 缺 proxy_buffering off;"
    assert re.search(r"proxy_read_timeout\s+3600s\s*;", block), "SSE location 缺 proxy_read_timeout 3600s;"
    assert re.search(r"X-Accel-Buffering\s+no", block), "SSE location 缺 X-Accel-Buffering no;"
    assert re.search(r"gzip\s+off\s*;", block), "SSE location 未显式关闭 gzip"
    assert not re.search(r"gzip\s+on\s*;", block), "SSE location 出现 gzip on"


# ---------------------------------------------------------------- T3
def test_t3a_runtime_versions_equal_locked(lock_file):
    """T3a 硬断言 / §2.13-1：运行时实测版本 == 锁文件声明值，任一不符即失败。"""
    pins = _parse_pins(_read(lock_file))

    for name, expected in LOCKED_VALUES.items():
        assert pins.get(name) == expected, f"{name} 锁文件声明 {pins.get(name)!r} != §2.13 锁定 {expected}"
        actual = _installed(name)
        assert actual is not None, f"{name} 未安装"
        assert actual == expected, f"{name} 运行时 {actual} != 锁定 {expected}（§2.13 偏离即破坏约束）"

    for name in WEB_PINNED:
        assert name in pins, f"锁文件缺 {name} 显式钉版（B5）"
        assert _installed(name) == pins[name], f"{name} 运行时 {_installed(name)} != 锁文件 {pins[name]}"


def test_t3b_health_returns_503_on_version_mismatch(client, monkeypatch):
    """T3b 硬断言：版本不符时 /api/health 必须 503 —— 不许吞。"""
    from server.core import lockfile

    real = lockfile.read_pins

    def fake(path=None):
        pins = dict(real())
        pins["pandas"] = "0.0.0-broken"
        return pins

    monkeypatch.setattr(lockfile, "read_pins", fake)
    r = client.get("/api/health")
    assert r.status_code == 503, f"版本不符却返回 {r.status_code}，属于吞错"
    body = r.json()
    assert body["status"] == "degraded"
    assert any("pandas" in str(x) for x in body["reasons"]), body["reasons"]


def test_t3c_health_returns_503_on_engine_import_failure(client, monkeypatch):
    """T3c 硬断言：engine 导入失败时 /api/health 必须 503 —— 不许吞。"""
    from server.adapters import engine_bridge

    monkeypatch.setattr(
        engine_bridge,
        "engine_selfcheck",
        lambda force=False: {"importable": False, "error": "ImportError: 模拟 engine 导入失败"},
    )
    r = client.get("/api/health")
    assert r.status_code == 503, f"engine 不可导入却返回 {r.status_code}，属于吞错"
    body = r.json()
    assert body["engine"]["importable"] is False
    assert any("engine" in str(x).lower() for x in body["reasons"]), body["reasons"]


# ---------------------------------------------------------------- T4
def test_t4_engine_importable_and_factors_enumerable():
    """T4 硬断言 / §2.8：engine 直接 import 成功，21 因子可枚举且元数据齐备。"""
    from server.adapters import engine_bridge

    check = engine_bridge.engine_selfcheck(force=True)
    assert check["importable"] is True, f"engine 不可导入：{check.get('error')}"

    reg = importlib.import_module("engine.factor_registry")
    factors = reg.get_all_factors()
    assert isinstance(factors, dict) and factors, "get_all_factors() 未返回因子字典"
    assert len(factors) == EXPECTED_FACTOR_COUNT, (
        f"因子数 {len(factors)} != {EXPECTED_FACTOR_COUNT}（依据 完整规格_合并版.md:695）"
    )

    required = ["name", "label", "category", "params_schema", "default_weight",
                "default_direction", "default_stats"]
    for key, fdef in factors.items():
        assert key == fdef.name, f"注册键 {key} 与 FactorDef.name {fdef.name} 不一致"
        for field in required:
            assert hasattr(fdef, field), f"因子 {key} 缺元数据字段 {field}"
        assert callable(fdef.calc_fn), f"因子 {key} 的 calc_fn 不可调用"

    assert check["factors"] == EXPECTED_FACTOR_COUNT
    for mod in ("engine.score_calculator_v2", "engine.report_builder",
                "engine.trading_pipeline", "engine.quant_config"):
        importlib.import_module(mod)


# ---------------------------------------------------------------- T5
def test_t5_health_contract(client):
    """T5：/api/health 200 且 tz 固定 Asia/Shanghai、engine 可导入、时间带 +08:00。"""
    r = client.get("/api/health")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["status"] == "ok"
    assert body["tz"] == "Asia/Shanghai"
    assert body["engine"]["importable"] is True
    assert body["engine"]["factors"] == EXPECTED_FACTOR_COUNT
    assert body["deps"]["pandas"] == "3.0.3"
    assert body["reasons"] == []

    # ISO8601 + 固定 +08:00 偏移；允许可选微秒（now_iso() 保留精度）
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+08:00$", body["time"]
    ), body["time"]


# ---------------------------------------------------------------- T6
def test_t6_api_single_replica(deploy_dir):
    """T6 / §7.2 C1#2 + R-09：api 单副本，禁止 replicas>1 与 --workers>1。"""
    compose_text = _read(deploy_dir / "docker-compose.yml")
    compose = yaml.safe_load(compose_text)
    api = compose["services"]["api"]

    assert "replicas" not in (api.get("deploy") or {}), "api 出现 deploy.replicas，违反单副本"
    assert api.get("scale") in (None, 1), "api 出现 scale>1"
    assert not re.search(r"replicas\s*:\s*[2-9]", compose_text), "compose 文本出现 replicas>=2"

    dockerfile = _read(deploy_dir / "Dockerfile")
    m = re.search(r"--workers\D{1,4}(\d+)", dockerfile)
    assert m, "Dockerfile 未显式声明 --workers（单副本必须显式）"
    assert m.group(1) == "1", f"Dockerfile --workers={m.group(1)}，违反单副本（R-09）"

    note = compose_text + dockerfile
    assert "R-09" in note, "未注明 R-09（全局状态未外置前禁止扩副本）"


# ---------------------------------------------------------------- T7
def test_t7a_sse_headers_contract_and_monotonic_ids(client):
    """T7a / migration-plan:70 + R-12：响应头五件齐备、事件名合契约、id 单调递增。"""
    frames = []
    with client.stream("GET", "/api/_probe/sse") as r:
        assert r.status_code == 200, r.read()
        h = {k.lower(): v for k, v in r.headers.items()}
        assert h.get("content-type", "").startswith("text/event-stream"), h.get("content-type")
        assert "no-cache" in h.get("cache-control", ""), h.get("cache-control")
        assert h.get("x-accel-buffering") == "no", h.get("x-accel-buffering")
        assert h.get("connection") == "keep-alive", h.get("connection")
        assert h.get("content-encoding", "identity") == "identity", h.get("content-encoding")

        event, data, eid = None, None, None
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("id:"):
                eid = int(line.split(":", 1)[1].strip())
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
            elif line == "" and event:
                frames.append((event, eid, data))
                event = data = eid = None
                if len(frames) >= 2:
                    break

    assert len(frames) >= 2, f"SSE 未产出足够帧：{frames}"
    ids = [f[1] for f in frames]
    assert ids == sorted(ids) and len(set(ids)) == len(ids), f"id 非单调递增：{ids}"
    for ev, _, payload in frames:
        assert ev in SSE_EVENTS, f"事件名 {ev} 不在契约 {sorted(SSE_EVENTS)} 内"
        assert isinstance(payload, dict), f"data 非 JSON 对象：{payload!r}"


def test_t7b_sse_keepalive_within_15s():
    """T7b / migration-plan:70：15s keepalive 实测定时（硬断言，真实等待一次心跳）。"""
    from server.core import sse

    assert sse.KEEPALIVE_SECONDS == 15, f"keepalive 常量 {sse.KEEPALIVE_SECONDS} != 15"

    async def _quiet():
        while True:
            await asyncio.sleep(3600)
            yield  # pragma: no cover

    async def _first_keepalive_latency() -> float:
        started = _time.monotonic()
        async for chunk in sse.sse_event_stream(_quiet()):
            if chunk.startswith(b":"):
                return _time.monotonic() - started
        raise AssertionError("流已结束却未收到 keepalive 帧")

    latency = asyncio.run(asyncio.wait_for(_first_keepalive_latency(), timeout=30))
    jitter = abs(latency - sse.KEEPALIVE_SECONDS)
    assert jitter <= 2.0, f"keepalive 实测 {latency:.2f}s 偏离 15s 超过 2s"


# ---------------------------------------------------------------- T8
def test_t8_layering_static_scan(server_dir):
    """T8 / §2.8 + 补充约束 1：engine 引用只允许出现在 adapters/engine_bridge.py。"""
    engine_ref = re.compile(r"^\s*(?:from|import)\s+engine(?:\.|\s|$)", re.M)
    bridge = server_dir / "adapters" / "engine_bridge.py"

    offenders = []
    for py in sorted(server_dir.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        if py.resolve() == bridge.resolve():
            continue
        if engine_ref.search(text):
            offenders.append(str(py.relative_to(server_dir)))
    assert not offenders, f"engine 引用越界（只允许 engine_bridge）：{offenders}"
    assert bridge.is_file(), "缺 adapters/engine_bridge.py 收口文件"

    bad = []
    for pkg in ("core", "worker", "api"):
        for py in sorted((server_dir / pkg).rglob("*.py")):
            text = py.read_text(encoding="utf-8")
            if pkg != "api" and re.search(r"^\s*(?:from|import)\s+server\.api", text, re.M):
                bad.append(f"{py.name} 反向依赖 api")
    assert not bad, f"分层反向依赖：{bad}"


# ---------------------------------------------------------------- T9
def test_t9_process_timezone_asia_shanghai(server_dir):
    """T9 / §2.13-2 + R-19：进程统一 Asia/Shanghai，禁止裸 datetime.now()。"""
    from server.core import time as srv_time

    now = srv_time.now()
    assert now.tzinfo is not None, "now() 返回 naive datetime"
    assert now.utcoffset() == timedelta(hours=8), f"时区偏移 {now.utcoffset()} != +08:00"

    naive = re.compile(r"datetime\.now\(\s*\)")
    offenders = []
    for py in sorted(server_dir.rglob("*.py")):
        if py.name == "time.py":
            continue
        if naive.search(py.read_text(encoding="utf-8")):
            offenders.append(str(py.relative_to(server_dir)))
    assert not offenders, f"出现裸 datetime.now()（R-19 混用 UTC 风险）：{offenders}"
