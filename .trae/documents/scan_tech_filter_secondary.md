# 全市场扫描：技术指标筛选改为「二级过滤」（即时、不清扫）

## Context
**现状问题**：初级模式「全市场扫描」的触发定档本就用技术条件（状态分界技术条件定档），而新增的「技术指标筛选」（`scan_tech_filter`）又是一套技术条件，两者同源重复。且当前实现是"后端扫描时逐股过滤"（`market_scan_core._quick_score_core`），每次改筛选都要**全量重扫**才生效，成本高、语义上也与定档抢活干。

**目标**：把技术指标筛选定位为**叠加在触发等级之上的二级过滤**——结构上从"扫描入参"改为"结果态过滤"（即时、不清扫）。典型用法：`站上MA5 = 强势（定档）`，再 `MACD金叉（二级过滤）` 把结果里没有金叉的票排除。用户在结果表上改筛选 → 保存 → 即时看过滤后的列表，无需重扫。

**判定基础**：二次过滤复用与建仓一致的技术条件判定 `UnifiedEntryLogic._evaluate_tech_conditions`（消费 `tech` 字典信号：`macd_status==金叉`、`kdj_signal`、`ma_arrangement`、`adx`、RSI 数值等），保证与定档同为权威判定。当前扫描结果**未保存技术信号快照**，需补齐。

---

## 后端

### 1) `engine/market_scan_core.py`
- **新增模块级 `build_tech_snapshot(tech, market, adx_result)`**：返回纯标量子字典
  ```
  {
    'tech': {k: _scalar(tech[k]) for k in _NUM_IND_LABELS if k in tech and tech[k] is not None
             } 合并信号字段 kdj_signal / macd_status / breakout_signal,
    'market': {'ma_arrangement': market.get('ma_arrangement'), 'volume_price': market.get('volume_price')},
    'adx': {'adx': (adx_result or {}).get('adx', 0)},
  }
  ```
  - `_scalar`：`np.generic→.item()`，否则 `float/str/bool()` 显式转换。**必须显式 cast**（缓存写入用 `json.dump(default=str)` 兜底），否则 numpy 标量会被 stringify，`_eval_indicator` 因 `isinstance(v,str)` 判 None → 条件恒不满足 → 误剔除全部。
  - 键集从 `unified_entry_logic._NUM_IND_LABELS` 派生，避免新增指标时漏同步。
- **结果 dict（L141-155）新增 `tech_snapshot`**，由已算好的 `tech/market/adx_result` 构造。
- **删除 L107-117 扫描时 `scan_filter` 过滤块**（含局部 `from unified_entry_logic import UnifiedEntryLogic` 导入）。过滤逻辑转入结果态。

### 2) `ui/web_api.py`
新增私有方法（放 `_aggregate_sectors` 附近）：
- `_apply_scan_tech_filter(self, results)` → `(filtered, any_skipped)`
  - `markets futures：直接 return (results, False)`（期货结果无快照、从未做该过滤）。用 `task['market_type']=='futures'` 判定。
  - 读 `quant_config.get_scan_tech_filter()`；**空列表短路** return `(results, False)`。
  - 逐股：有 `tech_snapshot` → 用 `UnifiedEntryLogic._evaluate_tech_conditions(snap['tech'], snap['market'], r.get('price',0), snap['adx'], direction='long')` 判定，命中保留；**无快照（旧缓存兼容）→ 保留但不套过滤并打 `scan_filter_skipped=True`**，不静默丢票；exceptions → 保留该股。

统一读路径套用（注意**不原地删 `task['results']`**，派生新列表；过滤插在 `topn` 截断**之前**）：
- `get_scan_progress`：排序后、分数/星级/topn 前，`results_filtered, any_skipped = _apply_scan_tech_filter(results, …)`；后续筛选/分页基于 `results_filtered`；`matched_count=len(filtered)`；返回 `sectors = _aggregate_sectors(results_filtered)`（全量过滤后，非分页）；响应新增 `scan_filter_partial=any_skipped`。
- `refresh_sectors`：内存 task 与缓存两种来源都先过滤再聚合。
- `load_scan_cache`：注入任务前过滤，板块聚合用过滤后列表。
- `get_sector_stocks`（第 4 处板块读路径，点击板块联动）：内存与缓存来源均套过滤后再按板块筛选，保证与过滤后 grid 一致。

---

## 前端 `ui_mockup/index.html`

- **`applyScanTechFilter`（L3378）改为即时二级过滤**：`markConfigDirty(); ok = await ensureConfigSaved(); if ok !== false → await applyScanFilter()`（不复用 `startMarketScan()`；`applyScanFilter()` 走现有 `get_scan_progress`，后端按新配置即时过滤 + 渲染 `renderScanResults`/`renderSectorGrid`）。
- **按钮文案 L1228**：「应用并重扫」→「应用筛选」。注释同步。
- 当响应 `scan_filter_partial` 为真，扫描状态栏追加提示"二级过滤未完全生效（旧缓存缺技术快照），建议重新扫描"。
- 确认 `applyScanFilter()` 不传 `false`（默认归零 offset，避免分页错位）。

---

## 兼容与门控
- **旧缓存（无 `tech_snapshot`）**：保留但不套过滤 + 标记，前端横幅提示；不做"缺快照即剔除"（会静默丢票）。
- **初/高级**：本次改造不引入 mode 门控——与非空 filter 生效的行为与现扫描时过滤对等。初级专属的 `scanFilterEditor` 不变；高级方案残留非空 filter 仍生效（与现状对等，不作回归）。
- 排序/分位：`percentile` 基于全样本不变，保持正确。

---

## 验证
- 后端单测 `tests/test_scan_filter.py` 增补：
  1. 同 filter 下，结果态过滤结果 == 原扫描时过滤结果（回归对等）。
  2. `build_tech_snapshot` 产出全部为原生 JSON 类型（`json.dumps` 不触发 `default`→str 丢失），并含 `kdj_signal/macd_status/ma_arrangement/adx` 可判定字段。
  3. 期货 / 无快照结果绕过过滤。
  4. 过滤后 topn 名额不被"滤除但排在前"的票占用。
  5. 空 filter 短路（无副作用、直接全保留）。
- 浏览器冒烟：启动服务 → 初级全市场扫描 → 在「技术指标筛选」配 e.g. `MACD金叉` → 点「应用筛选」（**不重扫**）→ 结果表即时只剩命中票、板块同步、状态栏无过期提示；再配一个必不命中条件确认无残留票。