# -*- coding: utf-8 -*-
"""AI（DeepSeek）解读层 —— 验收测试。

覆盖点：
  AS1  /api/ai/config GET PUT —— 不透出 key、保存后 configured 翻转
  AS2  四个解读端点 —— monkeypatch client.chat + analysis_adapter，
        验证把真实结果喂给模型、返回 {ok,text,scene}
  AS3  未配置 key 时统一 400 AI_NOT_CONFIGURED
  AS4  api_key 无效 → AI_AUTH_FAILED（client.chat 抛映射后的 ApiError）
"""

import pytest
from fastapi.testclient import TestClient

from server.ai import client as ai_client
from server.ai import config as ai_config


@pytest.fixture(autouse=True)
def _empty_ai_config(monkeypatch, tmp_path):
    """把 ai_config 的读写指向临时文件，测试隔离，避免污染真实配置。"""
    target = tmp_path / "ai_config.json"
    monkeypatch.setattr(ai_config, "CONFIG_PATH", str(target))
    return str(target)


@pytest.fixture(autouse=True)
def _fake_chat(monkeypatch):
    """全局 fake DeepSeek 返回固定文本，杜绝真实网络调用。"""
    # ⚠️ 签名必须跟上 client.chat 的形参（TaskMode 架构，2026-09-27 起每个场景都会按 mode
    #    传 temperature；同日又加了 scene 用于把「哪个入口在调用」写进 AI 日志）。
    #    少任何一项都会 TypeError，而且会伪装成「接口 500」，很难一眼看出是 mock 过期。
    def _fake_chat(prompt, system="", model=None, temperature=None, scene=None):
        assert model or prompt or scene is not None  # 形参使用，防 lint 告警
        return "这是一段 AI 解读。建议触发条件：回踩不破 MA20 可分批介入；止损设在 -5%。"
    monkeypatch.setattr(ai_client, "chat", _fake_chat)


# ================================================================
# AS1：配置端点
# ================================================================
def test_as1_get_config_hides_key(client, _empty_ai_config):
    """GET /api/ai/config 返回 configured/model/enabled，且不泄露 api_key。"""
    r = client.get("/api/ai/config")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "configured" in body and "model" in body and "enabled" in body
    assert "api_key" not in body, "配置快照不得返回 key 明文"
    assert body["configured"] is False


def test_as1_put_config_then_get(client, _empty_ai_config):
    """PUT 保存 key 后 configured 翻转为 true；GET 仍不透 key。"""
    r = client.put("/api/ai/config", json={"api_key": "sk-test-123", "model": "deepseek-flash"})
    assert r.status_code == 200, r.text
    assert r.json()["configured"] is True

    r2 = client.get("/api/ai/config")
    body = r2.json()
    assert body["configured"] is True
    assert "api_key" not in body


def test_as1_put_invalid_model_422(client, _empty_ai_config):
    """未知模型保存 → 422。"""
    r = client.put("/api/ai/config", json={"api_key": "sk-x", "model": "not-a-model"})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "INVALID_MODEL"


