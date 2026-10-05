# 功能矩阵：桌面 → H5 迁移对照（Feature Matrix）

> 产出时间：2026-09-12
> 依据：`docs/h5-feasibility.md`（63 条分级判定）+ `docs/完整规格_合并版.md` §4（功能明细）+ `docs/附录-配置页控件清单.md`（F-603 控件级验收依据）
> 说明：H5方案列中的 `A/B/C/E` 为 h5-feasibility 分级；`E→替代` 表示该条为 E（机制不可实现）的**替代方案**（来源 C1 §4.3），非"迁移现有实现"。
> 优先级口径：按"不做会怎样"判定，不按工作量。P0=不做没法用；P1=核心；P2=闭环；P3=边缘。
> 测试状态：H5 尚未开工，**均为「未测」**；验收标准无法定稿者汇总于「阻塞清单」（**2026-09-12 更新：原 9 条阻塞项已全部拍板定稿，含 `F#1` / `F#2`，现无待补项**）。
> ⚠️ **行号会漂移（2026-09-27 标注）**：本表「桌面入口」列的 `file:LINE` 继承自 `h5-feasibility.md`，是 2026-09-12 的快照，源码此后多次改动，行号**已普遍失准**。定位符号请按**函数/类名 grep**，不要按行号跳转。

## 一、逐条矩阵（63 条）

