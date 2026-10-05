# -*- coding: utf-8 -*-
"""全市场扫描适配层（F-501~506，能力 3+11）。

职责边界（B40，与诊断同构）：本层只做「启动 + 缓存 + 聚合 + 导出」的编排，
逐只评分走 `engine.market_scan_core`（经 `engine_bridge` 薄访问器收口，T8 唯一 import 点）。
worker 在 `server/worker/executor.py::_run_scan` 实现，复用 F-105 任务队列底座。

验收来源（feature-matrix.md §一 F-501~506 行）：
- F-501：POST /api/scan {market} → {task_id}；分钟级 period 被 4xx 拒绝；订阅制不扣次（仅准入校验）。
- F-503：结果分页（offset/limit）剥离 `tech_snapshot`；rating 过滤（⭐≥N）。
- F-504：缓存 24h 内 stale=false 可复用；>24h stale=true 需重扫且不报 very_stale。
- F-505：板块聚合仅含 ≥3 成分股板块；强/中/弱按 30/20/10 阈值；用户映射 > 内置（由 bridge 保证）。
- F-506：导出 utf-8-sig CSV（EF BB BF）；暂停/继续/取消=任务状态机。
"""

import csv
import io
import json
import os
from typing import Optional

from server.adapters import engine_bridge, usage
from server.core import time as srv_time
from server.core.errors import ApiError
from server.core import task_store
from server.worker.executor import get_executor

VALID_MARKETS = ("stock", "futures")
# 扫描仅支持日/周级（F-502 验收样本 = 300 根日K；分钟级源站配额不可行 → 4xx）
_SCAN_SUPPORTED_KTYPES = ("日K", "周K")

# 板块强度阈值（ui/web_api.py:3096 逐字沿用：强≥30 / 中≥20 / 弱≥10 / 极弱）
_SECTOR_STRONG, _SECTOR_MID, _SECTOR_WEAK = 30, 20, 10

# F-504 缓存新鲜窗口（秒）：24h
_CACHE_TTL_SECONDS = 24 * 3600


# ------------------------------------------------------------
# 缓存（F-504）
# ------------------------------------------------------------

def _cache_path(market: str) -> str:
    base = os.path.join(engine_bridge._ensure_quant_system_dir(), "scan_cache")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{market}.json")


def get_scan_cache(market: str) -> dict:
    """读取扫描缓存。

    返回 {exists, stale, saved_at, results}。
    stale = 距 saved_at > 24h。恒不返回 very_stale（仅两态）。
    """
    path = _cache_path(market)
    if not os.path.exists(path):
        return {"exists": False, "stale": True, "saved_at": None, "results": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        saved_at = data.get("saved_at")
        age = (srv_time.now() - _parse_iso(saved_at)).total_seconds() if saved_at else 1e12
        stale = age > _CACHE_TTL_SECONDS
        return {
            "exists": True,
            "stale": stale,
            "saved_at": saved_at,
            "results": data.get("results", []),
        }
    except Exception:
        return {"exists": False, "stale": True, "saved_at": None, "results": []}


def save_scan_cache(market: str, results: list) -> None:
    """落盘扫描结果 + saved_at（覆盖写；单副本无并发覆盖风险，R-09 外置后换 DB）。"""
    path = _cache_path(market)
    payload = {"market": market, "saved_at": srv_time.now_iso(), "results": results}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _parse_iso(s):
    if not s:
        return None
    try:
        from datetime import datetime, timezone
        # srv_time.now_iso() 返回带时区 'Z' 或偏移；兼容解析
        s2 = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s2)
    except Exception:
        return None


# ------------------------------------------------------------
# 启动（F-501）
# ------------------------------------------------------------

