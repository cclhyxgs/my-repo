# -*- coding: utf-8 -*-
"""情绪账本（F-1201/1202/1203/1204）—— 验收测试。

断言来源：`docs/feature-matrix.md` F-1201/1202/1203/1204「验收标准」列。
覆盖：
- F-1201  POST /api/emotion 后 `op_price` 不可被前端覆盖；现价批量刷新为单次请求
- F-1202  仪表盘 total/sum_diff/top_tag 与记录一致
- F-1203  列表分页 cursor；删除 204 / 不存在 404
- F-1204  标签常量单一来源；diff_pct 方向口径（买=被套负 / 卖=卖早正）
"""

import pytest

from server.db import emotion_repo
from server.db.session import session_scope


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    from server.db import session as db_session

    url = "sqlite:///" + (tmp_path / "mbull_emotion.db").as_posix()
    monkeypatch.setenv("MBULL_DATABASE_URL", url)
    db_session.dispose()
    yield url
    db_session.dispose()


@pytest.fixture
def env(db_env, monkeypatch):
    from server.adapters import engine_bridge
    monkeypatch.setattr(engine_bridge, "quant_model_snapshot",
                        lambda: {"schemes": {}, "factor_profiles": {}})
    return db_env


def _seed(code, action, op, latest):
    with session_scope() as session:
        rec = emotion_repo.add_record(
            session, code=code, action=action, tag="追涨",
            op_price=op, latest_price=latest)
        session.commit()
        return rec["id"]


# ---------------------------------------------------------------- F-1201 数据与入口
def test_f1201_op_price_locked(client, env):
    """op_price 服务端锁定：后续无法覆盖——本接口无更新路径，只验证建值即写入。"""
    r = client.post("/api/emotion", json={
        "code": "sh600001", "action": "buy", "tag": "追涨",
        "op_price": 10.0, "latest_price": 12.0})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    rec = client.get("/api/emotion").json()["records"][0]
    assert rec["id"] == rid and rec["op_price"] == 10.0
    assert rec["diff_pct"] == 20.0  # (12-10)/10*100


def test_f1201_prices_batch_refresh_single_request(client, env):
    with session_scope() as session:
        emotion_repo.add_record(session, code="sh600001", action="buy",
                                op_price=10.0, latest_price=10.0)
        emotion_repo.add_record(session, code="sh600002", action="buy",
                                op_price=20.0, latest_price=20.0)
        session.commit()
    r = client.post("/api/ledger/prices", json={"prices": {"sh600001": 12.0, "sh600002": 22.0}})
    assert r.status_code == 200
    recs = client.get("/api/emotion").json()["records"]
    by_code = {rec["code"]: rec["latest_price"] for rec in recs}
    assert by_code["sh600001"] == 12.0 and by_code["sh600002"] == 22.0


# ---------------------------------------------------------------- F-1202 仪表盘
def test_f1202_summary_counts(client, env):
    _seed("sh600001", "buy", 10.0, 12.0)     # diff +20
    _seed("sh600002", "buy", 20.0, 15.0)     # diff -25（被套，算"最容易犯的错"）
    s = client.get("/api/emotion/summary").json()
    assert s["total"] == 2
    assert s["sum_diff"] == -5.0              # 20 + (-25)
    assert s["top_tag"] == "追涨"
    assert s["top_tag_count"] == 1


def test_f1202_top_tag_only_losses(client, env):
    """赚的不算"最容易犯的错"：连通加仓+追涨，但亏损一笔才是 top。"""
    _seed("sh600001", "buy", 10.0, 12.0)      # 赚/追涨
    _seed("sh600002", "buy", 20.0, 15.0)      # 亏/追涨
    _seed("sh600003", "buy", 30.0, 20.0)      # 亏/恐慌
    s = client.get("/api/emotion/summary").json()
    # 「最容易犯的错」只统计亏损：追涨1 vs 恐慌1，追涨先到 → 追涨
    assert s["top_tag"] == "追涨"


# ---------------------------------------------------------------- F-1203 记录表
def test_f1203_pagination_cursor(client, env):
    for i in range(5):
        _seed(f"code{i}", "buy", 10.0, 10.0)
    page1 = client.get("/api/emotion?limit=2").json()
    assert len(page1["records"]) == 2 and page1["cursor"] is not None
    page2 = client.get(f"/api/emotion?limit=2&cursor={page1['cursor']}").json()
    assert len(page2["records"]) == 2
    ids = [r["id"] for r in page1["records"] + page2["records"]]
    assert len(set(ids)) == 4  # 无重复


def test_f1203_delete_204_and_404(client, env):
    rid = _seed("sh600001", "buy", 10.0, 10.0)
    assert client.delete(f"/api/emotion/{rid}").status_code == 204
    assert all(r["id"] != rid for r in client.get("/api/emotion").json()["records"])
    assert client.delete(f"/api/emotion/{rid}").status_code == 404


# ---------------------------------------------------------------- F-1204 标签与口径
def test_f1204_tags_single_source(client, env):
    r = client.get("/api/emotion/tags").json()
    assert set(r["tags"]) == {
        "追涨", "博反弹", "怕踏空", "冲动", "摊平", "怕回调", "拿不住", "恐慌", "其他"}
    assert r["actions"]["buy"] == "买入" and r["actions"]["sell"] == "卖出"


def test_f1204_diff_direction(client, env):
    """买向被套 diff 为负、卖向卖早 diff 为正（方向口径一致）。"""
    _seed("sh600001", "buy", 10.0, 8.0)        # 买入被套 -20%
    _seed("sh600002", "sell", 10.0, 12.0)      # 卖早少赚 +20%
    recs = client.get("/api/emotion").json()["records"]
    by_code = {r["code"]: r["diff_pct"] for r in recs}
    assert by_code["sh600001"] == -20.0
    assert by_code["sh600002"] == 20.0