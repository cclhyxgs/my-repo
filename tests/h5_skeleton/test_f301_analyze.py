# -*- coding: utf-8 -*-
"""F-301 A股分析流程 —— 验收测试。

断言来源（纪律 4）：
  feature-matrix.md:22 F-301「验收标准」列 =
    ① `POST /api/analyze {code:'sh600519',k_type:'日K'}` 返回 `reportData` 且含
       `factorScore` / `entry_tier` / `env_*`
    ② K线 <30 条返回「无法评分」
  输出契约 = app-architecture.md:337
    `{reportData, report_text, discipline, entry_tier}`

用户已拍板（2026-09-12）：
  B24 = ① `reportData` 组装（~870 行）**上移 engine**，UI 层改薄委托（单一真相源）
  B25 = ① 「授权准入」最小子集**不纳入** F-301，单列为 P0 内独立任务
  B26 = `<30 条` → 422 `INSUFFICIENT_DATA`（message 含「无法评分」）；
        `market_type='futures'` → 422 `NOT_IMPLEMENTED`（待 F-302）

本文件先固化 **B24 上移的零语义改动证明**（AS0a~AS0c），
再覆盖端点验收（AS1~ASn）。
"""

import ast
import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
ENGINE_REPORT_DATA = ROOT / "engine" / "report_data.py"
UI_WEB_API = ROOT / "ui" / "web_api.py"

# 上移前（ui/web_api.py）各函数体（去 docstring）AST dump 的 sha256，取自搬迁脚本报表。
# ⚠️ 2026-09-27 更新（AI「查漏补缺」改造，有意变更，非意外漂移）：
#   _build_tech_board  —— 补 ATR 项（原看板缺 ATR，而止损位最该以它为据）
#   _build_report_data —— 新增 techRaw / recentBars 两键（供 AI 两层投喂）；
#                         其后 recentBars 根数 10 → 30（用户要求，看得出一个月形态节奏）
#   其余 5 个函数未动，哈希保持原值。
FROZEN_BODY_HASHES = {
    "_market_status_label": "e76dccb1e61ecaf72379ee75885c5d1726ef2bd3da76a5b1da248e4281c4b315",
    "_explain_factor": "0cc2bd93e25e3516e0b657c9238de057948d799da0e6deb2048bb91cf8a586b8",
    "_pct_chg": "342917661f50ac65917cd6e01b0a174bc527fd8f27307f410f86a774b95df336",
    "_stars_and_ratio": "2298bd0b711a08479ceba82f18b5d894dd1f126c93f7df02314468f47491076b",
    "_parse_star_count": "4e05b458d96117f3b7e85c971759a0eb2930fc7e25a8d5810f7fb4c2dc7a4e1f",
    "_build_tech_board": "60b83446ba19a6db5e963e530a4d7b425778b9687976d328be6901e73a6ca708",
    "_build_report_data": "754068df9cd9b8c40ba6b09766e71f4cb9003dfec22b2d9f883d938ee32dde6b",
}


def _body_ast_hash(path: Path, func_name: str) -> str:
    src = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0].value, "value", None), str):
                body = body[1:]
            dump = ast.dump(ast.Module(body=body, type_ignores=[]))
            return hashlib.sha256(dump.encode("utf-8")).hexdigest()
    raise AssertionError(f"{path} 中未找到 {func_name}")