def start_scan(
    market: str = "stock",
    direction: Optional[str] = None,
    k_type: str = "日K",
    scheme_name: Optional[str] = None,
    use_cache: bool = True,
) -> dict:
    """启动一次全市场扫描，返回 {task_id, reused}。

    - market 校验（stock/futures）。
    - k_type 仅限日/周级（分钟级 4xx）。
    - 授权强制（F-1404）已迁移到端点依赖 `require_license`（账号维度，不扣次）。
    - use_cache 且缓存新鲜 → 直接复用已完成任务（stale=false），不真扫。
    """
    market = (market or "stock").strip().lower()
    if market not in VALID_MARKETS:
        raise ApiError("INVALID_MARKET", f"未知 market：{market}（可选 {'/'.join(VALID_MARKETS)}）", status_code=422)

    k_type = (k_type or "日K").strip()
    if k_type not in _SCAN_SUPPORTED_KTYPES:
        raise ApiError("INVALID_KTYPE", f"扫描不支持 {k_type}（仅支持 {'/'.join(_SCAN_SUPPORTED_KTYPES)}）", status_code=422)

    if direction not in (None, "long", "short"):
        raise ApiError("INVALID_DIRECTION", f"未知 direction：{direction}", status_code=422)

    # 缓存复用（F-504 新鲜态）
    if use_cache:
        cache = get_scan_cache(market)
        if cache["exists"] and not cache["stale"]:
            return {"task_id": _spawn_reused_task(market, cache["results"]), "reused": True}

    task_id = get_executor().submit(
        "scan",
        {
            "market": market,
            "direction": direction,
            "k_type": k_type,
            "scheme_name": scheme_name,
            "up_ratio": 0.5,
        },
    )
    return {"task_id": task_id, "reused": False}


def _spawn_reused_task(market: str, results: list) -> str:
    """从缓存结果直接生成一个已完成任务（stale=false 复用路径，不真扫）。"""
    task_id = task_store.store.create("scan", {"market": market, "reused": True})
    total = len(results)
    task_store.store.set_result(task_id, results=results, total_stocks=total)
    task_store.store.update_progress(task_id, total, total, "缓存复用（24h 内）")
    task_store.store.append_event(task_id, "done", {
        "task_id": task_id, "status": "completed",
        "current": total, "total": total, "pct": 100.0,
        "message": "缓存复用", "ts": srv_time.now_iso(),
    })
    task_store.store.set_status(task_id, "completed", "缓存复用")
    return task_id


# ------------------------------------------------------------
# 结果分页（F-503）
# ------------------------------------------------------------

# 星级过滤（rating=N → 星数≥N）。
# 星号字符集必须同时收录两种：engine/signal_rating.py::assign_tech_stars 实际产出
# emoji 星 "⭐"*tier（U+2B50），而早期测试夹具/历史数据写作 "★"（U+2605）。
# 只认其中一种会让另一类数据的星数恒为 0 → rating≥N 过滤把结果全部清空。
_STAR_CHARS = ("★", "⭐")


def read_results(task_id: str, offset: int = 0, limit: int = 200,
                 rating: Optional[int] = None, sort: str = "final_score",
                 order: str = "desc") -> dict:
    """读取扫描结果（分页 + 筛选 + 排序），剥离 tech_snapshot 防响应膨胀（F-503）。"""
    task = task_store.store.get(task_id)
    if task is None:
        raise ApiError("TASK_NOT_FOUND", f"任务不存在：{task_id}", status_code=404)
    results = list(task.get("results") or [])
    total = len(results)

    # 星级过滤（rating=N → 星数≥N；stars 形如 '⭐⭐⭐' / '★★★' / ''）
    if rating is not None:
        results = [r for r in results if _star_count(r.get("stars", "")) >= rating]

    # 排序（final_score / tech_strength / price）
    reverse = order != "asc"
    results.sort(key=lambda r: _sort_key(r, sort), reverse=reverse)

    page = results[offset: offset + limit] if limit else results[offset:]
    # 剥离 tech_snapshot（F-503 硬要求）
    cleaned = [_strip_snapshot(r) for r in page]
    return {
        "task_id": task_id,
        "total": total,
        "filtered_total": len(results),
        "offset": offset,
        "limit": limit,
        "items": cleaned,
    }


def _star_count(stars: str) -> int:
    if not stars:
        return 0
    return sum(1 for ch in str(stars) if ch in _STAR_CHARS)


