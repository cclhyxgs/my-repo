# -*- coding: utf-8 -*-
"""F-303 报告内容（reportData）一致性 —— 验收测试。

断言来源（纪律 4）：
  feature-matrix.md:24 F-303「验收标准」列 =
    analyze 响应 **`reportData.factorScore` 与 `report_text` 决策徽章结论一致（同 `ctx.decision` 派生）**
  业务规则列 = `reportData` 由 `_build_report_data(ctx)` 产出；`ctx.decision` 建仓裁决单一真相源
  H5 方案列   = 后端计算 + 前端渲染 `reportData`；`report_text` **直接透传**
  平台差异列  = 前端**不可**自行拼接（与 `report_builder` 语义分叉 → 「核心结论」与「多空信号」不一致）

用户已拍板（2026-09-12）：
  B32 = ① **不下发** `ctx.decision` —— 验收只要求"同 ctx.decision 派生"，`decisionConclusion`
        已是该派生结果；新增字段会鼓励前端自行解释决策，与平台差异列风险③相悖。

本条无需新增生产代码，交付物即本文件（F-301/302 已交付同一端点的 reportData + report_text）。
"""

import os
import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
ENGINE_ANALYZE = ROOT / "engine" / "analyze_service.py"


@pytest.fixture(scope="module", autouse=True)
def seeded_quant_config():
    """补种真实 `config/`（含 quant_model.json）并强制 quant_config 重载。

    与 F-301 同一个坑：隔离目录默认无配置，且 quant_config 在导入期已缓存；
    必须覆盖式拷贝 + `load_config(force_reload=True)`。
    """
    src = ROOT / "config"
    # 与 F-301 同源修正：优先用 App 真实运行目录，项目 config/ 仅作回退
    # （项目 config/ 可能只有 quant_model.json.bak，属源码直跑的兜底残缺态）
    _runtime = Path(os.environ.get("LOCALAPPDATA", "")) / "M-Bull" / "config"
    if (_runtime / "quant_model.json").is_file():
        src = _runtime
    assert src.is_dir(), f"缺配置目录 {src}"
    dst = Path(os.environ["QUANT_SYSTEM_DIR"]) / "config"
    dst.mkdir(parents=True, exist_ok=True)
    for item in sorted(src.iterdir()):
        if item.is_file():
            shutil.copy2(item, dst / item.name)

    from engine import quant_config

    quant_config.load_config(force_reload=True)


def _core_conclusion(report_text: str) -> str:
    """取出 report_text 中【核心结论】段落的第一行（决策徽章）。"""
    lines = [ln.strip() for ln in report_text.splitlines()]
    for i, line in enumerate(lines):
        if "核心结论" in line:
            for nxt in lines[i + 1:]:
                if nxt:
                    return nxt
    return ""


# ================================================================
# RS1~RS4：一致性的核心断言
# ================================================================
def test_rs1_core_conclusion_matches_decision_conclusion(client):
    """RS1（验收）：`report_text` 的【核心结论】徽章 == `reportData.decisionConclusion`。

    两者同源于 `ctx.decision`（`engine/report_builder.py` 的 `compute_decision`）。
    """
    body = client.post("/api/analyze", json={"code": "sh600519", "k_type": "日K"}).json()
    report = body["reportData"]

    conclusion = _core_conclusion(body["report_text"])
    assert conclusion, f"report_text 未找到【核心结论】：{body['report_text'][:200]}"
    assert conclusion == report["decisionConclusion"], (
        f"徽章与 decisionConclusion 不一致：\n  徽章={conclusion!r}\n  decisionConclusion={report['decisionConclusion']!r}"
    )


def test_rs2_score_in_report_text_matches_factor_score(client):
    """RS2：`report_text` 中的「得分N」 == `round(reportData.factorScore)`。"""
    body = client.post("/api/analyze", json={"code": "sh600519", "k_type": "日K"}).json()
    score = body["reportData"]["factorScore"]

    hits = re.findall(r"得分\s*(-?\d+)", body["report_text"])
    assert hits, f"report_text 未出现「得分N」：{body['report_text'][:300]}"
    expected = f"{score:.0f}"          # 与 engine 侧 f"{score:.0f}" 同一种取整
    assert hits[0] == expected, f"分数不一致：report_text 得分{hits[0]} vs factorScore={score}（取整应为 {expected}）"


def test_rs3_tier_badge_matches_status(client):
    """RS3：`reportData.status`（入场档位）应出现在核心结论徽章中。"""
    body = client.post("/api/analyze", json={"code": "sh600519", "k_type": "日K"}).json()
    status = body["reportData"]["status"]
    conclusion = _core_conclusion(body["report_text"])
    assert status, f"status 为空：{body['reportData'].get('status')}"
    assert status in conclusion, f"档位「{status}」未出现在徽章「{conclusion}」"


