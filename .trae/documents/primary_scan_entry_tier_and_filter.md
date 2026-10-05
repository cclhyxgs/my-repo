# 初级全市场扫描：按触发建仓等级展示 + 可配置技术指标筛选

## Summary

针对初级模式的全市场扫描，做两处改动：

1. **展示**：扫描结果不再显示「因子预期分 / 分位 / 星级」，改为按「触发建仓等级」（强势/标准/试探/观望/博反弹/恐慌反转/弱势）**分组 + 表头**展示。
2. **筛选**：扫描页新增一个**复用状态分界 AND 编辑器**的技术指标筛选控件，配置随方案保存（新增 `scan_tech_filter` 配置节点），后端在逐股扫描期间用与建仓一致的 `_evaluate_tech_conditions` 逻辑判定，命不中的股票直接过滤。

用户已确认两个决策：
- 筛选实现方式 = **复用 AND 编辑器 + 后端逐股判定**
- 展示形态 = **分组 + 表头样式**

## Current State Analysis

**后端扫描内核** `engine/market_scan_core.py::_quick_score_core`：
- 逐股调用 `compute_stock_score` 得到 `stock_score, final_result, tech, market, adx_result, tech_strength`，`final_score = final_result['final_score']`。
- 返回 dict 含 `code/name/price/stock_score/final_score/tech_strength/final_rating/stars/level/sector/factor_values`（见 `market_scan_core.py` L111-123）。
- 当前**没有**触发建仓等级字段。

**分类器** `engine/stock_classifier.py::StockClassifier.classify(ctx, direction)`：
- 内部已按 `quant_config.get_usage_mode()=='basic'` 分流（`_score_pass = (lambda t: True) if is_basic else final_score>=t`，见 L95-96），初级纯技术定档、高级因子分+技术定档。
- 返回 `{'type':'strong'|'standard'|'test'|'pending'|'weak_rebound'|'panic_rebound'|'weak', 'type_label': '📈 强势股' 等}`。
- `ctx` 需要字段：`final_score, stock_score, market, tech, latest_price, adx_state, up_ratio`——全部可从 `_quick_score_core` 现成拿到。

**技术条件求值** `engine/unified_entry_logic.py`：
- `_evaluate_tech_conditions(spec, tech, market, latest_price, adx_state, direction='long')`（L403-423）支持 `list of cond` AND 求值。
- `_evaluate_single_cond`（L353-384）支持 `{signal: 英文code}` 与 `{indicator,op,value}` 两种形态。这与 `entry_conditions[level]['tech_signal']` 存储形态一致（经 `_signal_cond_to_config` 转成英文 code）。

**配置映射** `ui/config_mapper.py`：
- `_quant_data_to_config(data)`（L628）写 `entry_conditions`，`_config_to_quant_data(cfg)`（L336）回读 `thresholds[].signal`，均通过 `_signal_cond_to_config` / `_signal_cond_to_front` 做中英码转换（L258-282）。
- `_default_quant_data()`（L37）预填 `thresholds`/`signalOptions` 等默认结构。

**配置读取** `engine/quant_config.py`：
- getter 统一读 `_cache`，如 `get_entry_conditions()`（L1189-1190）：`return _cfg().get('entry_conditions')`。
- 扫描 worker 已通过入口 `load_market_scheme` 将 `_cache` 设为用户选中方案（`_ensure_worker_stock_list` 只 `reload_model_only`，不冲 `_cache`，见 `market_scan_core.py` L134-156）。因此扫描期间新增的 `get_scan_tech_filter()` 能读到正确方案。

**前端** `ui_mockup/index.html`：
- 扫描控制卡 L1195-1242：`scanMinScore/scanMaxScore`（因子预期区间）、`scanTopN`、`scanRating`（评级）、`scanSortSelect`。
- 扫描结果表 L1252：`<thead><tr><th>#</th><th>代码</th><th>名称</th><th>现价</th><th>因子预期</th><th>分位</th><th>评级</th><th>板块</th></tr>`。
- `renderScanResults(results, matchedCount)` L5624-5667 生成行；初级分支已用 `tech_strength` 显示分（L5636-5641）但仍渲染 toFixed/百分比/星级列。
- 已有 `scanTechPanel`（L1228-1234）+ 前端 `techFilterArray`（L5458-5480）按 `factor_values` 过滤（**第二道因子值筛选，保留不动**）。
- 状态分界 AND 编辑器：`renderAndEditor(t,i)`（L3248-3279）渲染 chips + 面板，`addAndSignal/addAndIndicator/removeAndCond/toggleAndPanel/reopenAndPanel`（L3290-3330）；`quantData.thresholds[].signal` 存条件数组，形态 `{signal:中文}` 或 `{indicator,op,value}`。`getSignalOptions()`（L3223）与 `IND_LABELS`（L3231）、`AND_OPS`（L3232）可直接复用。

