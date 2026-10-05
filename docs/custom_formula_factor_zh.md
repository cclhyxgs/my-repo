# 自定义公式因子（全中文低代码编辑器）落地方案

> 目标：用户不写代码，用全中文公式（通达信风格子集）自己定义指标，命名后作为因子参与打分/扫描/回测/IC 回测。
> 定位：公式是"情绪账本"里把纪律写进去的入口，不是终点——输出必须进入因子体系，不是给人看的信号。

## 0. 现状核实（方案依据）

| 项 | 现状 | 位置 |
|---|---|---|
| 因子注册表 | `REGISTRY` 为 dict，`FactorDef(name/label/category/calc_fn/params_schema/default_weight/default_direction/default_stats/ic/desc)` | engine/factor_registry.py:37,284 |
| 因子签名 | `calc_fn(ctx, params) -> float`，ctx = closes/highs/lows/volumes/opens/data_list/latest_price/tech/market | factor_registry.py:63 |
| 打分入口 | `calc_all_active_factors(active_factors, ctx, factor_configs)` 遍历 active_factors → `get_factor(name)` → calc_fn | factor_registry.py:679 |
| 配置结构 | `active_factors: list` + `factor_configs: {name: {weight,direction,params,stats}}` + `score_scale` | engine/quant_config.py:197 |
| 配置写盘 | `save_scheme_config(name, config)` / `save_current_scheme(config)` | quant_config.py:485,789 |
| 现有算子 | indicators.py 有封装类：MATechnical(calc_sma/calc_ema)、RSI、MACD、KDJ、BOLL、ATR、ADX、OBV 等；**缺 LLV/HHV/REF/SUM/STD/CROSS 基础原语** | engine/indicators.py |
| 前端 | Vue 3 + Vite（web/src/App.vue），PyWebView JS API 桥（ui/web_api.py），ECharts 画图 | web/index.html |

关键结论：
- 自定义因子只要动态注册进 `REGISTRY`，打分/扫描/回测/IC 全链路自动生效，**无需改打分循环**。
- `indicators.py` 缺基础序列原语，需在新模块内用 numpy 实现（均为 O(n) 滑动窗口）。

## 1. 公式语言规范（全中文）

### 1.1 语法（通达信风格子集）

```
变量名 := 表达式;        # 变量赋值（中间变量）
输出 := 表达式;          # 显式声明输出（可选，默认最后一条语句为输出）
# 注释用井号
```

- 语句以 `;` 结尾，支持多行。
- 输出可以是：连续数值序列（因子值，参与 z-score 归一化）或布尔条件（0/1 条件因子）。
- 表达式支持：算术 `+ - * /`、比较 `> < >= <= =`（`==` 兼容）、逻辑 `且 或 非`（`AND OR NOT` 兼容）。

### 1.2 原始数据（中文名，英文兼容）

| 中文名 | 英文兼容 | 说明 |
|---|---|---|
| 收盘价 | CLOSE | |
| 最高价 | HIGH | |
| 最低价 | LOW | |
| 开盘价 | OPEN | |
| 成交量 | VOL | |

### 1.3 算子库（全中文）

| 中文名 | 签名 | 说明 | 实现来源 |
|---|---|---|---|
| 均线 | `均线(序列, N)` | N 日均线 | 复用 `MATechnical.calc_sma` |
| 指数均线 | `指数均线(序列, N)` | N 日 EMA | 复用 `MATechnical.calc_ema` |
| 平滑 | `平滑(序列, N, M)` | SMA(N,M) Wilder 递推 | 新增（numpy） |
| 最低N日 | `最低N日(序列, N)` | N 日最低值（滚动 min） | 新增（numpy） |
| 最高N日 | `最高N日(序列, N)` | N 日最高值（滚动 max） | 新增（numpy） |
| 前值 | `前值(序列, N)` | N 日前值 | 新增（numpy 切片） |
| 求和 | `求和(序列, N)` | N 日滚动求和 | 新增（numpy） |
| 标准差 | `标准差(序列, N)` | N 日滚动标准差 | 新增（numpy） |
| 上穿 | `上穿(序列A, 序列B)` | A 从下方穿过 B（布尔序列） | 新增（numpy） |
| 绝对值 | `绝对值(序列)` | | 新增（np.abs） |
| 最大 | `最大(序列A, 序列B或数)` | 逐点取大 | 新增（np.maximum） |
| 最小 | `最小(序列A, 序列B或数)` | 逐点取小 | 新增（np.minimum） |