| 功能ID | 桌面功能 | 桌面入口 | 业务规则 | H5方案 | 平台差异 | 优先级 | 验收标准 | 测试状态 |
|---|---|---|---|---|---|---|---|---|
| F-101 | 单实例锁 | 启动时 `CreateMutexW` 互斥（`main_webview.py:329`）；无菜单 UI | 同账号"单活动编辑会话"只许 1 个，第二会话踢出（防并发写） | **[E→替代]** 后端会话表（Redis `SETNX`+TTL / DB 唯一约束）+ 前端 `BroadcastChannel` 多标签提示；承诺由「防多开」改「防并发写」；**处置策略已定稿 = 踢旧（`F#9`）** | 多窗口/多设备无法阻止 → 会话唯一 | P3 | 同账号并发开 2 活跃会话，**新会话胜出**：第 1 个（旧）会话被强制登出，其后续心跳/请求返回 `401`；复用 F-102 心跳通道（5s）下发 revoke | 未测 |
| F-102 | 崩溃可见化 | `sys.excepthook` → `%TEMP%` 崩溃日志 + MessageBox 弹窗（`main_webview.py:53`） | 标签崩溃/白屏/OOM 时 JS 上下文已销毁，`window.onerror`/`unhandledrejection` 均不触发 | **[E→替代]** 后端结构化日志（服务端崩溃落盘）+ 前端 5s 心跳，超时判「客户端失联」（**无堆栈，只能事后推断**） | 进程级崩溃钩子/MessageBox → 心跳失联判定 | P3 | 前端每 5s `POST /api/heartbeat`；后端 15s 未收到该会话心跳 → 写 `client_lost` 日志（含时间戳+会话ID） | 未测 |
| F-103 | 前端 JS 异常上报 | `window.onerror`+`unhandledrejection` → 售后面板 `#jsErrorLogBox` | 捕获前端 JS 异常上报后端日志文件 | **[A]** `window.onerror` 原样复用，改为 `POST /api/js-error`（跨设备可见）；或 `localStorage` 环形缓冲（最近 200 条） | 本地面板读文件 → 跨设备可见 | P3 | 触发一次 JS 错误后 `POST /api/js-error` 收到 `{msg,url,line,col,stack}` 且返回 2xx | 未测 |
| F-104 | 版本与应用信息 | 设置→关于（`get_app_info`） | `get_app_info` 返回 `version`+授权+`machine_id` | **[B]** `/api/app-info` 返回 `version`+账号/授权状态；`machine_id` 作废换 `device_id`；license 文案改口径 | 硬件 `machine_id` → 服务端 `device_id` | P2 | `GET /api/app-info` 返回 `version` 与账号状态；响应不含 `machine_id`、含 `device_id` | 未测 |
| F-105 | 回测子进程协议 | 主进程 `--backtest-cli` 分发；关窗 `kill_tree`（`main_webview.py:270/355`） | 主进程启子进程跑回测，关窗强杀进程树 | **[B]** 后端任务队列 + SSE 推进度；取消=服务端可中断标记（保留逐只检查点语义 `:3210`）；协议层整体重写 | 子进程/文件句柄/进程树 kill → 后端任务状态机 | P1 | 提交回测返回 `task_id`；SSE `progress` 事件单调递增；`POST /cancel` 后 ≤1 只检查点内置 `cancelled` | **已测**（2026-09-13，`tests/h5_skeleton/test_f105_backtest.py`：BT1~BT6 三条验收逐字通过。**B38=进程内线程池**（单副本过渡，`TaskExecutor` 接缝不变，R-09 外置后换 Celery+Redis）；`server/core/task_store.py` 状态机 + `server/worker/executor.py` 真实实现 + `/api/backtest`（提交/SSE/cancel）） |
| F-201 | 行情跑马灯（顶部 ticker） | 顶部 ticker（`index.html:991-1003`），30s 刷新 | 指数+涨跌家数每 30s 拉取，源不可用显示 `--` 不阻塞 | **[B]** `/api/market/indices` + 30s 轮询或 SSE；跑马灯动画本身为 CSS | 源站（腾讯/新浪/东财）无 CORS → 服务端代理 | P0 | `GET /api/market/indices` 返回 `{indices[],advance,decline}`；非交易时段指数字段返回 `--` | **已测**（2026-09-12，`tests/h5_skeleton/test_f201_indices.py`：两条验收断言通过。**B15**：`--` 在接口中以 `null` 表示（前端渲染 `--`）；**B16**：交易时段判定服务端自建 `server/core/market_session.py`（`9:30-11:30/13:00-15:00`，含端点，无节假日日历）；**B17**：期货行情条已于 2026-09-13 补齐 —— `GET /api/market/indices?market=futures` 返回 6 大板块涨跌幅（`advance/decline` 恒 `null`），`engine/futures_indices.py` 由桌面 `_get_futures_indices` 上移，`tests/h5_skeleton/test_futures_indices.py` MF1~MF4） |
| F-202 | 实时行情 | 分析页行情面板 `#qPrice` 等（`index.html:1059-1086`） | 按腾讯/新浪**未公开**字段位序解析（`fields[3]`=现价等） | **[B]** `/api/quote/{code}` 代理 + 短 TTL 缓存；前端渲染结构复用 | 字段位序公开性 → 服务端兼容层 | P0 | `GET /api/quote/sh600519` 返回 `price/change_pct/volume/amount/time` 且字段映射正确 | **已测**（2026-09-12，`tests/h5_skeleton/test_f202_quote.py`：验收断言实盘通过，字段映射逐位校验。**B19**：非法代码 404、源不可用 200+`null`、不引入 `success`；**B20**：`volume` 统一为「手」（新浪源 /100）+ `volume_unit` 显式声明，已与同日 K 线量纲对账一致；**B23**：F-203 遗留「日K 实时修补」不纳入本条，单列待办） |
| F-203 | K 线查询 | 分析页查询栏（代码/周期/天数/手续费/K线根数）+ `klinePeriodSelect` | 周期日/周/60m/30m/15m/5m/1m；分钟级可用性随主源（默认 tdx 全周期；tencent 仅日/周，分钟报错）；日K300/分钟1023 根；期货夜盘合并 | **[B]** `/api/kline?code&period&days` 代理 + 磁盘缓存（复用 `qfq_daily_{code}.pkl` 语义）+ ECharts 渲染 | 分钟级仅新浪源 → 服务端集中代理（反爬放大）；夜盘归属依赖 `Asia/Shanghai` 时区 | P0 | `GET /api/kline?code=sh600519&period=日K&days=300` 返回 300 根序列；期货 `rb0` 日K 夜盘合并到下一交易日 | **已测**（2026-09-12，`tests/h5_skeleton/test_f203_kline.py`：FT1/FT2f 两条验收断言实盘通过；后端已交付，前端 K 线组件待 F-203 前端接入。**遗留**：桌面「日K 实时修补」（用实时行情刷新当日 OHLC）经 B23 判定**不纳入** F-202，已单列待办，见项目记忆） |
| F-204 | 股票搜索 | 分析页搜索框 `<datalist id="stockList">`，防抖 250ms | 名称/代码匹配，上限 20 条；Enter 触发查询 | **[B]** `/api/search` 查股票/合约池；或启动一次性下发全量码表（5121 条 ≈300KB JSON）本地过滤 | 全量下发 = 股票池变公开数据（商业资产评估）；低端机首次输入卡顿 | P0 | `GET /api/search?q=茅台` 返回 ≤20 条且含 `sh600519` | **已测**（2026-09-12，`tests/h5_skeleton/test_f204_search.py`：验收断言实盘通过；**B12 已拍板=服务端搜索**，不下发全量码表；**B13=支持裸 6 位数字代码匹配**；**B14=期货搜索已于 2026-09-13 补齐** —— 独立端点 `GET /api/search/futures?q=` 返回 `[{name,code,symbol,exchange,multiplier,contract_month}]`，`engine/futures_search.py` 由桌面 `search_futures` 上移，`tests/h5_skeleton/test_futures_search.py` FSF1~FSF6） |
| F-301 | 查询流程（A 股） | 分析页查询栏「查询」（`doQuery`） | 评分K线 365 天/600 根、<30 条返回无法评分；`TradingPipeline.execute` 计算；扣额度 | **[B]** `/api/analyze` 全量托管（引擎零改造复 import），前端只提交参数+渲染 `reportData` | 本地计算+额度 → 后端托管 + **服务端授权判定**（`F#4` 订阅制，不按次计量）；服务端锁 `pandas==3.0.3/numpy==2.5.1` | P0 | `POST /api/analyze {code:'sh600519',k_type:'日K'}` 返回 `reportData` 且含 `factorScore`/`entry_tier`/`env_*`；K线 <30 条返回「无法评分」 | **已测**（2026-09-12，`tests/h5_skeleton/test_f301_analyze.py`：AS1/AS2 两条验收断言实盘通过。**B24**：`reportData` 组装 863 行上移 `engine/report_data.py`；**B27**：`analyze()` 主体上移 `engine/analyze_service.py`（解 `self._market_type` 全局态，A/B 对拍 `reportData` 逐字段一致）；**B25**：授权准入单列未纳入；**B26**：`<30条`→422 `INSUFFICIENT_DATA`、期货→422 `NOT_IMPLEMENTED`。**字段名偏差**：实现无 `entry_tier`，入场档位真名为 `reportData.status`（顶层 `entry_tier` 取该值）；实现无 `env_*`，环境为 `reportData.env` 单字段） |
| F-302 | 期货分析流程 | 分析页期货查询 | 过期合约 YYMM<now 拦截；期货 `up_ratio=0.5`；方案 market/direction/period 全匹配 | **[B]** `/api/analyze` 带 `market_type='futures'`；前端按 `futures_contract` 结构渲染 | 过期判定依赖服务端时区；期货分钟走新浪反爬风险更高 | P0 | `POST /api/analyze {code:'rb2501',market_type:'futures'}` 过期返回「已到期/已交割」；有效合约返回 `futures_contract.multiplier` 等元信息 | **已测**（2026-09-12，`tests/h5_skeleton/test_f302_futures.py`。**B29**：期货主体 228 行上移 `engine/analyze_service.py: analyze_futures()`；**B30**：过期 → 422 `CONTRACT_EXPIRED`，message 含「已到期/已交割」；**B31**：期货搜索/期货行情条不随本条。**时区**：`now` 由服务端注入 `Asia/Shanghai`（原实现用 naive 本地时间）。⚠️ **有效合约 happy path 未端到端实测**：当前配置 `config/quant_model.json` **缺失**（仅存 .bak），引擎用兜底方案且**无期货方案**，故该路径仅做契约级断言，需补期货方案后复验） |
| F-303 | 报告内容（reportData） | 报告区（`report_text`/报告面板） | `reportData` 由 `_build_report_data(ctx)` 产出；`ctx.decision` 建仓裁决单一真相源（`report_builder.py:40`） | **[B]** 后端计算 + 前端渲染 `reportData`；`report_text` 直接透传 | 前端**不可**自行拼接（与 `report_builder` 语义分叉→「核心结论」与「多空信号」不一致） | P0 | analyze 响应 `reportData.factorScore` 与 `report_text` 决策徽章结论一致（同 `ctx.decision` 派生） | **已测**（2026-09-12，`tests/h5_skeleton/test_f303_report.py`：RS1~RS8 通过。实盘对照：`factorScore=-7.8` / `decisionConclusion='⏳ 观望 — 未达观望线，等待信号'` 与 `report_text`【核心结论】徽章**逐字一致**；文中「得分-8」= `f"{factorScore:.0f}"`。**B32**：不下发 `ctx.decision`（保持 reportData 35 键）。体积参考：reportData ≈6.6 KB / 35 键，移动端首屏懒加载属**前端职责**） |
| F-304 | 决策卡生成 | 决策卡弹窗 `#decisionCardModal` 导出按钮 | `html2canvas` 540×960 竖版 → base64 → 落本机目录 `decision_cards/` | **[A]** `html2canvas` **本地打包**（去掉 CDN）→ `canvas.toBlob()` → `<a download>`/`navigator.share`；或后端对象存储换 URL | 落本机目录 → 浏览器下载；iOS Safari 老版本不支持 `toBlob` | P0 | 点击导出触发 `Blob` 下载，文件名 `M-Bull_{code}_{YYYYMMDD}.png`，MIME `image/png` | 已测（2026-09-12，`web/src/decision-card/exportCard.spec.js`：9 条——文件名/MIME/Blob 下载/iOS toBlob 降级逐字通过；前端骨架 `web/` 初始化 Vue3+Vite+Element Plus+Pinia+本地 html2canvas） |
| F-305 | 仅行情查询 | 分析页「仅行情」按钮 `quoteOnlyBtn` | 只取 K 线+行情，**不扣额度**、不写报告、不走评分链 | **[B]** `/api/kline` + `/api/quote` 组合，**独立于 `/api/analyze`**（无额度/无方案/无因子） | 需独立限流桶，避免被分析请求挤占 | P0 | 走 `quote_only` 路径返回 kline+quote 且不写 `#reportContainer`；该路径独立限流、不扣额度 | 已测（2026-09-12，`tests/h5_skeleton/test_f305_quote_only.py`：QO1~QO7；独立限流桶 30次/分钟 env 可调、超限 429+`Retry-After`、期货未纳入 B35） |
| F-401 | 诊断启动 | 诊断页自选列表 textarea + 开始按钮 | 解析自选列表、方向过滤、方案校验、建任务；monitor 复用 `source='monitor'` | **[B]** `POST /api/diagnosis` 建任务返回 `task_id` | 内存任务字典 `_diag_tasks`（多副本 404）→ 外置任务表；`self._market_type` 全局态 → 请求级上下文 | P1 | `POST /api/diagnosis {text,market,direction,scheme_name}` 返回 `task_id`；同账号并发不同 market 不串市场 | **已测**（2026-09-13，`tests/h5_skeleton/test_f401_diagnosis.py`：DG1~DG6 两条验收逐字通过。**B39=`_parse_watchlist` 上移 `engine/watchlist_parser.py`**（`self._market_type`→显式 `market` 参数，AST 等价）；**B40=只做启动**，`diagnosis` 载体任务占位，逐只分析留 F-402；任务复用 F-105 队列，`market` 由 payload 显式携带不串市场） |
| F-402 | 诊断 worker | 诊断后台线程（`_diag_worker`） | 逐只串行 `analyze(_skip_quota=True)`；**整批只扣 1 次额度**；五组分类 | **[B]** 后端 worker 逐只执行 + SSE 推进度；`is_cancelled` 换服务端标记；建议服务端有限并发（4-8） | 串行逐只（50 只≈50 次网络往返）→ 服务端并发但顺序回传 | P1 | 50 只自选的诊断完成时 `summary` 五组计数之和=50；全程仅触发 1 次额度扣减 | **已测**（2026-09-13，`tests/h5_skeleton/test_f402_diag_worker.py`：DW1~DW6。**B41=`_classify_for_report` 上移 `engine/report_classify.py`**（AST 等价）；**B42=额度随 F#4 订阅制不扣减**（「仅触发 1 次额度扣减」退化为「授权准入在 F-401 启动时 1 次」，worker 逐只不扣）；worker 逐只串行 `analyze_stock`/`analyze_futures` + 五组分类 `buy/watch/avoid/position/error` 恒等式） |
| F-403 | 进度与取消 | 诊断进度条 + 取消按钮（1.5s 轮询 `setTimeout`） | 1.5s 轮询 `get_diagnosis_progress`；取消置 `is_cancelled`（下轮生效，最多延迟 1 只） | **[B]** 优先 SSE 单向下推替代轮询（减少请求量）；取消=服务端标记 | N 用户×M 任务轮询放大 → SSE | P1 | 诊断运行中 SSE 收到 `progress` 事件；`POST /diag/{id}/cancel` 后 worker ≤1 只内退出，状态 `cancelled` | **已测**（2026-09-13，`tests/h5_skeleton/test_f403_progress.py`：PR1~PR5 两条验收逐字通过。`GET /api/diagnosis/{id}` 进度查询 + `GET /events` SSE + `POST /cancel` 协作式取消，复用 F-105 task_store 与 F-402 summary） |
| F-404 | 报告导出 | 诊断页导出按钮 | 写 `.txt` 报告到 `diagnosis_reports/` 目录 | **[A]** `Blob` + `<a download>`（`自选股诊断报告_{ts}.txt`）；iOS 备「复制剪贴板」 | 写本机目录/原生保存对话框 → 浏览器下载（File System Access API iOS 无） | P1 | 导出触发 `Blob` 下载 `自选股诊断报告_{ts}.txt`（UTF-8，含 5 组表头） | 已测 |
| F-501 | 扫描启动 | 扫描页扫描按钮 | 5215 只全市场；限速源；~~`consume_use()` 预扣~~（**H5 取消：订阅制不计次**）；分钟级禁用 | **[B]** `POST /api/scan` 建任务；股票池由服务端持有 | 本地股票池 → 服务端持有；并发多用户需排队/配额 | P1 | `POST /api/scan {market:'stock'}` 返回 `task_id`；分钟级参数被 4xx 拒绝；**✅ 已定稿（`F#4`/`F#5` = 订阅制）**：提交不预扣、不计次，仅校验订阅/试用有效性 | **已测**（2026-09-13，`tests/h5_skeleton/test_f501_scan.py`：`POST /api/scan {market:'stock'}` 返回 `task_id`；分钟级 `k_type` 被 422（信封 `error.message`）拒；订阅制不预扣、仅校验授权） |
| F-502 | 扫描 worker | 扫描后台线程（`_scan`） | 每片 20 只、`ThreadPoolExecutor` 上限 16、watchdog 放弃慢分片；每 100 只/3s 聚合板块 | **[B]** 后端 worker 池 + SSE 推进度；`auto_n_workers()` 改 `psutil`/cgroup 限额；watchdog 语义保留 | 本地 16 线程 → 服务端 IP 集中请求源站易限流（需缓存预热+增量） | P1 | 扫描完成 `status='completed'` 且 `total_stocks` 计数准确；无进展分片被放弃后任务仍能推进至 99%+；**✅ 已定稿（`F#6`）**：验收样本 = 服务器 K 线缓存全量池（≈5215 只 × 300 根日K），前提 = 命中热缓存，耗时上限 ≤30 min（★P1 实测校准） | **已测**（2026-09-13，同上：分片并发跑完 `status='completed'`、`total_stocks` 计数准确；被放弃分片仍推进至 99%+） |
| F-503 | 结果读取/排序/筛选/分页 | 扫描结果表（排序/筛选/分页、每页 200 行滚动） | 星级→技术二级→分数区间→percentile→topn→分页；返回前剥离 `tech_snapshot` | **[B]** 排序/筛选/分页下推 SQL（`WHERE score BETWEEN...ORDER BY...LIMIT`）；percentile 用 `percent_rank()` | 内存全量排序+切片 → SQL 分页；`tech_snapshot` 剥离防响应膨胀 | P1 | 请求 `offset/limit` 返回对应页且响应不含 `tech_snapshot`；`rating=5` 过滤只含 ⭐≥5 结果 | **已测**（2026-09-13，同上：`GET /api/scan/{id}?offset&limit` 分页正确且响应剥离 `tech_snapshot`；`rating=5` 过滤只含 ⭐≥5） |
| F-504 | 扫描缓存 | 后台 `_save_scan_cache` 落盘 | 24h/72h 过期判定（`very_stale` 分支恒不可达=既有缺陷）；按市场隔离 | **[B]** DB 表（scan_run + scan_result）+ 24h 过期标记；修 `very_stale` 缺陷 | 单文件全量 JSON 覆写 → DB（避免并发互相覆盖） | P1 | 缓存 24h 内 `stale=false` 可复用；>24h 返回 `stale=true` 需重扫且不报 `very_stale` | **已测**（2026-09-13，同上：24h 内 `stale=false` 复用；>24h `stale=true` 且响应不含 `very_stale`） |
| F-505 | 板块强度 | 板块面板（`sectorMinScore`/`sectorMinCount`/刷新/编辑/模板，内置 26 板块） | `≥3 只`成板块、强/中/弱阈值 30/20/10；用户可编辑映射 | **[B]** 板块映射落 DB + 服务端聚合（复用 `_aggregate_sectors`）；用户映射按账号隔离 | 期货兜底「交易所名归其他」兼容逻辑需保留；缩小池子时大量板块消失 | P1 | `GET /api/sectors` 聚合仅含 ≥3 成分股板块；强弱分类按 30/20/10 阈值划分；**✅ 已定稿（`F#7` = 保留用户自定义）**：按账号隔离，合并优先级 **用户映射 > 内置 26 板块** | **已测**（2026-09-13，同上：`GET /api/sectors` 仅含 ≥3 成分股板块；强弱按 30/20/10 划分；用户映射 > 内置 26 板块） |
| F-506 | 导出/暂停/开始 | 扫描导出/暂停/开始按钮 | 导出 CSV（前端传完整路径 `open(path,'w')`）；暂停用 `threading.Event` | **[B]** 导出改后端生成 CSV + 前端 `Blob` 下载（弃「前端传路径」）；暂停/取消=服务端任务状态机 `paused/running/cancelled` | 「前端传路径写盘」浏览器不可能（iOS 无 File System Access → 服务端生成 + 下载） | P1 | 导出返回可下载 CSV（`utf-8-sig` 首字节 `EF BB BF` 供 Excel 不乱码）；暂停后 worker ≤1 片内置 `paused` | **已测**（2026-09-13，同上：导出 CSV `Content-Disposition` 用 RFC 5987 编码中文名、首字节 `EF BB BF`（utf-8-sig）；暂停→服务端 `paused`、继续/取消状态机） |
| F-601 | 方案管理 | 模型配置页顶部方案下拉 + 新增/重命名/删除/导入导出（`index.html:1310-1355`） | 方案唯一；名校验（非空/≤50/禁`-`）；导入 3 格式兼容；失败回滚 | **[B]** 方案落 DB + 导入改 `<input type=file>` + `file.text()`（现有代码已如此 `:7140-7145`）→ POST 后端；导出 `Blob` | 本地方案文件 → DB + **行锁/乐观锁**防并发写（既有 P0 历史，profile 互相覆盖） | P1 | `POST /api/scheme` 建同名方案返回 409；并发写同一 `factor_profile` 一成一败（乐观锁）；含 `-` 的命名被拒 | **已测**（2026-09-13，`tests/h5_skeleton/test_f601_scheme.py` 20 测 + 真 uvicorn 冒烟：同名 POST→409 `SCHEME_EXISTS`；含 `-`/空/超 50→422 `SCHEME_NAME_INVALID`；两写者持同版本并发 CAS→一成一败 `VERSION_CONFLICT`（含最新版本+`X-Latest-Version` 头）；列表字段名逐字对齐 §3.2；导入 3 格式 + 中途失败整体回滚无残留；导出 JSON 流 + 中文名 RFC 5987。存储层：SQLAlchemy（生产 PG / 测试 SQLite）） |
| F-602 | 配置保存 | 模型配置「保存配置」+ 分析前自动保存（`index.html:5122`） | 校验→加载→建壳→因子段写 profile→`save_scheme_config`；原子性靠**手动回滚**；`load_config(force_reload=True)` 进程级全局缓存 | **[B]** `PUT /api/scheme` **事务化写入**（方案壳+profile 同事务替代手动回滚）；缓存改请求级/租户级 | 手动回滚 → 真事务；全局缓存并发互冲 → 请求级 | P1 | 方案壳+profile 两步骤同事务：任一步失败整体回滚、无残留 | **已测**（2026-09-15，`tests/h5_skeleton/test_f601_scheme.py`：版本 CAS 冲突→409 整事务回滚、壳内容不变（`test_f601_put_version_cas` / `test_f601_concurrent_profile_write_one_wins_one_loses`）；多方案导入中途失败整体回滚无残留（`test_f601_import_library_atomic_rollback`）。H5 方案模型将 `config` 内联于壳行，profile 写入仅在导入路径出现且与壳同事务） |
| F-603 | 8 个子页签 | 配置页 8 子页签（`q-manage/q-weight/q-stats/q-penalty/q-threshold/q-position/q-backtest/q-ghost`，`index.html:1357`） | 因子管理/权重/统计/惩罚/状态分界/仓位/回测/幽灵；初/高级门控保留 | **[A]** 8 子页签 **1:1 搬迁**（表单+本地状态+提交）；数据由 F-602 读写 | 命令式 DOM `id` 直接取值（漏字段风险）→ 框架数据绑定；控件多**动态生成**（q-position 约 94% / q-ghost 100%）需逐项对照 | P1 | 「验收引用《附录-配置页控件清单》：q-position 渲染出 P-01~P-14（6 静态容器 + 动态控件，§1）；q-backtest 渲染 B-01~B-28（日志/结果容器 id 为 `btLog`/`btResult`，§2）；q-ghost 渲染 G-01~G-07 且 `ma_confirm` 空值回退显示 `sma_20` 不落盘（§3.3）」 | **已测**（2026-09-15，前端 `web/src/config/quantModel.spec.js` 11 测 + `panes.spec.js` 6 测）：quantModel 双层映射镜像 config_mapper（config↔quantData 双向、否决键 LONG/SHORT 按方向、因子 params 后端优先、百分比互转）；8 子页签组件全量落位 ConfigView（q-manage~q-ghost），挂载级验证 q-backtest 含 `btLog`/`btResult` 容器、q-ghost `ma_confirm` 空值回退显示 `sma_20` 不落盘、q-position 方向门控（长单隐顶部回落/短单用顶部回落）；F-602 保存链路 `quantToConfig → PUT /api/scheme`（乐观锁 version，409 冲突提示）。`vite build` 通过（ConfigView chunk 67KB） |
| F-604 | 模式门控（初/高级） | `html[data-usage]` CSS 门控 + 后端 `_is_basic_mode()` | 初级隐藏因子页签、扫描按 `tech_strength*100` 排序、免因子校验 | **[A]** 前端 CSS/JS 门控（切换即生效）+ 模式值 F-1601；**双端必须一致** | 首帧 `data-usage` 硬编码 advanced 闪烁 → SSR 注入 basic | P1 | 初级时 `data-usage=basic` 隐藏因子页签，且 `/api/scan` 排序按 `tech_strength*100`（双端一致） | **已测**（2026-09-15，`tests/h5_skeleton/test_f604_usage.py` 7 测：`GET /api/usage` 默认 basic（无 usage.json 或缺省非法字段回退 basic）、显式 advanced 生效、非法值 422；basic 下 `/api/scan` 默认排序按 `tech_strength*100`、advanced 下按 `final_score`（镜像桌面 `_scan_rank`），显式 sort=tech_strength/price 独立生效不被门控覆盖。新增 `server/adapters/usage.py` + `GET/PUT /api/usage`。**注**：engine `get_usage_mode()` 默认 advanced 仅用于保护存量配置，H5 缺省 basic 只作用于无 usage.json 的新户，二者不冲突） |
| F-701 | 回测模式 | 回测页 5 个模式按钮（B-14~B-18） | 5 模式（strategy/scoreic/factoric/futures/futuresic）；初级禁用；~~`consume_use()` 预扣~~（**H5 取消：订阅制不计次**） | **[B]** 后端异步长任务 + **独立 worker 镜像**（防 OOM 拖垮 API） | 回测分钟~小时级；容器化 ProcessPool 与 cgroup 交互 | P1 | `POST /api/backtest {mode:'factoric'}` 返回 `task_id`；非法 mode 4xx；初级账号 403 | 已测（见 test_f701_backtest.py，5 模式可提 + 非法 mode 422 + 初级 403） |
| F-702 | 参数映射 | 回测参数表单（B-01~B-13） | 池映射 全市场→full / 自选→watchlist / 默认→148；hold 5-60、scan 1-20 | **[A]** 纯字符串/数值映射可在前端完成（提交前组装 `run_params`）+ 后端再校验（推荐双端） | 中文常量改文案即静默失配（落 else→148）；前端后端同值 | P1 | 选「全市场」提交后后端收到 `pool='full'`；`hold=70` 被拒并提示范围 5-60 | 已测（见 test_f702_params.py，5 测：GET /api/backtest/modes 返 5 模式含 pool/hold/scan 约束；pool='full'/'all' 通过；hold=70→422 含「5-60」；hold 5/60 边界过、scan 越界拒；未知 pool 拒。新增 server/adapters/backtest.py） |
| F-703 | 子进程编排 | 回测任务编排（`BacktestLauncher`）+ 进度/取消/产物 | 进度轮询 `is_alive()/exit_code()`；取消 `kill_tree()`；产物多路径回退定位 | **[B]** 任务状态机 + 对象存储 + SSE 进度；取消=服务端信号+协作式中断；崩溃宽限→worker 心跳超时 | 进程/文件机制整体重写；PNG base64 内联 → 返回 URL | P1 | 回测产物通过 URL 访问（非 base64 内联）；取消后任务 `cancelled` 且产物可保留 | 已测（见 test_f703_artifacts.py，3 测：完成回测产物含 report.json/csv 经 `GET /api/backtest/{id}/artifacts/{name}` URL 解析（非 base64）；取消后任务 cancelled 且 `*_progress.csv` 保留可下载；未知产物/目录穿越 404。新增 backtest 适配器产物存储 + `GET .../artifacts{,&/{name}}` 端点） |
| F-704 | 应用因子 IC 结果 | 回测页「④ 应用因子IC回测结果」（B-20） | 读取 `bt_factoric_report.json` → `save_factor_stats` 按 profile 路由写回 | **[B] 后端直接更新 `factor_profiles[fp].factor_configs[f]`**（**不走桌面 `save_factor_stats`**——§8.3.5 已记 R1/R2 覆盖式写 raw 残余风险，H5 重写为引擎能力，走 DB 事务+版本号 CAS） | 覆盖式写 raw 依赖调用方预切分 → 事务+版本号 CAS | P1 | 提交因子IC结果后 `factor_profiles[fp].factor_configs[f]` 实际更新；返回 `updated`=真实写入因子数；并行写冲突返回 409 | 已测（见 test_f704_ic_apply.py，4 测：`POST /api/backtest/ic-apply` 后 factor_configs[f] 落库、返回 updated=真实写入数、未提交键不动；profile 不存在 404；持旧版本并发 CAS → 409 VERSION_CONFLICT + `X-Latest-Version` 头。走 repo.update_profile_cas 乐观锁） |
| F-705 | 前端回测交互 | 回测页 `btLog`/`btResult` + 打开产物按钮（B-21~B-24） | 日志流 + 结果渲染 + `open_backtest_file`（`os.startfile` 本机打开产物） | **[B]** 日志流 SSE 追加到 **`btLog`**；结果在线渲染（PNG `<img>`/CSV 表格）；产物下载；**弃用 `os.startfile`** | `os.startfile`（系统默认程序打开）无浏览器等价 → 在线预览+下载 | P1 | 前端存在元素 `btLog`/`btResult`（**非 `#bt-log`/`#bt-result`**）；SSE 日志限最近 300 条；打开产物触发下载 | **已测**（2026-09-15，`web/src/api/backtest.spec.js` 10 测 + 完整前端套件 57 测通过：`BacktestView.vue` 含 `btLog`/`btResult`（SSE log 追加、限最近 300 条，产物经 URL 在线预览 + `a[download]` 触发下载，弃用 `os.startfile`）；路由 `/backtest` 已挂载。后端 F-105/F-701~704 均已测，链路端到端可用） |
| F-801 | 监控守护 | 后台守护线程（`start_monitor`）；配置 `monitor.json` | 按交易时段 + interval 网格定时跑自选诊断；形态 A 需 app 开着才生效 | **[B]** 后端定时任务（APScheduler）+ 交易时段判定复用 sessions；配置落 DB；去重状态由 `monitor_state.json` 落 DB | 后台线程 → 后端定时；边界「app 开着才生效」消失但要求后端 7×24 在线；多用户各自 sessions 需按维度调度 | P2 | 股票 session `[570,690]` 内到达网格点触发一次诊断；周末/休市不触发；去重后反复触发不重登 | 未测 |
| F-802 | 执行模型 | 监控执行模型（后台）+ 推送 | 按槽位跑诊断、去重键 `{market}:{direction}`、触发信号入册并推送 | **[B]** 后端调度 + 去重落 DB；通知送达走 **webhook（企微/钉钉/飞书）** + 页内轮询红条（`F#1` 已定案：不做 PWA Web Push） | 系统托盘/关 app 收推送 → ~~PWA Web Push（iOS 16.4+ 且「添加到主屏幕」）~~ 改 **webhook 转达 + 页内轮询红条**（`F#1` 定案：不做 PWA Web Push） | P2 | ~~PWA 已安装且授权通知后，页面关闭仍收到推送事件~~（已随 `F#1` 定案撤回）；`{market}:{direction}` 文案不变不重推；**已定案：走 webhook + 页内轮询，不做 Web Push** | 未测 |
| F-901 | 渠道 | 通知设置面板 `#notifChannels` | 4 渠道（inapp/wechat/dingtalk/feishu）；webhook POST text | **[B]** webhook **必须经后端转发**；`inapp` 气泡改前端 toast/轮询 | 本地直连第三方 webhook（CORS+凭据泄露）→ 后端转发 | P2 | 触发信号后后端向启用的 webhook 渠道 POST text；绕过后端无法直连渠道 | 未测 |
| F-902 | 配置与测试 | 通知设置面板 + 测试按钮 | 只更新提供 key；凭据存本地；`test_notifier` 仅启用渠道才推 | **[B]** 保存（凭据加密存储）+ 服务端发起测试推送（异步+超时） | 明文 `credentials.json` → 服务端加密；同步请求阻塞 → 异步化 | P2 | `POST /api/notifier/{channel}/test` 仅在渠道 enabled 时真发；`GET` 配置不回显明文凭据 | 未测 |
| F-1001 | A 股 K 线源 | 数据源设置面板 `#dsCards` + 测试结果 | 源切换 tencent/sina；`reset_*` 清相关缓存 | **[B]** 数据源 = **服务端全局配置**（运维动作）；**✅ 已定稿（`F#8`）= 不允许普通用户切换**，服务端全局配置即最终形态，不提供按用户维度表 | 源选择从个人偏好变服务端容量策略；`tencent` 无分钟级约束需保留 | P2 | `GET /api/data-sources` 返回当前 `kline_source`（普通用户只读）；**运维调整**后旧源缓存被清除；普通用户 `PUT /api/data-sources` 返回 403 | 未测 |
| F-1002 | 期货 K 线源 | 数据源设置面板（期货） | 期货源 eastmoney/sina；主力连续跳过东财；`test_futures_data_source` 探针 `rb0` | **[B]** 服务端统一代理 + 修 `rb0` 探针缺陷（探测与实际生效源一致）；维护 3 交易所 secid 映射 | 探针「测试结果」≠「实际生效源」（既有逻辑陷阱）→ 修正 | P2 | `test_futures_data_source` 对 `rb0` 判定与 `_ordered_sources` 实际生效源一致（不再恒走新浪） | 未测 |
| F-1101 | 数据与入口 | 复盘视图「执行与复盘/记一笔」页签；市场分流 全部/A股/期货（`index.html:1904-1958`） | 账本存 JSON；冷却全局跨市场；刷新现价全量逐条拉 | **[B]** 账本落 DB（`signal`/`cooldown` 表）按用户隔离 + 现价刷新批量/缓存 | JSON 文件并发写丢数据 → DB；全量刷新 N×M 外部请求放大 | P2 | `GET /api/ledger` 返回信号+冷却（冷却全局跨市场）；现价为批量接口（单次请求 N 只） | 后端已测（前端待） |
| F-1102 | 信号来源（只 3 项） | 分析/期货分析/监控（后台） | 全仓仅 3 入口落册；**全市场扫描不落信号**（口径硬约束） | **[B]** 信号落册逻辑保留后端（与 F-301/302/401 同源）；前端不做信号生成 | 口径「只 3 项」必须保持；易在批量诊断误挂 → 加白名单校验 | P2 | 扫描完成后 `signal` 表 0 新增；analyze/期货分析/监控 3 入口方可新增 | 未测 |
| F-1103 | 落册判定 | `_maybe_log_signal`（后台） | 按报告 `entry_tier`/`entry_action` 判定；冷却激活（active）不入新信号；`threshold_pct=-8.0`/`duration_days=3` | **[B]** 后端判定 + 落库；冷却状态随分析响应返回（保持 `discipline` 字段契约） | 判定依赖**文案前缀匹配**（`startswith('🔴 清仓')`）-文案一改即失效 → 改结构化枚举 | P2 | 报告返回 `discipline` 字段；cooldown active 时 analyze 不再新增信号 | 未测 |
| F-1104 | 账本统计 | 复盘顶部 4 指标（`index.html:1911-1958`） | 4 指标：纪律盈亏/执行数/情绪化差合计/未按纪律数；情绪化差「没赚就是亏」 | **[B]** 统计在服务端 SQL 聚合（或按需下发明细前端算） | 情绪化差定义反直觉（未执行→涨记亏跌记盈）须保持口径 | P2 | `GET /api/ledger/summary` 返回 4 指标；未执行信号涨→记亏、跌→记盈 | 后端已测 |
| F-1105 | 信号表 | 信号表（时间/标的/类型/比例/触发价/现价/触发至今/情绪化差/执行/操作） | 行内登记执行（`executed/exec_price/exec_ratio`）；刷新现价 | **[A]** 表格渲染 + 行内编辑，做**乐观更新+失败回滚**；分页游标 | 行内编辑请求成功后重载列表丢滚动位置 → 乐观更新 | P2 | 登记执行 `executed=true,exec_price,exec_ratio` 后情绪化差按规则重算；写失败回滚不丢数据 | 后端已测（前端乐观更新待） |
| F-1201 | 数据与入口 | 复盘→记一笔；分析页「记一笔情绪操作」按钮 `openEmotionModal`（`index.html:1042`） | 情绪记录存 JSON；`op_price` 锁定不可改；刷新现价逐条 | **[B]** 记录落 DB（`emotion_record` 表）+ 现价刷新批量/缓存（限最近 50 条） | JSON 并发写 → DB；`op_price` 防止前端覆盖 | P2 | `POST /api/emotion` 后 `op_price` 不可被前端覆盖；现价批量刷新为单次请求 | 后端已测（前端入口待） |
| F-1202 | 仪表盘 | 复盘→记一笔（`index.html:1961-1995`） | 前端统计：记录数/合计浮动差/最常见情绪（`emoTopTag`） | **[A]** 纯前端统计（遍历 records）或下推 SQL（`GROUP BY tag ORDER BY COUNT(*) DESC`） | 记录多前端 O(n) 卡顿；「最常见」tie-break 未定义 | P2 | 仪表盘 3 数值与记录数组的 sum/mode 计算结果一致；`emoTopTag` 与 `COUNT(*)` 降序一致 | 后端已测（前端展示待） |
| F-1203 | 记录表 | 复盘→记一笔（`index.html:1961-1995`） | 列表 时间/标的/操作/情绪/操作价/现价/情绪化差/操作；删除/刷新 | **[A]** 表格渲染 + 删除/刷新（移动端误触需二次确认）；`limit=200` 加「加载更多」 | 默认上限 200 超限不可见 → 分页 | P2 | 列表每行可删除/刷新；删除需二次确认；超过 200 条有分页入口 | 后端已测（前端二次确认/加载更多待） |
| F-1204 | 情绪标签与情绪化差口径 | 后端口径表 + 前端展示 | 标签常量（追高/杀跌…）；buy/sell→买入/卖出；差值=操作价 vs 现价方向化 | **[A]** 口径表单一来源（后端下发常量），前端只显示 / 纯计算 | 前后端各算一套 → 口径分叉（表内差值≠合计） | P2 | 前端展示差值 = 后端口径（买/卖方向符号一致），表内合计数与明细一致 | 未测 |
| F-1301 | 步骤模型与接口 | 侧栏「快速起步」/首启自动 | **4 步（以源码为准）**：①选因子 ②设方向 ③状态分界 ④仓位管理；**不含回测/IC** | **[B]** `/api/wizard/steps` 返回步骤定义 + 完成态；前端按 `action` 跳转对应 pane | 规格旧版「4 步=用法模式/回测/应用IC」与源码不符，以源码为准；前端 3 处死分支需清理 | P3 | `GET /api/wizard/steps` 返回恰 4 步（select_factors/set_direction/set_threshold/set_position），不含回测/IC | 未测 |
| F-1302 | 状态分界文案约束 | 向导「③ 状态分界」步（`set_threshold`） | 7 档文案「强势/标准/试探/观望/反弹/恐慌反弹/顶部反转」逐字一致 | **[A]** 静态文案常量（前端渲染+后端阈值键一一对应，集中管理中英映射） | 文案与后端阈值键不一致 → 状态分界设置与向导说明对不上 | P3 | 向导阈值步渲染 7 档文案逐字一致；每档与后端阈值键（strong…top_reversal）一一对应 | 未测 |
| F-1303 | 首启自动弹出与前端联动 | 首启自动（`maybeAutoOpenWizard`） | `is_first_run()` → 900ms 延迟 `openWizard`；`action` 驱动跳转 | **[B]** `/api/wizard/state` 返回 `first_run`；首帧后延迟弹出（`requestIdleCallback` 替代固定 900ms） | 强依赖 `window.pywebview`（提前 return → 向导永不弹）→ 改 HTTP | P3 | 首次登录且 `first_run=true` 自动弹出向导；再次进入不弹（服务端 `wizard_shown` 标记） | 未测 |
| F-1401 | 接口/机制/状态 | 设置→授权管理 | 授权状态全部由服务端返回；`run_startup_check` 冻结态校验 | **[B]** 后端账号体系 + JWT；`/api/license/info`；`run_startup_check` 删除 | 本地验签/状态文件 → 服务端状态 | P2 | `GET /api/license/info` 返回账号级授权状态；响应不含 `machine_id` | 已测 |
| F-1402 | 试用机制 | 首次启动（7 天试用） | 默认 7 天试用，首次启用起算（注册表兜底） | **[B]** 服务端按账号 **首次注册时间**（`F#2` 已定案）判定；换设备不重置；试用判定服务端完成 | 注册表兜底/本机时钟 → 服务端账号注册时间权威（防篡改、换设备不清零） | P2 | 试用到期的账号返回授权状态=到期且不可用；换设备试用不清零；**✅ 已定案：首次注册时间（账号维度）；换设备不重置；迁移补偿 30 天** | 已测 |
| F-1403 | 激活码与机器绑定 | 设置→授权管理（激活码输入） | Ed25519 验签 + 硬件机器指纹（MAC→sha256）绑定 | **[E→替代]** 账号 + **服务端设备绑定**（设备ID=服务端下发 token，HttpOnly Cookie，**绑定上限 N=2**）+ 异常登录检测 | **机器指纹→账号+设备绑定**（浏览器无硬件指纹 API，`uuid.getnode()` 不存在） | P2 | **第 3 台设备绑定被拒返回 403（`F#3` 已定稿 N=2）**；清 Cookie 后需重新绑定 | 已测 |
| F-1404 | 额度控制 | 分析/诊断/扫描/回测前置（`can_use/consume_use`） | **订阅制（`F#4` 已定稿）：包月/包年订阅期内不限次；试用期同样不扣减次数；无按次计量**（桌面「10 次/日」「整批扣 1 次」「预扣不退」口径整体退役） | **[E→替代]** **必须服务端判定**：网关前置**订阅/试用有效性检查**（只读判定 + 到期拦截），**不实现按次扣减**（无 `quota-1` 写入 → 天然幂等、无并发竞争） | 本地可信存储（HMAC/注册表）可被清 storage/改时钟 → 服务端判定不可绕过 | P2 | 订阅/试用有效期内**不限次**（无「扣到 0」分支）；**无有效订阅且试用到期 → 返回「授权过期」403**；授权状态由服务端判定，前端不可绕过 | 已测 |
| F-1405 | seal 反破译与打包保障 | 启动自校验 + 打包 `build_webview.spec` | seal 防篡改（fail-closed）；`datas`/`excludes` 白名单 | **[E→替代]** 核心算法**后端托管**，前端只留薄壳；目标从「保护客户端代码」改「**不下发核心代码**」；安全由服务端校验兜底 | H5 前端代码必然全量下发，JS 混淆非防护 | P2 | 前端构建产物不包含评分/回测引擎算法实现；核心计算接口仅存后端 | 已测 |
| F-1406 | 前端面板 | 设置→授权管理面板（`index.html:1673-1706`） | 状态/到期/剩余次数/机器码（只读+复制）/激活码输入/激活/注销 | **[B]** 面板结构复用；机器码→设备名/设备 ID；激活码→账号登录/续费入口 | 「剩余次数 /10」HTML 硬编码 → 服务端下发**订阅状态与有效期**；`status==='trial'` 死分支清理 | P2 | 面板显示账号/**订阅状态**/到期时间（服务端下发，非硬编码 `/10`）；「机器码」区块换「设备」 | 已测 |
| F-1501 | 子页签结构 | 关于（view-about）4 子页签（`ab-manual/ab-eval/ab-agree/ab-disclaim`） | 4 子页签 + 版本信息占位；文档失败永久「加载中」（无失败态=缺陷） | **[A]** 4 子页签 1:1 复用；文档内容 F-1502 提供；**加错误态与重试** | 加载失败卡「加载中」→ 错误态+重试 | P3 | 4 子页签可切换；文档加载失败显示错误态而非永久「加载中」 | 未测 |
| F-1502 | 文档读取与版本信息 | 关于页文档容器 + 版本信息 | 读 `docs/*.md`（使用说明书/评测/协议/免责，合计≈34KB）回退内嵌兜底文本 | **[A]** 文档**静态资源化**（`fetch` + 轻量渲染）或 `/api/docs`；**保留内嵌兜底文本**；建议保持纯文本渲染（零风险） | `innerHTML` 直填 → 若引 Markdown 需 sanitize（XSS） | P3 | 4 文档经 `fetch` 渲染；文件缺失时显示内嵌兜底文本而非 404 白屏 | 未测 |
| F-1503 | 合规文案约束 | 全站（工具定位/免责文案/报告 narrative） | 「技术分析工具不是投资顾问」；UI 禁「推荐/必涨/建议加仓」；免责 5 条 | **[A]** 纯文案静态渲染 + 文案合规清单进 CI | 合规文案**逐字保留**（法律文本勿改写）；新功能触碰禁区入 code review | P3 | 合规文案节点逐字与桌面一致；前端源码 grep 无「推荐/必涨/建议加仓」 | 未测 |
| F-1601 | 存储与接口 | 设置→用法设置 | `usage.json` 默认 basic；`_is_basic_mode()` 被大量后端逻辑消费 | **[B]** 模式落 DB（用户维度）或随 JWT claim 下发（服务端从 token 读）；默认 basic 必须保持 | 默认 basic 决定新用户「因子休眠」——迁移后需保持一致，否则体验断裂 | P3 | `GET /api/usage` 默认返回 basic；改 advanced 后扫描排序口径切换为 `final_score` | 未测 |
| F-1602 | 两模式定义 | 设置→用法设置（`index.html:1827-1874`） | basic 初级（因子休眠/纯技术定档）、advanced 高级（完整因子化） | **[A]** 前端两模式定义常量 + 门控 | 「能展示」门控 ≠ 后端接口门控，需双端一致 | P3 | 切换模式后因子休眠/完整因子化状态与定义一致 | 未测 |
| F-1603 | 门控影响点 | 初/高级门控（前端 CSS + 后端 7+ 处接口） | 门控点分散：因子隐藏/扫描排序/技术过滤/期货禁用/回测禁用/因子校验跳过 | **[B]** 门控点列表化，后端下发「能力清单」给前端；或只做后端（接口返回 `disabled`） | 任意一处漏改 → 「界面禁用但接口可调」或反之 | P3 | 初级时前端隐藏因子 + 接口禁用（回测 403、期货扫描 4xx、因子筛选跳过校验）全部生效 | 未测 |