# ================================================================
# AS2：四个解读端点（monkeypatch 到真实结果 + fake chat）
# ================================================================
def test_as2_analyze(client, _empty_ai_config, monkeypatch):
    """/api/ai/analyze 用真实报告结构喂给模型并返回解读（fake chat）。"""
    client.put("/api/ai/config", json={"api_key": "sk-test"})
    # 真实分析链路耗网络，直接 stub analysis_adapter.analyze 返回契约结构
    from server.api import ai as ai_api
    from server.adapters import analysis as analysis_adapter

    def _fake_analyze(code, market_type="stock", k_type="日K", scheme=None):
        return {
            "reportData": {
                "code": code, "stock": "贵州茅台", "status": "观望",
                "factorScore": -7.8, "plus": [{"score": 2, "desc": "RSI 修复"}],
                "minus": [{"score": -3, "desc": "均线空排"}],
                "decisionConclusion": "观望 — 等待信号",
            },
            "report_text": "报告正文…", "scheme_summary": {"name": "A股·日K"},
        }
    monkeypatch.setattr(analysis_adapter, "analyze", _fake_analyze)
    assert hasattr(ai_api, "ai_analyze")  # 确保路由已挂

    r = client.post("/api/ai/analyze", json={"code": "sh600519", "k_type": "日K"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["scene"] == "analyze"
    assert "AI 解读" in body["text"]


def test_as2_diagnosis(client, _empty_ai_config):
    client.put("/api/ai/config", json={"api_key": "sk-test"})
    r = client.post("/api/ai/diagnosis", json={"item": {"code": "sh600519", "name": "贵州茅台", "category": "buy"}})
    assert r.status_code == 200, r.text
    assert r.json()["scene"] == "diagnosis" and r.json()["text"]


def test_as2_diagnosis_empty_item_422(client, _empty_ai_config):
    client.put("/api/ai/config", json={"api_key": "sk-test"})
    r = client.post("/api/ai/diagnosis", json={"item": {}})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "INVALID_ITEM"


def test_as2_scan(client, _empty_ai_config):
    client.put("/api/ai/config", json={"api_key": "sk-test"})
    items = [{"code": "sh600519", "name": "贵州茅台", "final_score": 85, "stars": "⭐⭐⭐", "sector": "白酒"}]
    r = client.post("/api/ai/scan", json={"items": items, "filters": {"rating": 3}})
    assert r.status_code == 200, r.text
    assert r.json()["scene"] == "scan" and r.json()["text"] and r.json()["covered"] == 1


def test_as2_scan_empty_422(client, _empty_ai_config):
    client.put("/api/ai/config", json={"api_key": "sk-test"})
    r = client.post("/api/ai/scan", json={"items": []})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "EMPTY_ITEMS"


def test_as2_ledger(client, _empty_ai_config, tmp_path, monkeypatch):
    """/api/ai/ledger 走真实 ledger_repo（独立 SQLite）+ fake chat。"""
    from server.db import session as db_session

    url = "sqlite:///" + (tmp_path / "mbull_ai_ledger.db").as_posix()
    monkeypatch.setenv("MBULL_DATABASE_URL", url)
    db_session.dispose()

    client.put("/api/ai/config", json={"api_key": "sk-test"})
    try:
        r = client.post("/api/ai/ledger", json={"market": "all"})
    finally:
        db_session.dispose()
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scene"] == "ledger" and body["text"]
    # 空库时应 covered=0 或合理值，且不报错
    assert body["covered"] >= 0


# ================================================================
# AS3：未配置 key
# ================================================================
def test_as3_not_configured_400(client, _empty_ai_config):
    """未保存 key 时任何解读端点 → 400 AI_NOT_CONFIGURED。"""
    r = client.post("/api/ai/analyze", json={"code": "sh600519"})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "AI_NOT_CONFIGURED"


# ================================================================
# AS5：自然语言 → 方案配置条件
# ================================================================
def _set_chat(client, monkeypatch, payload):
    """把 client.chat 替换为固定返回 JSON 文本。"""
    from server.ai import client as ai_client

    def _fake(prompt, system="", model=None, temperature=None, scene=None):
        assert prompt and system  # 确保 prompt 模板被调用
        return payload
    monkeypatch.setattr(ai_client, "chat", _fake)


def test_as5_scheme_conditions_clean(client, _empty_ai_config, monkeypatch):
    """合法文本 → filters/factor_tweaks 清洗，非法/基本面指标被剔除并进 unsupported。"""
    _set_chat(client, monkeypatch, '{"filters":[{"indicator":"rsi_14","op":">","value":80},{"signal":"多头排列"},{"indicator":"pb","op":"<","value":1},{"indicator":"sma_20","op":"!=","value":1}],"factor_tweaks":{"rsi_value":1.0,"fake_factor":5},"unsupported":["市净率"]}')
    client.put("/api/ai/config", json={"api_key": "sk-test"})

    r = client.post("/api/ai/scheme-conditions", json={"text": "帮我配 RSI 大于 80、均线多头排列"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    # pb 不在白名单被剔除；!= 不在白名单被剔除
    assert all("pb" not in str(c) for c in body["filters"])
    assert all(c.get("op") != "!=" for c in body["filters"])
    assert {"indicator": "rsi_14", "op": ">", "value": 80.0} in body["filters"]
    assert {"signal": "多头排列"} in body["filters"]
    # 假因子被剔除，合法因子保留
    assert body["factor_tweaks"].get("rsi_value") == 1.0
    assert "fake_factor" not in body["factor_tweaks"]
    assert body["unsupported"] == ["市净率"]


def test_as5_scheme_conditions_markdown_fence(client, _empty_ai_config, monkeypatch):
    """AI 返回带 ```json 围栏时也能解析。"""
    _set_chat(client, monkeypatch, '```json\n{"filters":[{"signal":"MACD金叉"}],"factor_tweaks":{},"unsupported":[]}\n```')
    client.put("/api/ai/config", json={"api_key": "sk-test"})
    r = client.post("/api/ai/scheme-conditions", json={"text": "MACD 金叉"})
    assert r.status_code == 200, r.text
    assert r.json()["filters"] == [{"signal": "MACD金叉"}]


def test_as5_scheme_conditions_parse_fail(client, _empty_ai_config, monkeypatch):
    """AI 返回非 JSON → 走该模式降级模板（契约变更 2026-09-27：不再抛 502）。

    TaskMode 架构下，解析失败与超时/限流同属「上游异常」，统一回 200 + degraded=True
    + 该模式的降级模板（可照着手工配置的指引），而不是甩一个错误码给用户。
    """
    _set_chat(client, monkeypatch, '完全不是 JSON')
    client.put("/api/ai/config", json={"api_key": "sk-test"})
    r = client.post("/api/ai/scheme-conditions", json={"text": "随便说点"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["degraded"] is True
    assert body["degrade_reason"] == "AI_PARSE_FAILED"
    assert body["filters"] == [] and body["factor_tweaks"] == {}


def test_as5_scheme_conditions_empty_text_422(client, _empty_ai_config, monkeypatch):
    """空文本 → 422 EMPTY_TEXT。"""
    _set_chat(client, monkeypatch, '{}')
    client.put("/api/ai/config", json={"api_key": "sk-test"})
    r = client.post("/api/ai/scheme-conditions", json={"text": "   "})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "EMPTY_TEXT"


def test_as5_scheme_conditions_not_configured(client, _empty_ai_config):
    """未配置 key → 400 AI_NOT_CONFIGURED。"""
    r = client.post("/api/ai/scheme-conditions", json={"text": "RSI 大于 80"})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "AI_NOT_CONFIGURED"


# ================================================================
# AS4：上游错误映射
# ================================================================
def test_as4_auth_failed(client, _empty_ai_config, monkeypatch):
    """client.chat 抛 AI_AUTH_FAILED 时 API 透出 401。"""
    from server.core.errors import ApiError

    def _boom(prompt, system="", model=None, temperature=None, scene=None):
        raise ApiError("AI_AUTH_FAILED", "key 无效", status_code=401)

    monkeypatch.setattr(ai_client, "chat", _boom)
    client.put("/api/ai/config", json={"api_key": "sk-bad"})
    r = client.post("/api/ai/diagnosis", json={"item": {"code": "sh600519"}})
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "AI_AUTH_FAILED"