## Proposed Changes

### 1. 后端：扫描结果附带触发建仓等级（engine/market_scan_core.py）

在 `_quick_score_core` 内，`compute_stock_score` 之后、`return` 之前：

```python
from engine.stock_classifier import StockClassifier
from types import SimpleNamespace
try:
    _ctx = SimpleNamespace(
        final_score=final_score, stock_score=stock_score,
        market=market or {}, tech=tech or {},
        latest_price=closes[-1], adx_state=adx_result or {},
        up_ratio=up_ratio)
    _cls = StockClassifier.classify(_ctx, direction=direction)
    _entry_tier = _cls.get('type', 'weak')
    _entry_tier_label = _cls.get('type_label', _entry_tier)
except Exception:
    _entry_tier, _entry_tier_label = 'weak', '弱势股'
```

在 `return {...}` 中追加两个字段：
```python
'entry_tier': _entry_tier,
'entry_tier_label': _entry_tier_label,
```
> 两模式都计算（成本约一次技术条件求值），但仅初级的展示会用到。direction 原样透传，空单/期货语义由 classifier 内部处理。

### 2. 后端：新增扫描技术指标筛选（可配置 / 逐股判定）

**a. 配置节点 `scan_tech_filter`**

- `ui/config_mapper.py::_quant_data_to_config`：新增
  ```python
  config['scan_tech_filter'] = [_signal_cond_to_config(c) for c in (data.get('scanFilter') or [])]
  ```
  （空列表 = 无筛选；`_signal_cond_to_config` 把中文 signal 转英文 code，数值条件原样保留。）
- `ui/config_mapper.py::_config_to_quant_data`：新增
  ```python
  data['scanFilter'] = [_signal_cond_to_front(c) for c in (cfg.get('scan_tech_filter') or [])]
  ```
- `ui/config_mapper.py::_default_quant_data`：在初始结构中加 `'scanFilter': []`（供默认无筛选）。

**b. 读取 getter**

- `engine/quant_config.py`：在 `get_entry_conditions()` 附近新增：
  ```python
  def get_scan_tech_filter():
      """返回当前方案的扫描技术指标筛选条件列表（英文 code / 数值条件）。空=不筛选。"""
      v = _cfg().get('scan_tech_filter') or []
      return v if isinstance(v, list) else []
  ```

**c. 逐股判定过滤**

在 `_quick_score_core` 计算并附上 `entry_tier` 之后、`return result` 之前，读筛选并判定，不满足就 `return None` 跳过该股：

```python
_scan_filter = quant_config.get_scan_tech_filter()
if isinstance(_scan_filter, list) and _scan_filter:
    from engine.unified_entry_logic import UnifiedEntryLogic
    if not UnifiedEntryLogic._evaluate_tech_conditions(
            _scan_filter, tech, market, closes[-1], adx_result, direction=direction):
        return None   # 命不中用户配置的筛选条件，滤除
```
> `_quick_score_core` 仅被全市场扫描 worker 使用，此行为不影响个股分析/诊断。
> 空单/期货下 `direction` 透传，与建仓判定同语义。

### 3. 前端：扫描控制区（ui_mockup/index.html）

**a. 初级隐藏「因子预期/评级」控件（体现"没有因子预期分和星级"）**
- 加模式门控：给 `scanMinScore~scanMaxScore` 控件容器与 `scanRating` 加 `.gated-factor`（初级隐藏），或复用现有 `applyUsageGating` 门控数组。保留 `scanTopN`、`scanSortSelect`（初级排序已映射 `tech_strength`，见 `_scanSortField` L5505）。
- 扫描控制卡新增一行「技术指标筛选（触发等级）」，放置 AND 编辑器容器 `<div id="scanFilterEditor">` + `应用并重扫` 按钮。

**b. 结果表头按模式切换**
- `renderScanResults` 开头根据 `window.isBasic()`：
  - 高级：维持当前 `<thead>`（# 代码 名称 现价 因子预期 分位 评级 板块）。
  - 初级：改为 `# 代码 名称 现价 触发建仓 板块`（colspan=6，无因子/分位/星级）。

### 4. 前端：AND 筛选编辑器复用（ui_mockup/index.html）