- 算子参数支持：序列（如 `收盘价`、已定义变量）、标量数字（如 `9`）。
- 全部 O(n)，用 numpy 实现，不做 pandas 依赖假设。

### 1.4 完整示例

全中文 KDJ（模板之一，用户可再改）：

```
RSV := (收盘价 - 最低N日(最低价, 9)) / (最高N日(最高价, 9) - 最低N日(最低价, 9)) * 100;
K   := 平滑(RSV, 3, 1);
D   := 平滑(K, 3, 1);
输出 := 3 * K - 2 * D;
```

用户自建因子示例（布尔条件输出）：

```
牛熊线 := 均线(收盘价, 30);
强度   := (收盘价 - 前值(收盘价, 5)) / 前值(收盘价, 5) * 100;
输出   := 上穿(收盘价, 牛熊线) 且 强度 > 3;
```

### 1.5 词法与安全约束

- 标识符：`[A-Za-z_\u4e00-\u9fa5][A-Za-z0-9_\u4e00-\u9fa5]*`（变量名可用中文）。
- 保留字（不可作变量名）：`且 或 非 输出` + 全部中文数据名 + 全部算子中文名 + 英文兼容名。
- **全角归一化**：输入预处理把全角标点转半角（`（）：；，、"` → `():;,"`），中文输入法默认全角，必须自动转换。
- **安全（绝不用 eval）**：`ast.parse` 后白名单遍历。只允许：
  - 节点：BinOp、UnaryOp、Compare、BoolOp、Call、Name、Constant
  - 运算符：加减乘除、取反、比较、且/或/非
  - 函数：仅算子表白名单
  - 拒绝：Attribute / Subscript / Lambda / 非白名单 Call / 下划线开头的 Name / 字符串常量（防止路径/代码注入）
  - 求值超时 0.5s，异常与 NaN 一律回落 0.0。

## 2. 后端实现

### 2.1 新模块 `engine/custom_factor.py`

组件：

| 组件 | 职责 |
|---|---|
| `normalize_fullwidth(text)` | 全角→半角预处理 |
| `FormulaLexer` / `FormulaParser` | 词法+递归下降解析，产出 AST（节点复用 ast 标准库常量/表达式节点，或自定义轻量节点均可，推荐自定义节点便于白名单校验） |
| `BUILTIN_OPERATORS` | 算子中文名/英文名 → 实现函数 映射表（含参数个数校验、中文说明） |
| `compile_formula(formula_text)` | 解析 + 变量收集 + 依赖拓扑排序 + AST 白名单校验，返回编译产物 `CompiledFormula` |
| `CompiledFormula.eval_series(ctx) -> np.ndarray` | 对整段 K 线求值，返回输出序列（长度=len(closes)） |
| `compile_to_factor(formula_text, label)` | 编译并包装成 `FactorDef.calc_fn` 闭包：`calc_fn(ctx, params) -> float` 取输出序列末值（NaN→0.0），支持 `输出` 为布尔序列时转 0/1 |

序列算子实现要点（numpy，全 O(n)）：
- 滚动 min/max/sum/std：`numpy.lib.stride_tricks.sliding_window_view`（n ≤ 250，内存安全）或等价的卷积/循环，窗口不足 N 的位置置 NaN。
- 平滑（Wilder）：`y[i] = (x[i] * M + y[i-1] * (N - M)) / N` 递推。
- 前值：`np.concatenate([nan, arr[:-N]])`。
- 上穿：`(A > B) & (~(A_prev > B_prev))`。
- 所有算子入口防御：输入长度不足 → 输出全 NaN；除零 → NaN。

### 2.2 注册进因子体系

- 自定义因子内部 name 用 `cf_` 前缀 + 中文名（如 `cf_牛熊强度`），label 为中文名，避免与内置英文名冲突。
- 新增 `factor_registry.register_custom_factor(fdef)`：向 `REGISTRY` 插入 `FactorDef`，`category='custom'`。
- `calc_fn` 用 `compile_to_factor` 生成的闭包；`params_schema={}`；`default_weight=1.0`；`default_direction` 用户配置；`default_stats` 默认 `{mean:0, std:1}`（见 2.5 样本估算）。
- 应用启动、配置保存时：读取 `custom_factors` 段 → 全部编译注册。编译失败则该因子置为"无效"并在配置界面报错（不阻断启动）。
- 打分循环 `calc_all_active_factors` 无需改动。