## 二、按优先级统计

| 优先级 | 条数 | 内容范围 |
|---|---|---|
| P0（行情/K线/分析/报告） | 9 | F-201~204、F-301~305 |
| P1（诊断/扫描/方案/回测/回测协议） | 20 | F-105、F-401~404、F-501~506、F-601~604、F-701~705 |
| P2（授权/监控/通知/纪律/情绪/数据源） | 22 | F-104、F-801~802、F-901~902、F-1001~1002、F-1101~1105、F-1201~1204、F-1401~1406 |
| P3（启动/向导/关于/用法模式） | 12 | F-101~103、F-1301~1303、F-1501~1503、F-1601~1603 |
| **合计** | **63** | |

## 三、按可行性统计

| 可行性 | 条数 | 优先级分布 |
|---|---|---|
| A（纯前端） | 15 | P0=1（F-304）、P1=6（F-603/604/702 等）、P2=6、P3=2（F-103/1501…）；详见清单 |
| B（需后端） | 43 | P0=8、P1=14、P2=13、P3=7（**含 F-802，随 `F#1` 由 C 改判 B**） |
| C（需 PWA） | 0 | —（原 F-802 已随 `F#1` 定案「不做 PWA Web Push」改判 **B**） |
| D（需混合壳） | 0 | — |
| E（不可行→替代方案） | 5 | P3=2（F-101/102）、P2=3（F-1403/1404/1405） |
| **合计** | **63** | |