# ================================================================
# AS0：B24 上移的零语义改动证明
# ================================================================
@pytest.fixture(scope="module", autouse=True)
def seeded_quant_config():
    """把项目真实 `config/` 补种进测试隔离目录（**仅补缺，不覆盖**）。

    `quant_config.load_market_scheme(...)` 需要真实的方案配置；隔离目录默认为空，
    会导致「未配置 A股·日K 方案」而分析无法进行。与 F-204「拷真实股票池」同源做法：
    用真实配置、离线、与运行日期解耦。
    """
    import os
    import shutil

    src = ROOT / "config"
    # 源目录优先取「App 真实运行目录」，项目 config/ 仅作回退。
    # 原因：项目 config/ 只是**源码直跑**时的兜底，可能残缺（当前它只有
    # quant_model.json.bak、没有正式文件）→ 补种后断言失败，看起来像代码回归，
    # 实为测试注入源选错。真实运行目录才是开发机上引擎实际加载的配置。
    _runtime = Path(os.environ.get("LOCALAPPDATA", "")) / "M-Bull" / "config"
    if (_runtime / "quant_model.json").is_file():
        src = _runtime
    assert src.is_dir(), f"缺配置目录 {src}"
    # ⚠️ 目标必须是 conftest 设定的隔离目录（`os.environ["QUANT_SYSTEM_DIR"]`，
    # 即 <temp>/mbull_h5_skeleton_dev），**不是**项目下的 .mbull-h5-dev。
    dst = Path(os.environ["QUANT_SYSTEM_DIR"]) / "config"
    dst.mkdir(parents=True, exist_ok=True)
    copied = []
    # 用**覆盖**而非「仅补缺」：隔离目录里可能已有 engine 早先生成的空 quant_model.json，
    # 只补缺会跳过它，导致 load_market_scheme 报「未配置方案」。
    for item in sorted(src.iterdir()):
        if item.is_file():
            shutil.copy2(item, dst / item.name)
            copied.append(item.name)
    assert (dst / "quant_model.json").is_file(), f"补种后仍缺 quant_model.json：{copied}"

    # 关键：config 目录此前为空，quant_config 已在导入期缓存了空配置，
    # 必须强制重载，否则 load_market_scheme 仍返回「未配置方案」。
    from engine import quant_config

    quant_config.load_config(force_reload=True)
    return copied


def test_as0a_engine_functions_body_identical_to_frozen_hashes():
    """AS0a：engine/report_data.py 的 7 个函数体 == 冻结基线（AST 结构）。

    原意是证明 B24「UI→engine 上移」为零语义改动。上移已完成，本测试转为**基线冻结**：
    任何函数体变动都会失败，迫使改动者显式更新 FROZEN_BODY_HASHES 并注明原因，
    防止「改引擎逻辑时悄悄改坏了 UI 侧行为」。
    """
    for name, expected in FROZEN_BODY_HASHES.items():
        actual = _body_ast_hash(ENGINE_REPORT_DATA, name)
        assert actual == expected, f"{name} 上移后函数体漂移：{actual} != {expected}"


def test_as0b_ui_layer_is_thin_delegation():
    """AS0b：UI 层不再是实现方 —— 7 个函数均退化为对 engine.report_data 的单条委托。"""
    src = UI_WEB_API.read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in FROZEN_BODY_HASHES}
    assert set(found) == set(FROZEN_BODY_HASHES), f"UI 层缺委托：{set(FROZEN_BODY_HASHES) - set(found)}"

    for name, node in found.items():
        segment = ast.get_source_segment(src, node)
        assert "engine.report_data" in segment, f"{name} 未委托到 engine.report_data"
        # 委托体必须极短，否则说明实现细节仍留在 UI 层
        assert len(segment.splitlines()) <= 10, f"{name} 委托体过长（{len(segment.splitlines())} 行）"
        assert "for " not in segment and "if " not in segment, f"{name} 委托体仍含逻辑"


def test_as0c_engine_module_has_no_ui_dependency():
    """AS0c：engine/report_data.py 不得 import `ui.*`（否则依赖方向反转，上移不成立）。"""
    src = ENGINE_REPORT_DATA.read_text(encoding="utf-8")
    offenders = re.findall(r"^\s*(?:from|import)\s+ui(?:\.|\s|$).*$", src, re.M)
    assert not offenders, f"engine 模块反向依赖 UI 层：{offenders}"
    assert ENGINE_REPORT_DATA.is_file()


def test_as0d_public_alias_for_service_consumers():
    """AS0d：engine 侧公开别名为 `build_report_data`（服务端不依赖下划线私有名）。"""
    src = ENGINE_REPORT_DATA.read_text(encoding="utf-8")
    assert re.search(r"^build_report_data\s*=\s*_build_report_data\s*$", src, re.M), "缺公开别名"


