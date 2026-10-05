# -*- coding: utf-8 -*-
"""F-401 诊断启动 验收测试。

验收（feature-matrix.md:27）：
  ① `POST /api/diagnosis {text,market,direction,scheme_name}` 返回 `task_id`
  ② 同账号并发不同 market 不串市场
"""

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from server.core import task_store

ROOT = Path(__file__).resolve().parents[2]
REAL_POOL = ROOT / "cache" / "stock_list.json"


@pytest.fixture(scope="module", autouse=True)
def seeded_stock_pool():
    """拷真实股票池到隔离目录（缓存优先，刷新 saved_at），使股票解析离线确定。"""
    assert REAL_POOL.is_file(), f"缺股票池 {REAL_POOL}"
    cache_dir = Path(os.environ["QUANT_SYSTEM_DIR"]) / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    data = json.loads(REAL_POOL.read_text(encoding="utf-8"))
    data["saved_at"] = datetime.now().isoformat()
    (cache_dir / "stock_list.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return len(data.get("name_to_code") or {})


def _submit(client, text, market="futures", direction=None):
    return client.post(
        "/api/diagnosis",
        json={"text": text, "market": market, "direction": direction},
    )


def _payload(client, text, market="futures", direction=None):
    r = _submit(client, text, market=market, direction=direction)
    assert r.status_code == 200, r.text
    return task_store.store.get(r.json()["task_id"])["payload"]


def test_dg1_submit_returns_task_id(client):
    """验收①：提交诊断返回 task_id。"""
    r = _submit(client, "rb\njd2609", market="futures")
    assert r.status_code == 200, r.text
    assert r.json()["task_id"]


def test_dg2_empty_text_422(client):
    """空 text → 422 INVALID_TEXT。"""
    r = _submit(client, "", market="futures")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_TEXT"


def test_dg3_direction_filter(client):
    """direction=short 只保留 short 标的（第 4 字段 short）。"""
    payload = _payload(client, "rb,0,0,short\njd2609", market="futures", direction="short")
    assert len(payload["items"]) == 1
    assert payload["items"][0]["code"] == "rb"


def test_dg4_market_not_shared(client):
    """验收②：并发不同 market 不串市场（payload.market 各自隔离）。"""
    r1 = _submit(client, "rb", market="futures")
    r2 = _submit(client, "sh600519", market="stock")
    assert r1.status_code == 200 and r2.status_code == 200
    p1 = task_store.store.get(r1.json()["task_id"])["payload"]
    p2 = task_store.store.get(r2.json()["task_id"])["payload"]
    assert p1["market"] == "futures"
    assert p2["market"] == "stock"
    assert p1["items"][0]["code"] == "rb"
    assert p2["items"][0]["code"] == "sh600519"


def test_dg5_parse_stock(client):
    """股票解析：sh600519 / 贵州茅台 → 均为 sh600519（名称与完整代码两条路径）。"""
    payload = _payload(client, "sh600519\n贵州茅台", market="stock")
    assert len(payload["items"]) == 2
    assert all(it["code"] == "sh600519" for it in payload["items"])


def test_dg6_unknown_code_tolerated(client):
    """未知品种容错：不可识别行 code=query 兜底（桌面同口径）。"""
    payload = _payload(client, "zzz9", market="futures")
    assert len(payload["items"]) == 1
    assert payload["items"][0]["code"] == "zzz9"