def test_rs4_score_variants_consistent(client):
    """RS4：`scoreLong` 与 `factorScore` 一致（多空视角派生自同一分数）。"""
    report = client.post("/api/analyze", json={"code": "sh600519", "k_type": "日K"}).json()["reportData"]
    assert report["scoreLong"] == report["factorScore"], report


# ================================================================
# RS5：report_text 直接透传（服务端不改写报告语义）
# ================================================================
def test_rs5_report_text_is_passthrough(client, monkeypatch):
    """RS5（H5 方案列「直接透传」）：端点返回的 `report_text` 与 engine 产出**逐字相同**。"""
    from server.adapters import engine_bridge

    synthetic_text = "▌【核心结论】\n⏳ 观望 — 未达观望线，等待信号\n\n数据来源：日K · 新浪未复权"
    synthetic = {
        "success": True,
        "reportData": {"factorScore": -7.8, "status": "观望", "env": "中性",
                       "decisionConclusion": "⏳ 观望 — 未达观望线，等待信号",
                       "scoreLong": -7.8, "report_text": synthetic_text},
        "report_text": synthetic_text,
        "used_scheme_name": "1", "used_scheme_period": "日K", "scheme_summary": {},
    }
    monkeypatch.setattr(engine_bridge, "analyze_stock", lambda *a, **k: synthetic)

    body = client.post("/api/analyze", json={"code": "sh600519", "k_type": "日K"}).json()
    assert body["report_text"] == synthetic_text, "服务端改写了 report_text（违反「直接透传」）"
    assert body["reportData"]["report_text"] == synthetic_text


def test_rs6_report_built_by_engine_only():
    """RS6（平台差异列「前端不可自行拼接」）：报告组装只在 engine，analyze 核心调用 engine 的 builder。"""
    src = ENGINE_ANALYZE.read_text(encoding="utf-8")
    assert "from engine.report_data import build_report_data" in src, "analyze 核心未使用 engine 的报告组装器"
    # 不得再引用 UI 层的 _build_report_data
    assert "from ui" not in src and "import ui" not in src, "engine 侧反向依赖 UI 层报告实现"


# ================================================================
# RS7：字段完整性与体积（体积仅记录，供前端懒加载参考）
# ================================================================
def test_rs7_report_data_has_render_keys(client):
    """RS7：reportData 含前端渲染必需键；体积打印供参考（不做硬断言）。"""
    report = client.post("/api/analyze", json={"code": "sh600519", "k_type": "日K"}).json()["reportData"]
    required = ["factorScore", "status", "env", "decisionConclusion", "keyLevels", "tech_board", "signals", "report_text"]
    missing = [k for k in required if k not in report]
    assert not missing, f"缺渲染键：{missing}（现有 {len(report)} 键：{sorted(report)[:15]}…）"

    size = len(__import__("json").dumps(report, ensure_ascii=False, default=str))
    print(f"\n[体积参考] reportData ≈ {size/1024:.1f} KB，键数 {len(report)}（前端首屏建议懒加载，属前端职责）")


# ================================================================
# RS8：期货同理，一次调用内同源
# ================================================================
def test_rs8_futures_report_and_contract_same_source(client, monkeypatch):
    """RS8：期货响应的 `futures_contract` 与 reportData 同一次调用产出（不做二次装配）。"""
    from server.adapters import engine_bridge

    contract = {"symbol": "rb", "name": "螺纹钢", "exchange": "SHFE", "multiplier": 10,
                "margin_rate": 0.13, "tick_size": 1.0, "main_code": "rb0",
                "contract_month": None, "is_continuous": True}
    text = "▌【核心结论】\n✅ 标准 — 达到标准线\n"
    synthetic = {
        "success": True,
        "reportData": {"factorScore": 25.0, "status": "标准", "env": "中性",
                       "decisionConclusion": "✅ 标准 — 达到标准线", "scoreLong": 25.0,
                       "futures_contract": contract, "report_text": text},
        "report_text": text,
        "used_scheme_name": "期货日K", "used_scheme_period": "日K", "used_scheme_direction": "long",
        "scheme_summary": {},
    }
    monkeypatch.setattr(engine_bridge, "futures_spec", lambda code: object())
    monkeypatch.setattr(engine_bridge, "analyze_futures", lambda *a, **k: synthetic)

    body = client.post("/api/analyze", json={"code": "rb0", "market_type": "futures", "k_type": "日K"}).json()
    assert body["futures_contract"] == contract
    assert body["report_text"] == text
    assert _core_conclusion(text) == body["reportData"]["decisionConclusion"]