### 2.3 配置存储

在 scheme 配置新增独立段 `custom_factors`（避免污染 `factor_configs` 的 schema 校验）：

```json
{
  "custom_factors": {
    "牛熊强度": {
      "formula": "牛熊线 := 均线(收盘价, 30);\n强度 := ...;\n输出 := ...;",
      "weight": 1.0,
      "direction": 1,
      "stats": { "mean": 0, "std": 1 }
    }
  }
}
```

- `quant_config`：新增 `get_custom_factors() / save_custom_factors(d)`；`save_scheme_config` 写盘时一并持久化该段。
- `get_factor_configs()`（打分取参处）在返回前**合并** custom_factors 生成的条目（`{weight, direction, params:{}, stats}`），保证打分与 UI 读取一致；`active_factors` 中直接存放中文因子名。
- `ui/config_mapper.py`：`_default_quant_data` / `_config_to_quant_data` / `_quant_data_to_config` 三处同步映射 `custom_factors`（对齐项目"嵌套结构、键名一致"约定），`_validate_quant_before_save` 增加公式非空/唯一名校验。

### 2.4 样本估算 stats

- 新因子无历史 mean/std，z-score 会失衡。提供"用当前扫描样本估算"：取最近一次全市场扫描（或选定的 500 只样本）的因子值计算 mean/std，写入 stats；未估算时回落 `{mean:0, std:1}`。
- IC 回测链路（`get_factor_stats` / `save_factor_stats` 已有）可直接覆盖自定义因子，验证有效性。

## 3. API 设计（ui/web_api.py，PyWebView JS API 风格）

| 方法 | 入参 | 返回 |
|---|---|---|
| `get_custom_factor_meta()` | — | 算子清单：`{中文名, 签名, 说明, 分类}`（供函数库面板渲染） |
| `validate_custom_formula(formula)` | 公式文本 | `{ok, errors:[{line,col,msg}], variables:[...], output}` |
| `preview_custom_formula(formula, code)` | 公式、股票代码 | `{dates, values, latest, kline}`（kline 供 ECharts 叠图；复用现有 K 线数据） |
| `try_custom_formula(formula, codes)` | 公式、若干股票代码 | 每只股票的 `{code, name, latest, mean, std, valid}`（多股抽查，防除权/停牌算歪） |
| `list_custom_factors()` | — | 已存因子列表（名称/公式/权重/方向/状态） |
| `save_custom_factor(name, formula, weight, direction, estimate_stats)` | — | `{ok}` / `{ok:false, error}`（保存即编译注册；estimate_stats=true 时先跑样本估算） |
| `delete_custom_factor(name)` | — | `{ok}` |

授权口径：跟随 scheme/配置类端点（无 require_license），与项目"配置类端点免授权、避免 H5 二次限制"的既有约定一致。

## 4. 前端 UI（桌面壳 `ui_mockup/index.html`）

> 落地决策：编辑器建在桌面壳（`ui_mockup/index.html`），不建 H5/Vue 壳。
> 原因：后端引擎/注册/配置/API 已在 Python 侧就绪，桌面壳经 `ui/web_api.py` 的 PyWebView JS API 直连同一套评分链路；H5 壳需另起 Vue 组件与构建链，成本更高且与"配置类端点免授权"的 H5 约定耦合。

### 4.1 入口与布局

- 在"量化模型"页（`quantTabs`）新增「自定义指标」子页（`#q-custom-indicator`），与因子管理/权重方向等同级，并纳入 `data-gated-factor` 用法门控。
- 三栏布局（对齐深色行情终端风格，红涨绿跌、高信息密度）：

```
┌ 顶部：指标名称输入 │ 模板(KDJ/MACD/RSI) │ 权重/方向 │ 保存并启用 ┐
├───────────────┬─────────────────────┬────────────────────────────┤
│ 函数库面板    │ 公式编辑区           │ 实时预览区                  │
│ · 序列算子    │ · 多行变量赋值       │ · 样例股票 K线 + 指标曲线   │
│ · 原始数据    │ · 语法高亮（中文）   │ · 最新值 / 近5日数值         │
│ 点击即插入    │ · 边写边校验         │ · 换股抽查 / 多股试算       │
│ 中文名+说明   │ · 输出行标记         │                            │
└───────────────┴─────────────────────┴────────────────────────────┘
```

### 4.2 关键交互

