# 接入 DeepSeek AI 解读层

## Context（背景）

M-Bull 股票工具目前只输出引擎量化结果（分数/星级/报告文本），面向不懂代码的散户缺乏"说人话"的解读。用户希望在四个功能处接入 DeepSeek 大模型，把量化结果翻译成白话分析，帮助散户克制情绪、执行纪律。

已与用户确认的设计决策：
- **触发方式**：每个结果加"AI解读"按钮，点按才调用、按需付费。
- **Key 配置**：复用现有 `Config` 视图（模型配置页）加一个 AI 配置表单，key 写入服务端配置文件。
- **首期范围**：四个功能全部接入（个股分析 / 自选诊断 / 全市场扫描 / 情绪账本）。
- **延伸能力**：AI 解读输出里同时给出**建议触发条件（买入/止损）**和**筛选条件建议**，作为文案返回，不做结构化入库（避免过度工程）。对"用 AI 帮助设计方案/触发/筛选"的回答：可以，但以"AI 在解读中给出建议"的形式提供，不新增独立的方案设计 UI。

## 总体原则

- **不改核心交易引擎**。AI 是叠加在既有 API 结果之上的"解读层"：新增独立模块 + 独立端点，对现有 analyze/scan/diagnosis/ledger 的返回结构零侵入。
- 复用现成基础，不重复造轮子。

## 现状结构（已探明）

- 后端 FastAPI，路由在 `server/api/router.py`，各功能适配层：`server/adapters/analysis.py`、`scan.py`、`diagnosis.py`；账本数据在 `server/db/ledger_repo.py`、`emotion_repo.py`。
- 依赖已含 `requests==2.34.2`（锁在 `requirements-server.txt`），**无需新增 SDK**，直接调 DeepSeek 的 OpenAI 兼容 REST 端点即可。
- 服务端配置文件：`config/` 目录下 JSON（如 `data_sources.json`）。API key 的读写可放在 `config/ai_config.json`。
- 前端 Vue3 + Element Plus，`web/src/App.vue` 顶部导航，`web/src/views/` 各视图，`web/src/api/*.js` 用原生 `fetch` 封装后端调用。

## DeepSeek 接口要点

- 端点：`POST https://api.deepseek.com/chat/completions`（OpenAI 兼容）
- 鉴权：`Authorization: Bearer <API_KEY>`
- 请求体：`{model, messages:[{role,content}], stream:false}`
- 模型名（2026 现行）：`deepseek-flash`（便宜、够用，推荐默认）/ `deepseek-v4-pro`（更强、更贵）。key 在 platform.deepseek.com 申请。
- 说明：本项目通过 `requests` 直连，不引入 openai SDK。

## 方案（新增 + 改造）

### 1. 新增服务端 AI 模块（核心）

**`server/ai/__init__.py` + `server/ai/client.py`** —— DeepSeek 调用封装：
- `chat(prompt, system) -> str`：POST `/chat/completions`，`requests.post`，超时设置合理（如 40s），解析 `choices[0].message.content`。
- 捕获错误映射为统一 `ApiError`（Key 未配置 `AI_NOT_CONFIGURED`、额度/401 `AI_AUTH_FAILED`、超时 `AI_TIMEOUT`、其他 `AI_UPSTREAM_ERROR`）。
- 支持按需指定 model（读配置默认值）。

**`server/ai/prompts.py`** —— 四类 prompt 模板（拼接喂给模型的结构化数据）：
- `analyze_prompt(report_data, report_text)` → 白话解读 + 买卖点/止损触发条件建议。
- `diagnosis_prompt(stock_item)` → 单只诊断白话解读 + 触发条件建议。
- `scan_prompt(items_sample, filters)` → 扫描结果概览 + 筛选条件改进建议（只喂前台可见的 top-N，控 token）。
- `ledger_prompt(signals, summary)` → 纪律/情绪画像白话复盘 + 改善建议。

**`server/ai/config.py`** —— 读写 `config/ai_config.json`（`{api_key, model, enabled}`），提供 `get_ai_config()/save_ai_config()`。