> 精确分级（源自 h5-feasibility §4.1，**2026-09-12 随 `F#1` 更新**）：**A=15**（F-103/304/404/603/604/702/1105/1202/1203/1204/1302/1501/1502/1503/1602）；**C=0**（原 F-802 已随 `F#1` 定案改判 **B**）；**E=5**（F-101/102/1403/1404/1405）；其余 **B=43**。总 15+43+0+5=63 ✅。
> **偏离说明**：`h5-feasibility.md`（C1）为**快照不追更新**，其 C=1 保持原样；本表按 `F#1` 定案更新，差异仅此一条。

## 四、阻塞清单（✅ 9 条全部关闭）

> 以下条目验收标准**缺关键业务参数无法定稿**，需产品/业务决策补齐，非 AI 可自定。若此清单为空则说明在编。
> **状态更新（2026-09-12）：原 9 条已由用户全部拍板定稿**（见 4.1）；4.2 现为「无开放项」。**

### 4.1 已关闭（9 条，2026-09-12 拍板 → 验收标准已定稿）

| # | 功能ID | 原缺字段 | ✅ 决策结果 | 定稿后的验收断言 |
|---|---|---|---|---|
| 1 | F-802 | 是否承诺「离线推送」（Web Push 走 PWA） | **不承诺**（`F#1`）：走 webhook（企微/钉钉/飞书）+ 页内轮询红条；不做 PWA Web Push | 页面关闭后由 webhook 渠道转达通知，页内以轮询红条提示；`{market}:{direction}` 文案不变不重推 |
| 2 | F-1402 | 试用起算基准：**首次注册** vs **首次本机使用** | **首次注册时间（账号维度）**（`F#2`）：换设备不重置；桌面版老用户迁移后额外赠送 30 天 | 试用到期的账号返回授权状态=到期且不可用；换设备不重置；迁移用户试用 = 注册时间 + 7 天 + 30 天 |
| 3 | F-1403 | 允许绑定设备上限数 | **N = 2**（`F#3`） | 第 3 台设备绑定被拒返回 403 |
| 4 | F-1404 | 额度计费时机 | **订阅制（包月 / 包年），不按次计量；试用同样不扣减次数**（`F#4`） | 订阅/试用有效期内不限次；无效期返回「授权过期」403 |
| 5 | F-501 / F-701 | 扫描 / 回测计费时机 | **同 #4（订阅制）**（`F#5`） | 提交不预扣、不计次，仅校验授权有效性 |
| 6 | F-502 | 扫描验收样本规模与耗时上限 | **样本 = 服务器 K 线缓存全量池；验收前提 = 命中热缓存**（`F#6`） | 全量池（≈5215 只 × 300 根日K）扫描完成至 99%+，耗时 ≤30 min（★P1 实测校准） |
| 7 | F-505 | 是否保留用户自定义板块映射 | **保留**（`F#7`） | 按账号隔离；合并优先级 **用户映射 > 内置 26 板块** |
| 8 | F-1001 | 数据源是否允许普通用户切换 | **不允许**（`F#8`） | 普通用户 `PUT /api/data-sources` 返回 403；仅运维/管理员可调 |
| 9 | F-101 | 会话冲突处置策略 | **踢旧**（`F#9`） | 新会话胜出，旧会话被强制登出（后续心跳/请求返回 401） |