- 函数库：后端 `get_custom_factor_meta` 返回算子/原始数据/逻辑词清单，按分类渲染；点击项 → 插入光标处（函数自动补 `(`），支持搜索过滤。
- 语法高亮：自研轻量高亮器（`cfHighlight`），`textarea` 透明叠在 `pre` 之上、滚动同步；按 meta 动态着色函数/数据/逻辑/数字/注释，不引 CodeMirror/CDN，避免打包负担。
- 校验：输入变化防抖 450ms 调 `validate_custom_formula`，显示首个错误的行/列与原因；通过时渲染变量清单（输出变量高亮）。
- 预览：默认自选第一只股票（可手填代码/切周期），调 `preview_custom_formula`，ECharts 双轴叠图（收盘价 + 指标）；显示最新值、有效点数、近 5 值。
- 多股试算：调 `try_custom_formula` 抽查自选前 20 只，列出末值/均值/标准差/有效占比，排查除权/停牌算歪。
- 模板：后端提供全中文模板，一键载入可改。
- 全角自动归一化在后端词法层完成（`normalize_fullwidth`）。

### 4.3 保存流程

输入校验通过 → `save_custom_factor(name, formula, weight, direction, estimate_stats=true)` → 后端编译注册 + 写配置 + 样本估算 stats → 前端把新因子并入 `quantData.factors`（`cat='custom'`）并刷新因子表格 → 保存方案后进入评分/扫描/回测链路。已保存列表支持编辑/预览/删除（删除同步 `unregister_custom_factor`）。

## 5. 测试清单

| 层 | 用例 |
|---|---|
| 词法/语法 | 中文变量名、全角标点归一化、中文算子调用、缺分号/括号报错、保留字作变量名报错 |
| 安全 | 拒绝 `import`/`__`/属性访问/字符串常量/未知函数；超时回落 |
| 算子正确性 | KDJ 全中文模板 vs `KDJCalculator.calc_all` 数值一致（±1e-6）；MA/EMA 复用路径对照；LLV/HHV/REF 边界（N=1、N=len） |
| 集成 | 注册后 `calc_all_active_factors` 生效；NaN→0；active_factors 含中文名可打分 |
| 配置 | custom_factors 写盘/读回一致；config_mapper 三向映射；`_validate_quant_before_save` 通过 |
| API | validate/preview/save/delete 往返；estimate_stats 后 stats 非默认 |
| UI（桌面壳） | 编辑器 JS 块 `node --check` 语法校验通过；DOM id / CSS class 与 JS 引用逐一核对；模板载入、点击插入、错误行标红、预览出图为人工走查 |

## 6. 落地顺序（每阶段独立可交付）

1. **阶段 1 引擎**（新模块 `engine/custom_factor.py` + `tests/test_custom_factor.py`）：词法/解析/AST 白名单/算子/编译求值/闭包包装。验收：KDJ 模板全中文与现有 KDJ 因子数值一致。
2. **阶段 2 注册与配置**：`register_custom_factor` + `custom_factors` 段读写 + `get_factor_configs` 合并 + config_mapper 映射。验收：手工注册后打分/扫描/回测链路跑通，IC 可回测。
3. **阶段 3 API**：web_api.py 新增 7 个方法。验收：validate/preview 返回正确 JSON。
4. **阶段 4 UI（桌面壳）**：`ui_mockup/index.html` 新增「自定义指标」子页（函数库/编辑区/预览三栏 + 自研高亮 + ECharts 叠图）。验收：完整走通"选模板→改公式→预览→保存→打分生效"。
5. **阶段 5 测试**：`tests/test_custom_factor.py`（43 例：词法/解析/安全/算子/模板/注册集成/配置往返/WebAPI）；另在 `server/adapters/engine_bridge.py` 健康口径排除 `cf_` 前缀，保住骨架契约 21 内置因子不变。

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| 中文输入法全角标点导致语法错误 | 输入归一化 + 校验提示（1.5） |
| 用户拿通达信语法直接贴（兼容预期） | 定位为"子集"，meta 接口提供每个算子的中英文名与说明；文档明示差异 |
| 除零 / 停牌 / 次新股数据不足算歪 | 算子 NaN 防御 + 多股抽查试算（try API） |
| 自定义因子 z-score 失衡 | 默认 0/1 + 样本估算（2.4） |
| 全市场扫描性能 | 与现有因子同量级 O(nodes×n)；后续可并入 `_precompute_tech_series` 缓存 |
| 公式编译失败阻断启动 | 无效因子标记降级，配置界面报错，不阻断（2.2） |
