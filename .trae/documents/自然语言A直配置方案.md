# 自然语言 → AI 配方案（筛选条件 + 因子权重）

## Context（背景）

用户希望**直接用自然语言描述需求**（如"帮我配 RSI 大于 80、市净率在 1 以下的方案"），AI 理解后自动配出方案，而不是让用户填偏好表单。已与用户确认：

- **生成方式**：AI 解析自然语言 → 输出结构化条件，套在固定骨架内（不自由改结构）。
- **覆盖范围**：**两个场景都要** —— ① 扫描/筛选条件（`scan_tech_filter`），② 因子权重/阈值（`factor_configs` 或方案打分相关）。AI 根据描述自动判断配到哪个。
- **硬约束（关键发现）**：方案因子白名单全是**技术因子**（21 个），数值指标白名单 `AND_INDICATORS` 也是技术指标（MA/RSI/MACD/KDJ/量比/ADX/ATR/乖离率等）。**市净率(PB)、市盈率等基本面指标目前不支持**。用户已确认：**只在现有白名单内翻译**，遇到市净率/市盈率等不在白名单的指标，AI 明确提示"暂不支持"，不臆造。

## 目标产物形态（必须合法）

`scan_tech_filter` 单条两种形态（见 `quantModel.spec.js:33` 测试夹具 + `Threshold.vue`）：
- 技术信号：`{ signal: '多头排列' }`（值来自 `SIGNAL_OPTIONS` 中文，如 无要求/多头排列/空头排列/站上MA5/跌破MA20/KDJ金叉/MACD金叉/放量上涨…）
- 数值条件：`{ indicator: 'sma_20', op: '>=', value: 60 }`，其中 `indicator` 取 `AND_INDICATORS` 的 key（sma_5/sma_10/rsi_14/macd/volume_ratio/bias20…），`op` 取 `AND_OPS = ['>=','<=','>','<']`

因子权重侧：`factor_configs[因子name] = { name, weight, direction, params, stats:{mean,std} }`，因子 name 必须来自 `FACTOR_ROWS`。

## 设计

### 后端

**新增 prompt 模板**：`server/ai/prompts.py` 加 `scheme_conditions_prompt(natural_text)`：
- system 讲清楚两份白名单（SIGNAL_OPTIONS 中文、AND_INDICATORS key→中文、AND_OPS）+ 约束：只输出白名单内的条件；基本面指标不在白名单没如实提示不支持；一个自然句可产出多个条件（同为 AND）。
- 要求 AI 只输出 JSON：`{ "filters": [...], "factor_tweaks": {...}, "unsupported": ["市净率", ...] }`，其中 `filters` 是 `scan_tech_filter` 合法条目，`factor_tweaks` 是因子权重调整，`unsupported` 列出识别到但不支持的基本面指标。
- 用 `client.chat` 请求，让模型只回 JSON（用 json.loads 解析，失败则提示）。

**新增端点**：`server/api/ai.py` 加 `POST /api/ai/scheme-conditions { text: str }`：
- `_require_configured()` 校验
- 调用 client.chat；解析 JSON
- **合法性兜底**：`filters` 里 indicator 不在 `AND_INDICATORS`、op 不在 `AND_OPS`、signal 不在 `SIGNAL_OPTIONS` 的条目丢弃；factor name 不在 `FACTOR_ROWS` 的丢弃。返回清洗后的 `{filters, factor_tweaks, unsupported}`，供前端预览。

>`server/api/scheme.py` 的创建/更新请求体 `config` 是 dict，直接接受，无需改写入契约；如用户最终保存，前端复用现有 `quantToConfig` 把 AI 清洗后的条件塞进 config，再走 `POST /api/scheme` 的现有 CAS 写入。

### 前端（Config 视图）

- ConfigView 相关页签（扫描筛选 `Threshold.vue` / 因子 `FactorManage.vue`）加一个"自然语言配置"入口框 + 按钮：用户输入"帮我配 RSI 大于 80、市净率在 1 以下"。
- 调 `web/src/api/ai.js` 新增 `aiSchemeConditions(text)`。
- 返回后：
  - `filters` → 映射进 `scanFilter`，在 Threshold 页签展示为可预览的可编辑条件列表；
  - `factor_tweaks` → 映射进 factors 的 weight/dir；
  - `unsupported` 非空 → 弹提示"市净率：本工具暂不支持基本面指标，已忽略"。
- 用户确认后保存（走现有方案保存）。

## 需要改动的文件

后端：
- `server/ai/prompts.py`：加 `scheme_conditions_prompt(text)`，导入白名单常量
- `server/ai/client.py`：无改动（复用 chat）
- `server/api/ai.py`：加 `POST /api/ai/scheme-conditions`
- 可能：`server/engine/...` 里确认 `SIGNAL_OPTIONS`/`AND_INDICATORS` 白名单的后端来源（若白名单需要服务端读取，加一个常量模块）

前端：
- `web/src/api/ai.js`：加 `aiSchemeConditions`
- `web/src/config/panes/Threshold.vue`：加自然语言输入 + 预览应用 filters
- `web/src/config/panes/FactorManage.vue`：加自然语言输入 + 预览应用 factor_tweaks（或集中在 ConfigView 顶部统一入口）

## 验证

1. **单元**：`client.chat` monkeypatch 返回非法/合法 JSON，验证清洗逻辑（非法 indicator/op/signal 被丢弃、unsupported 透出）。
2. **API**：pytest 覆盖 `POST /api/ai/scheme-conditions`（合法文本 → filters/factor_tweaks；含市净率文本 → unsupported 非空；未配置 key → 400）。
3. **端到端**：Config 页输入"RSI 大于 80 小于 90" → 预览出 `{indicator:'rsi_14', op:'>', value:80}` 等条件，保存后扫描生效；输入含市净率 → 提示不支持并忽略。
4. **回归**：确认 `quantToConfig` 生成的 `scan_tech_filter` 能被现有 `scan_tech_filter` 消费（对照 `quantModel.spec.js` 夹具）。

## 备注

- "RSI 大于 80 小于 90"应译为两条：`rsi_14 >= 90`（或按 AI 判断用 `>`/区间），方案里 AND 条件叠加。具体 `rsi_14` vs `rsi_6/24` 由 AI 结合语境选择并如实输出。
- 不新增基本面因子（用户已确认，避免动数据源/引擎）。
- 因子权重 `factor_tweaks` 也可用于非打分场景，根据描述由 AI 决定落到 filter 还是 weight。