def _sort_key(r: dict, sort: str):
    if sort == "tech_strength":
        return float(r.get("tech_strength") or 0)
    if sort == "price":
        return float(r.get("price") or 0)
    # 默认综合分排序：初级（因子休眠）下因子预期分无意义 → 按技术强度×100 驱动
    # （F-604 双端一致；镜像桌面 `_scan_rank`：basic→tech_strength*100 / advanced→final_score）。
    if usage.is_basic():
        return float(r.get("tech_strength") or 0) * 100
    return float(r.get("final_score") or 0)


def _strip_snapshot(r: dict) -> dict:
    """返回去除 tech_snapshot 的副本（F-503 响应不含该字段）。"""
    out = dict(r)
    out.pop("tech_snapshot", None)
    return out


# ------------------------------------------------------------
# 板块聚合（F-505）
# ------------------------------------------------------------

def aggregate_sectors(results: list) -> list:
    """按板块聚合（ui/web_api.py:3078 _aggregate_sectors 服务端镜像）。

    仅含 ≥3 成分股板块；avg 排名分按 30/20/10 判强/中/弱/极弱；含触发建仓等级计数。
    排名分默认用 final_score（高级口径；初级 gating 属 F-604，未做时统一此口径）。
    """
    stats = {}
    for r in results:
        sector = r.get("sector") or "未分类"
        if not sector or sector == "未分类":
            continue
        if sector not in stats:
            stats[sector] = {"scores": [], "stocks": []}
        stats[sector]["scores"].append(float(r.get("final_score") or 0))
        stats[sector]["stocks"].append(r)

    sectors = []
    for sector, data in stats.items():
        scores = data["scores"]
        if len(scores) < 3:
            continue
        avg = sum(scores) / len(scores)
        sorted_stocks = sorted(data["stocks"], key=lambda x: float(x.get("final_score") or 0), reverse=True)
        level = ("强" if avg >= _SECTOR_STRONG else
                 "中" if avg >= _SECTOR_MID else
                 "弱" if avg >= _SECTOR_WEAK else "极弱")
        tier_counts = {"strong": 0, "standard": 0, "test": 0, "pending": 0,
                       "weak_rebound": 0, "panic_rebound": 0, "weak": 0}
        for _r in data["stocks"]:
            _t = _r.get("entry_tier")
            tier_counts[_t if _t in tier_counts else "weak"] += 1
        sectors.append({
            "sector": sector,
            "avg_score": round(avg, 1),
            "count": len(scores),
            "strong_count": sum(1 for s in scores if s >= _SECTOR_STRONG),
            "level": level,
            "tier_counts": tier_counts,
            "max_stock": _strip_snapshot(sorted_stocks[0]),
            "min_stock": _strip_snapshot(sorted_stocks[-1]),
        })
    sectors.sort(key=lambda x: x["avg_score"], reverse=True)
    return sectors


# ------------------------------------------------------------
# CSV 导出（F-506）
# ------------------------------------------------------------

_CSV_COLUMNS = [
    ("code", "代码"), ("name", "名称"), ("price", "现价"),
    ("stock_score", "技术分"), ("final_score", "综合分"),
    ("stars", "星级"), ("level", "等级"), ("sector", "板块"),
    ("entry_tier_label", "触发档位"),
]


def build_scan_csv(results: list) -> bytes:
    """生成 utf-8-sig CSV（首字节 EF BB BF，Excel 不乱码，F-506 硬要求）。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([label for _, label in _CSV_COLUMNS])
    for r in results:
        row = []
        for key, _ in _CSV_COLUMNS:
            v = r.get(key, "")
            row.append("" if v is None else v)
        writer.writerow(row)
    content = buf.getvalue()
    # utf-8-sig：BOM（EF BB BF）+ utf-8 正文
    return ("\ufeff" + content).encode("utf-8")


def export_filename(market: str) -> str:
    ts = srv_time.now().strftime("%Y%m%d_%H%M%S")
    return f"全市场扫描_{market}_{ts}.csv"