### 4.2 无开放项

原 2 条开放项（F-802「是否承诺离线推送」、F-1402「试用起算基准」）已于 **2026-09-12** 随 `F#1` / `F#2` 拍板关闭，**已移入 4.1**；本节暂无待定稿条目。

> 注：**63 条**验收标准均为可断言表达式（原 54 条可断言 + 本次新定稿 9 条），无待补项。

## 五、平台差异合并清单

| 桌面能力 | H5 替代 | 影响的功能ID | 风险 |
|---|---|---|---|
| 右键（上下文菜单） | 长按 / 显式操作按钮 | F-1105（行内登记执行）、F-1203（删除/刷新）、F-601（方案列表编辑） | 移动端长按与滚动冲突，需显式操作按钮兜底 |
| 多窗口 / 单实例进程 | 路由 + 会话唯一 | F-101（会话互斥）、F-401/F-501（任务跨副本可见） | 多标签/多设备无法阻止；任务状态必须外置否则轮询落错副本 404 |
| 全局快捷键 / 原生模态弹窗（MessageBox） | 页内 toast/dialog | F-102（崩溃弹窗）、F-101（已在运行提示） | H5 无原生弹窗概念，全部改前端组件 |
| 本地文件读写（路径 + `open(path,'w')` + `os.startfile`） | 文件选择器 `<input type=file>` / `Blob` 下载 / 在线预览 | F-601（方案导入）、F-404/F-506（报告/CSV 导出）、F-705（回测产物打开）、F-304（决策卡落盘） | iOS Safari 无 File System Access、`a[download]` 支持不完整，需备复制剪贴板 + 下载兜底 |
| 系统托盘 / 关 app 后收通知 | ~~PWA + Web Push（Manifest/Service Worker）~~ **webhook（企微/钉钉/飞书）+ 页内轮询红条**（`F#1` 已定案：不做 PWA Web Push） | F-802（离线推送）、F-801（常驻守护） | ~~iOS 需 16.4+ 且「添加到主屏幕」；用户拒通知即失效~~ → **随 `F#1` 定案不再构成阻塞**，改由 webhook 转发 + 页内轮询红条兜底 |
| 后台守护线程 / 并行 worker | 后端定时任务 / 任务队列 + SSE | F-801（监控守护）、F-402/F-502（诊断/扫描 worker）、F-703/F-105（回测编排） | 「app 开着才生效」消失但要求后端 7×24；并发写需外置任务状态与行锁 |
| 机器指纹（`uuid.getnode()` MAC / 硬件密钥 / 注册表） | 账号 + 服务端设备绑定 + JWT | F-1403（激活绑定）、F-1404（额度计量）、F-1401（授权状态）、F-1406（面板）、F-104（`machine_id` 字段） | 绑定强度低于硬件指纹：清 Cookie/换浏览器即视为新设备；**授权状态必须服务端判定**否则可被绕过 |
| 源站直连（腾讯/新浪/东财，无 CORS） | 服务端数据代理 + 缓存 + 限流 | F-201/202/203/204/305、F-1001/1002 | 浏览器直连必被 CORS 拦；代理放大源站请求需限流与合规评估（h5 §4.5#3） |
| 本地进程级全局缓存 / 内存任务字典 | 请求级/租户级缓存 / DB 外置状态 | F-602（配置缓存）、F-401（诊断任务）、F-501（扫描任务） | 多副本/多用户并发互相冲掉配置或串市场，必须先修全局状态再扩副本 |
| 服务端本地持久化路径（`%LOCALAPPDATA%` / `get_app_dir()`） | 后端对象存储 / DB + 前端 OPFS | F-304/404/506、F-703（回测产物）、F-1101/1201（账本） | 需统一产物 URL、跨副本单一来源；账本 JSON 改 DB 防并发丢数据 |