def test_as0e_engine_module_importable():
    """AS0e：模块可独立导入（engine 侧不依赖任何 UI 运行时）。"""
    import importlib

    mod = importlib.import_module("engine.report_data")
    assert callable(mod.build_report_data)
    assert callable(mod._build_tech_board)


# ================================================================
# AS0f~AS0h：B27 上移 `analyze()` 主体的结构性证明
#   行为等价性由真实数据 A/B 对拍证明（旧 WebAPI.analyze vs 新 analyze_stock，
#   reportData 逐字段深度相等 —— 见项目记忆 2026-09-12「步骤 2」与
#   `.mbull-h5-dev/ab_analyze_equiv.py` 证据脚本）；此处固化可长期回归的结构约束。
# ================================================================
ENGINE_ANALYZE = ROOT / "engine" / "analyze_service.py"


def test_as0f_ui_analyze_is_thin_delegation():
    """AS0f：`WebAPI.analyze` 已退化为委托 —— 评分链不再出现在 UI 层。"""
    src = UI_WEB_API.read_text(encoding="utf-8")
    node = None
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.FunctionDef) and n.name == "analyze" and n.lineno > 200:
            node = n
            break
    assert node is not None, "未找到 WebAPI.analyze"
    segment = ast.get_source_segment(src, node)
    assert "engine.analyze_service" in segment and "analyze_stock(" in segment, "未委托到 engine"
    assert "TradingPipeline.execute" not in segment, "评分链仍留在 UI 层"
    assert "build_report_data(" not in segment.replace("engine.analyze_service", ""), "报告组装仍在 UI 层"
    assert len(segment.splitlines()) <= 60, f"委托体过长：{len(segment.splitlines())} 行"


def test_as0g_engine_analyze_is_free_of_global_market_state():
    """AS0g：`analyze_stock` 函数体内不出现 `self.*`（解掉 R-09 全局态），且显式暴露 4 个钩子。

    注意：用 AST 检查**函数体**，不能对全文做子串匹配 —— 模块 docstring 里会描述
    「原先读 self._market_type，现已移除」，子串匹配会误报。
    """
    tree = ast.parse(ENGINE_ANALYZE.read_text(encoding="utf-8"))
    fn = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "analyze_stock"),
        None,
    )
    assert fn is not None, "未找到 analyze_stock"

    self_attrs = [
        n.attr for n in ast.walk(fn)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self"
    ]
    assert not self_attrs, f"函数体内仍引用 self.*：{self_attrs}（R-09 全局态未解耦）"

    params = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    for hook in ("authorize", "is_basic_mode", "basic_preview", "on_quota"):
        assert hook in params, f"缺钩子参数 {hook}（UI 侧分支需在原调用位注入）"


def test_as0h_engine_analyze_has_no_ui_or_license_dependency():
    """AS0h：不得反向 import `ui.*` / `license.*`（授权经钩子注入，避免依赖反转）。"""
    src = ENGINE_ANALYZE.read_text(encoding="utf-8")
    offenders = re.findall(r"^\s*(?:from|import)\s+(?:ui|license)(?:\.|\s|$).*$", src, re.M)
    assert not offenders, f"engine 模块依赖了 UI/授权层：{offenders}"


# ================================================================
# AS1~AS7：端点验收（断言来自 F-301 验收标准列）
# ================================================================
def test_as1_analyze_returns_report_data_with_score_and_tier(client):
    """AS1（验收①）：`{code:'sh600519',k_type:'日K'}` → reportData 含 `factorScore`，
    顶层含 §3.2 要求的 `entry_tier`，并带环境口径字段。

    字段名说明（B28）：验收写的 `entry_tier` / `env_*` 在实现里不存在 ——
      入场档位的真实字段是 `reportData.status`；环境是 `reportData.env`（字符串，非 `env_*` 明细）。
    故：顶层 `entry_tier` 取 `reportData.status`，环境以 `env` 断言。
    """
    r = client.post("/api/analyze", json={"code": "sh600519", "k_type": "日K"})
    assert r.status_code == 200, r.text
    body = r.json()

    report = body["reportData"]
    assert isinstance(report["factorScore"], (int, float)), f"factorScore 非数值：{report.get('factorScore')}"
    assert "env" in report, f"缺环境字段：{sorted(report)[:20]}"
    assert body["entry_tier"] == report.get("status"), (
        f"顶层 entry_tier 应与 reportData.status 一致：{body['entry_tier']} vs {report.get('status')}"
    )
    assert body["report_text"], "report_text 不应为空"
    assert isinstance(body["discipline"], dict)


