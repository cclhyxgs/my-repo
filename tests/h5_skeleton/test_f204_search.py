# -*- coding: utf-8 -*-
"""F-204 股票搜索 —— 验收测试。

断言来源（纪律 4）：
  feature-matrix.md:21 F-204「验收标准」列 =
    `GET /api/search?q=茅台` 返回 ≤20 条且含 `sh600519`
  业务规则列 = 名称/代码匹配；上限 20 条
  输出契约 = app-architecture.md:326 `?q=` → `[{code, name}]` ≤20 条（**裸数组**）

用户已拍板（2026-09-12）：
  B12 = 服务端搜索（**不**下发全量码表）
  B13 = **支持**裸 6 位数字代码匹配（桌面只支持 sh/sz 前缀完整代码）
  B14 = 期货搜索**不纳入**本条
"""

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REAL_POOL = ROOT / "cache" / "stock_list.json"


@pytest.fixture(scope="module", autouse=True)
def seeded_stock_pool():
    """把项目真实股票池拷进测试隔离目录，使搜索测试**离线且确定**。

    engine 的 `StockListLoader.load()` 是「缓存优先」：缓存条数达标且 `saved_at` ≤1 天
    就直接复用、**不发网络请求**。故拷入时刷新 `saved_at`，让测试与运行日期解耦。
    （池文件与池内容是真实的，只刷新时间戳。）
    """
    assert REAL_POOL.is_file(), f"缺股票池文件 {REAL_POOL}，无法离线验证 F-204"
    cache_dir = Path(os.environ["QUANT_SYSTEM_DIR"]) / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    data = json.loads(REAL_POOL.read_text(encoding="utf-8"))
    data["saved_at"] = datetime.now().isoformat()
    (cache_dir / "stock_list.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    return len(data.get("name_to_code") or {})


# ---------------------------------------------------------------- FS1（验收逐字）
def test_fs1_search_maotai_returns_le20_and_contains_sh600519(client):
    """FS1（验收标准逐字）：`?q=茅台` → ≤20 条且含 `sh600519`。"""
    r = client.get("/api/search", params={"q": "茅台"})
    assert r.status_code == 200, r.text
    items = r.json()

    assert isinstance(items, list), f"响应应为数组（§3.2 裸数组），实际 {type(items).__name__}"
    assert len(items) <= 20, f"上限 20 条，实际 {len(items)} 条"
    codes = [it["code"] for it in items]
    assert "sh600519" in codes, f"未命中 sh600519，实际 {codes}"
    hit = next(it for it in items if it["code"] == "sh600519")
    assert hit["name"] == "贵州茅台", f"名称应为「贵州茅台」，实际 {hit['name']!r}"


# ---------------------------------------------------------------- FS2 代码路径
def test_fs2_full_code_query_hits(client):
    """FS2（业务规则「代码匹配」）：`?q=sh600519` → 命中贵州茅台。"""
    items = client.get("/api/search", params={"q": "sh600519"}).json()
    assert any(it["code"] == "sh600519" for it in items), items
    assert len(items) <= 20


def test_fs8_bare_digit_code_prefix_hits(client):
    """FS8（B13 已拍板支持）：裸 6 位数字 `?q=600519` → 命中 sh600519。"""
    items = client.get("/api/search", params={"q": "600519"}).json()
    assert any(it["code"] == "sh600519" for it in items), items
    assert len(items) <= 20


def test_fs8b_bare_digit_prefix_is_prefix_not_substring(client):
    """FS8 边界：裸数字按**前缀**匹配（`600` 命中 600xxx 一簇），不误命中非前缀。"""
    items = client.get("/api/search", params={"q": "600"}).json()
    assert items, "600 前缀应有命中"
    for it in items:
        assert it["code"].replace("sh", "").replace("sz", "").startswith("600"), it


# ---------------------------------------------------------------- FS3 契约
def test_fs3_item_keys_strictly_code_and_name(client):
    """FS3（§3.2 契约）：元素只含 `code`/`name` 两键，不多不漏。"""
    items = client.get("/api/search", params={"q": "茅台"}).json()
    assert items
    for it in items:
        assert set(it) == {"code", "name"}, f"元素键不符合契约：{it}"
        assert isinstance(it["code"], str) and isinstance(it["name"], str)


# ---------------------------------------------------------------- FS4 / FS5 空与无匹配
@pytest.mark.parametrize("q", ["", "   "])
def test_fs4_empty_query_returns_empty_list(client, q):
    """FS4：空 / 全空白查询 → 200 `[]`（桌面同口径，返回 [])。"""
    r = client.get("/api/search", params={"q": q})
    assert r.status_code == 200, r.text
    assert r.json() == []


def test_fs5_no_match_returns_empty_list_not_404(client):
    """FS5：无匹配 → 200 `[]` —— 搜索无结果不是错误，不得 404。"""
    r = client.get("/api/search", params={"q": "zzz不存在的品种zzz"})
    assert r.status_code == 200, r.text
    assert r.json() == []


# ---------------------------------------------------------------- FS6 上限
def test_fs6_broad_query_capped_at_20(client):
    """FS6（业务规则「上限 20 条」）：宽泛词命中数必须被截到 20。"""
    items = client.get("/api/search", params={"q": "中国"}).json()
    assert len(items) == 20, f"宽泛词应被截到 20 条，实际 {len(items)}"


# ---------------------------------------------------------------- FS7 池
def test_fs7_pool_loaded_with_real_data(seeded_stock_pool, client):
    """FS7：池可用 —— 真实池条数 >5000 且含 sh600519（不硬断言 5218，池会增长）。"""
    assert seeded_stock_pool > 5000, f"池条数异常：{seeded_stock_pool}"
    items = client.get("/api/search", params={"q": "sh600519"}).json()
    assert items, "池已加载却查不到 sh600519"


def test_fs9_futures_not_searched(client):
    """FS9（B14 已拍板）：期货搜索不纳入本条 —— `rb` 不应命中股票池结果。"""
    items = client.get("/api/search", params={"q": "rb"}).json()
    assert all(it["code"].startswith(("sh", "sz", "bj")) for it in items), (
        f"不应返回期货合约：{items}"
    )