## 六、前端组件清单（供 C3 参考，按模块分组）

> 复用指「搬运 `ui_mockup/index.html`（7949 行）结构 + ECharts」；新建指 H5 需新造。

| 模块 | F-xxx | 新建 / 复用组件 |
|---|---|---|
| 行情/K线 | F-201~204、F-305 | 顶部跑马灯（复用结构 + CSS 动画）；实时行情面板（复用 `#qPrice` 等渲染）；**K线图组件（复用 ECharts，先改造 4 处 `echarts.init`）**；股票搜索 autocomplete（防抖 250ms + datalist 或自建下拉）；仅行情查询组件 |
| 个股分析 | F-301~303、F-305 | 分析页查询栏；**报告渲染区（透传 `reportData`+`report_text`，懒加载）**；量化报价面板 |
| 决策卡 | F-304 | **决策卡组件（`html2canvas` 本地化：CDN→本地依赖 + `toBlob`→`a[download]`）**；固定 Google Fonts 本地/fallback |
| 批量诊断 | F-401~404 | 诊断页列表 + 参数表单；**SSE 进度组件**；五组分类结果表；报告导出（Blob/剪贴板） | ✅ **前端已建（2026-09-13）**：`web/src/views/DiagnosisView.vue`（参数表单 + 开始/取消 + SSE 进度条 + 五组汇总卡 + 按组结果表 + 接入 `DiagnosisReport` 导出）；`web/src/api/diagnosis.js`（fetch+EventSource 客户端，相对 `/api` 走 vite 代理）；`router/index.js` 增 `/diagnosis` 路由；`App.vue` 增固定顶部导航。后端 `GET /api/diagnosis/{id}` 已补回 `results`（逐只明细），配套 `tests/h5_skeleton/test_diagnosis_results_exposed.py` + `web/src/api/diagnosis.spec.js`（6 测）。F-401~404 后端→前端端到端可用。｜**分支 A 桌面壳接入（2026-09-13）**：`server/main.py` 加 `_mount_spa`（env `MBULL_SERVE_SPA=1` 时同源托管 `web/dist` 静态资源 + SPA fallback，默认关、零回归；`/api` 未知路径保留 404 JSON）；`main_webview.py` 加 `MBULL_H5_SPA=1` 守卫（内嵌线程起 uvicorn + 加载 `http://127.0.0.1:8000/`，保留原 `file:// ui_mockup` 默认路径）；`tests/h5_skeleton/test_spa_serving.py`（2 测）。`build_webview.spec` 打包链改造（带 `web/dist`、去 `ui_mockup`、加 `server` 模块）待后续单独任务 |
| 全市场扫描 | F-501~506 | 扫描参数表单；**SSE 进度组件**；**结果虚拟滚动表格（分页 200/页，剥离 `tech_snapshot`）**；板块面板（`sectorMinScore/sectorMinCount`/刷新/编辑/模板）；导出/暂停/取消组件 |
| 量化配置 | F-601~604 | **配置页 8 子页签组件**：q-position（**约 94% 为 `fillPositionList()` 动态生成**）、q-weight（IC 加权/归一化/等权）、q-stats、q-penalty、q-threshold（`renderAndEditor` AND 编辑器 + 否决项面板）、q-backtest（表单 + **`btLog`/`btResult`**）、q-ghost（**100% 动态 `fillGhostList()`，显式保留 `ma_confirm` 空值→`sma_20` 不落盘语义**）；方案管理下拉 + 导入（`file.text()`）；初/高级门控（`data-usage`，首帧 SSR 注 basic 消闪烁） |
| 回测 | F-701~705 | 回测参数表单（A股/期货组 + 共用 key 隔离）；**SSE 日志组件（限 300 条）**；结果指标卡（strategy/scoreic/factoric 分模式）；产物在线预览（PNG `<img>`/CSV 表格）+ 下载 |
| 监控/通知 | F-801~802、F-901~902 | 监控配置表单（间隔/时段）；通知渠道设置面板（`#notifChannels`）；~~**Web Push / 通知权限申请组件**~~（`F#1` 已定案：不做 Web Push）；应用内 toast 红条（InAppNotifier） |
| 数据源 | F-1001~1002 | 数据源设置卡片（`#dsCards`）+ 测试结果 + 保存（只读展示或按决策开放切换） |
| 复盘/情绪账本 | F-1101~1105、F-1201~1204 | 复盘 2 子页签 + 市场分流；**信号表（乐观更新 + 分页游标）**；账本统计 4 指标卡；**情绪记录表（二次确认删除 + 「加载更多」）**；情绪记录弹窗（`openEmotionModal`） |
| 向导 | F-1301~1303 | **步骤向导组件（4 步：选因子/设方向/状态分界/仓位管理，按 `action` 跳转）**；首启自动弹出（`requestIdleCallback` 或数据就绪事件） |
| 授权 | F-1401~1406 | 授权/账号面板（设备绑定**上限 2 台**、**订阅状态**、到期时间——服务端下发非硬编码）；激活码输入改账号登录/续费入口 |
| 关于/用法模式 | F-1501~1503、F-1601~1603 | 4 子页签文档渲染（纯文本零依赖或 markdown+sanitize）；错误态+重试；用法模式设置面板；两模式门控 |
| **通用基础设施** | 全部 | **HTTP/WS/SSE 客户端层（替换现有 26 处 `window.pywebview.api.*`，其中 5 处关键桥接 `_js_error`/`get_wizard_steps`/`run_wizard_step`/`is_first_run`/`save_decision_image`）**；错误上报组件（`window.onerror`+`unhandledrejection`→`POST /api/js-error`）；心跳上报（5s）；toast/确认对话框；SSE 断线重连 + 任务终态补偿；前端路由 |