def test_as2_insufficient_klines_returns_422_cannot_score(client, monkeypatch):
    """AS2（验收②）：K线 <30 条 → 422 `INSUFFICIENT_DATA`，message 含「无法评分」。

    端到端注入：把 `DataAPI.get_kline` 换成 10 根（<30），走真实 engine 判据。
    """
    import pandas as pd

    from engine.data_layer import DataAPI

    short = pd.DataFrame({
        "trade_time": pd.to_datetime([f"2026-09-{d:02d}" for d in range(1, 11)]),
        "open": [1275.0] * 10, "high": [1280.0] * 10,
        "low": [1270.0] * 10, "close": [1276.0] * 10,
        "volume": [34801.0] * 10,
    })
    monkeypatch.setattr(DataAPI, "get_kline", lambda *a, **k: (short, None))

    r = client.post("/api/analyze", json={"code": "sh600519", "k_type": "日K"})
    assert r.status_code == 422, f"应 422，实际 {r.status_code}：{r.text}"
    err = r.json()["error"]
    assert err["code"] == "INSUFFICIENT_DATA", err
    assert "无法评分" in err["message"], err["message"]


def test_as2b_engine_side_has_the_30_bar_rule():
    """AS2b：engine 侧确实存在 `< 30 → 无法评分` 判据（防止端点映射与实现脱节）。"""
    src = ENGINE_ANALYZE.read_text(encoding="utf-8")
    assert "len(dff_score) < 30" in src, "engine 侧缺少 30 条判据"
    assert "无法评分" in src, "engine 侧缺少「无法评分」文案"


def test_as3_invalid_and_unknown_codes(client):
    """AS3：空 code → 422；不可解析代码 → 404。"""
    r1 = client.post("/api/analyze", json={"code": "  "})
    assert r1.status_code == 422, r1.text
    r2 = client.post("/api/analyze", json={"code": "notacode"})
    assert r2.status_code == 404, r2.text
    assert r2.json()["error"]["code"] == "NOT_FOUND"


def test_as4_futures_now_routed_to_futures_branch(client):
    """AS4：期货请求**不再**返回 `NOT_IMPLEMENTED` —— F-302 已交付该分支。

    （F-301 交付时该分支未开放，故当时断言 NOT_IMPLEMENTED；F-302 后本条改为断言
    「已进入期货分支」，期货自身的验收在 test_f302_futures.py。）
    """
    r = client.post("/api/analyze", json={"code": "rb2501", "market_type": "futures"})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] != "NOT_IMPLEMENTED", "期货分支未接入（F-302 未生效）"


def test_as5_scheme_is_echoed_back(client):
    """AS5：`scheme` 透传生效 —— 用实际命中的方案名回显一致。"""
    first = client.post("/api/analyze", json={"code": "sh600519", "k_type": "日K"}).json()
    scheme = first.get("used_scheme_name")
    assert scheme, f"未回显实际使用的方案：{first}"

    second = client.post("/api/analyze", json={"code": "sh600519", "k_type": "日K", "scheme": scheme}).json()
    assert second["used_scheme_name"] == scheme, second
    assert second["used_scheme_period"] == "日K", second


def test_as6_report_text_has_source_annotation(client):
    """AS6：报告尾部带「数据来源」标注（上移前 analyze 的行为，须保持）。"""
    body = client.post("/api/analyze", json={"code": "sh600519", "k_type": "日K"}).json()
    assert "数据来源：" in body["report_text"], body["report_text"][-200:]


def test_as7_contract_top_level_keys(client):
    """AS7：顶层键符合 §3.2 `{reportData, report_text, discipline, entry_tier}`。"""
    body = client.post("/api/analyze", json={"code": "sh600519", "k_type": "日K"}).json()
    assert {"reportData", "report_text", "discipline", "entry_tier"} <= set(body), sorted(body)