### 2. 新增 API 端点（`server/api/ai.py` + 挂到 router.py）

- `GET  /api/ai/config` → 返回是否已配置（**不返回 key 明文，只回 `configured: bool` + `model`**）
- `PUT  /api/ai/config` `{api_key, model}` → 保存（`enabled` 依据是否有 key）
- 四个解读端点（按功能，各取所需数据再交 prompt 模板）：
  - `POST /api/ai/analyze {code, market_type?, k_type?, scheme?}` → 复用 `analysis.analyze()` 拿结果，生成解读
  - `POST /api/ai/diagnosis {item}` → item 为诊断单条结果对象
  - `POST /api/ai/scan {items, filters?}` → items 为扫描结果列表
  - `POST /api/ai/ledger {market?}` → 复用 `ledger_repo` 拿 signals+summary，生成复盘
  - 统一响应 `{ok, text, cost_estimate?}`
- 这些端点**不依赖 license 也不强制**——为控制成本，默认按"已配置 key 才可用"，401 时返回明确的配置提示。可暂时挂在公开路由（与 ledger 一致）。

### 3. 前端改造

- **Config 视图**（`web/src/views/ConfigView.vue`）：页面顶部加"AI 配置"卡片（api_key 密码框 + model 下拉 + 保存/测试）。新增 `web/src/api/ai.js`。
- **四个视图各加"AI解读"按钮 + 结果展示区**（用 `el-dialog` 或新增 `web/src/components/AiInterpret.vue` 复用组件，入参场景 + 数据，内聚按钮加载态/错误/文案渲染）：
  - `HomeView.vue`（个股分析页，推测首页承载）→ 分析结果旁加按钮
  - `DiagnosisView.vue` → 每行结果加按钮
  - `ScanView.vue` → 结果表格顶部"AI概览"按钮
  - 账本页（在哪个视图需实现时定位，可能在 Home 或独立账本区）→ 复盘按钮
- `App.vue` 导航不动（尊重"不改信息架构"）。

### 4. 依赖与配置

- `requirements-server.txt`：`requests` 已在列，无需新增。
- 新增 `config/ai_config.json.example`（骨架，含字段说明；`api_key` 留空）。
- `server/core/lockfile.py` 不需改（未加新第三方包）。

## 需要改动/新增的文件清单（代表路径）

后端：
- 新增 `server/ai/__init__.py`、`server/ai/client.py`、`server/ai/prompts.py`、`server/ai/config.py`
- 新增 `server/api/ai.py`
- 改 `server/api/router.py`（import + include ai.router）
- 新增 `config/ai_config.json.example`

前端：
- 新增 `web/src/api/ai.js`、`web/src/components/AiInterpret.vue`
- 改 `web/src/views/ConfigView.vue`（AI 配置卡片）、`web/src/views/HomeView.vue`（个股分析）、`web/src/views/DiagnosisView.vue`、`web/src/views/ScanView.vue`、账本所在视图

## 验证方式

1. **单元**：`server/ai/` 的 `config` 读写与 prompt 模板构造（无网络）；`client` 用 monkeypatch `requests.post` 返回假响应。
2. **API 层**：pytest 覆盖 `/api/ai/config` GET/PUT（验证不透出 key）与四类解读端点（monkeypatch client）。
3. **端到端**：本机填好 key → Config 页保存 → 首页/诊断/扫描/账本点"AI解读"→ 看到白话文案与触发/筛选建议；不填 key 时点按钮提示引导配置。
4. 回归：现有 analyze/scan/diagnosis/ledger 端点行为不变（AI 为纯增量，路由已回归）。

## 备注 / 待实现时确认的小项

- 账本视角实际落在哪个前端视图（需定位账本 UI 所在组件后接入）。
- DeepSeek 模型名用 `deepseek-flash` 为默认，可在 config 里改 `deepseek-v4-pro`。
- token 成本控制：scan/ledger 只喂摘要与 top-N，不喂全量；prompt 模板里明确限制篇幅。