- `quantData` 默认对象（L2826 附近）加 `scanFilter: []`；`normalizeDescs`/load 后兜底 `quantData.scanFilter = quantData.scanFilter || []`。
- 复用现有编辑器渲染：将扫描筛选绑定到下述目标解析，最小化改动现有函数：
  - 新增 `_andTarget(key)`：`key==='scan_filter' ? quantData.scanFilter : (quantData.thresholds.find(t=>t.key===key)||{}).signal`。
  - 把 `addAndSignal/addAndIndicator/removeAndCond`（L3290-3330）内对 `quantData.thresholds.find(...)` 的数组写入，统一改为经 `_andTarget` 取数组，从而状态分界与扫描筛选共用一套编辑器逻辑，`renderAndEditor` 无需改。
  - 新增 `renderScanFilterEditor()`：`scanFilterEditor.innerHTML = renderAndEditor({key:'scan_filter', signal: quantData.scanFilter}, 0)`，渲染 chips + "+ AND条件" 面板（中文信号勾选 + 数值指标条，复用 `getSignalOptions/IND_LABELS/AND_OPS`）。
- 「应用并重扫」按钮：写入 `quantData.scanFilter` → 调用后端保存当前方案配置（复用现有保存链路，`_quant_data_to_config` 持久化 `scan_tech_filter`）→ 触发 `startMarketScan()` 全量重扫（后端逐股按新筛选过滤）。可加 `markConfigDirty()` 提示。

### 5. 前端：结果按触发等级分组展示（ui_mockup/index.html）

- `renderScanResults` 初级分支调整渲染：
  - 固定分组顺序：`strong → standard → test → pending → weak_rebound → panic_rebound → weak`。
  - 对 `results` 按 `r.entry_tier` 分组，每组先输出一行组头 `<tr class="scan-tier-head"><td colspan="6">📈 强势股（N 只）…</td></tr>`，再接该组行；组头用 `r.entry_tier_label`（含 emoji）为标准。
  - 组内行去掉因子/分位/星级列，触发建仓列直接以等级配色标签展示（或用组头已表达，行内给出代码/名称/现价/板块即可）。
  - 匹配统计行（`matchedCount`）colspan 同步改为 6。
- 顶级管理保持不涉及；板块强度副标签页不受影响。

### 6. 测试（tests）

- `tests/test_scan_filter.py`（新增，纯单测，不联网）：
  - `test_scan_filter_config_roundtrip`：构造含 `scanFilter` 的 data，`_quant_data_to_config` → `_config_to_quant_data` 往返，验证 `scan_tech_filter` 中英码转换与空列表默认。
  - `test_scan_filter_evaluate`：用 `UnifiedEntryLogic._evaluate_tech_conditions` 对手工构造的 `tech`（如 `{'rsi_14': 3, 'ma_arrangement': 2, ...}`）+ 筛选列表（`[{indicator:'rsi_14',op:'<',value:40}]`）断言 命中/未命中。
  - `test_scan_filter_indicator_missing_blocks`：指标缺失时 AND 判 False（防误放行）。
- 在 `tests/run_all.py` 中注册 `test_scan_filter.py`，确认全量通过。

## Assumptions & Decisions

- 筛选配置**随方案保存**（新增 `scan_tech_filter` 节点），与状态分界技术门槛一致；空列表 = 不做筛选。
- 后端逐股判定复用 `_evaluate_tech_conditions`，与建仓门槛**同一实现**，保证筛出股票与"会触发该档 + 满足筛选"严格一致，不引入第二套语义。
- 触发等级字段两模式都计算，仅初级展示使用；高级模式展示逻辑不变。
- 已有「第二道因子值筛选」`scanTechPanel` 保留不动，与本次新增的"触发等级 AND 筛选"相互独立、可叠加。
- 应用筛选会触发一次全量重扫（后端逐股过滤，无法在缓存上直接改），这是该实现方式的固有代价，已在方案中明确。

## Verification

1. 运行 `python tests/run_all.py`，全部通过（新增扫描筛选用例）。
2. 前端冒烟（初/高级模式切换）：
   - 初级：扫描结果表无「因子预期/分位/评级」列，按 强势/标准/试探/观望/博反弹/恐慌反转/弱势 分组 + 组头展示。
   - 初级：点「+ AND条件」配置 `RSI14 < 40` → 应用并重扫 → 仅保留命中的等级与股票；未配置时显示全部。
   - 高级：列与分组显示维持原状，"因子预期/评级"控件恢复可见。
3. 方案切换后在扫描页确认筛选配置随方案加载（`scanFilter` 回读正确）。