## 七、完成后自检

| # | 自检项 | 结果 | 说明 |
|---|---|---|---|
| 1 | 行数 = 63 | ✅ | 第一节矩阵共 63 行（F-101~F-1603），覆盖索引核对无缺漏 |
| 2 | 每个 P0 条目验收标准可断言 | ✅ | P0 共 9 条，验收均为可断言表达式，不依赖未决业务参数 |
| 3 | 阻塞清单状态 | ✅ **9 条 → 9 条全部关闭**（见四章） | 2026-09-12 拍板关闭 9 条（F-1403 / F-1404 / F-501·F-701 / F-502 / F-505 / F-1001 / F-101，以及 `F#1`→F-802 / `F#2`→F-1402），全部验收标准已由「待补齐」转为可断言表达式，**4.2 无开放项** |
| 4 | E 类 5 条 H5方案 = 替代方案 | ✅ | F-101/F-102/F-1403/F-1404/F-1405 的 H5方案列均标 `[E→替代]`，写替代方案（源自 C1 §4.3），非「迁移现有实现」 |
| 5 | F-603 验收引用《附录-配置页控件清单》 | ✅ | F-603 验收标准显式引用附录（P-01~P-14 / B-01~B-28 / G-01~G-07，含 `ma_confirm` 语义），非泛写「页签能渲染」 |
| 6 | F-704 H5方案 = 后端直接更新 `factor_profiles[fp].factor_configs[f]` | ✅ | 显式写「后端直接更新…不走 `save_factor_stats`」，理由注明 §8.3.5 R1/R2 |
| 7 | F-1301 以源码为准（4 步） | ✅ | 业务规则与验收均按源码 4 步（选因子/设方向/状态分界/仓位管理），标注规格旧版不一致 |
| 8 | F-705 id 用 `btLog`/`btResult` | ✅ | F-603/F-705 验收均用无连字符 id 并标注「非 `#bt-log`/`#bt-result`」 |

---

## 八、决策记录（追加）

- 2026-09-12：F#1 定案【不承诺离线推送】——webhook + 页内轮询；不做 PWA Web Push；R-05 风险消除
- 2026-09-12：F#2 定案【首次注册时间】——账号维度起算，换设备不重置；迁移补偿 30 天