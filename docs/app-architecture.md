# M-Bull H5 架构设计（C3）

> 产出时间：2026-09-12 ｜ **v1.2（2026-09-12 决策冻结：形态 + 8 项待拍板 + `F#1`/`F#2` 全部关闭，合计 12 项）**
> 依据：`docs/h5-feasibility.md`（C1，63 条分级判定）+ `docs/feature-matrix.md`（C2，逐条矩阵/阻塞清单/平台差异/组件清单）

## 决策状态速览

| 决策 | 结果 | 影响 |
|---|---|---|
| **`C1#1` 授权模式** | ✅ **分支 A** —— 账号 + 服务端设备绑定 | **形态冻结 = `H5 + 后端`**，不引入混合壳；分支 B 转存档 |
| **`C1#3` 数据源合规** | ✅ **免费 API + 可插拔适配层** | 保留现状源（通达信/腾讯/新浪/东财），新增统一接口 + 注册表 + 降级链；**合规风险为接受态**（§7.3） |
| **`C1#2` 成本模型** | ✅ **规格 M**（中等并发：300–500 DAU / 30–50 并发任务） | 部署按 §7.2 规格 M；**扩副本前置 = R-09 全局状态全部外置** |
| `F#3` 绑定设备上限 N | ✅ **N = 2** | F-1403 验收 = 「第 3 台设备绑定被拒 403」 |
| **`F#4` / `F#5`** 计费时机 | ✅ **订阅制（包月 / 包年），不按次计量；试用期同样不扣减次数** | 「预扣不退 vs 成功后计费」之争**消解**；准入改为「订阅/试用有效性 + 到期拦截 403」 |
| `F#6` 扫描验收样本 | ✅ **服务器 K 线缓存全量池**（≈5215 只 × 300 根日K），验收前提 = 命中热缓存 | 样本规模不再是瓶颈变量；耗时上限沿用 ≤30 min（★需实测校准） |
| `F#7` 板块映射 | ✅ **保留用户自定义** | `sector_map` 按账号隔离表落地；**用户映射 > 内置 26 板块** |
| `F#8` 数据源切换 | ✅ **不允许普通用户切换** | 服务端全局配置为最终形态；`PUT /api/data-sources` 收权限（普通用户 403） |
| `F#9` 会话冲突 | ✅ **踢旧**（新会话胜出，旧会话强制下线） | F-101 验收由 `role='readonly'` 改为「旧会话被强制登出」；复用 F-102 心跳通道下发 revoke |
| `F#1` / `F#2` | ✅ **已全部拍板**（2026-09-12，**全 12 项冻结**） | `F#1` = **不承诺离线推送**（webhook + 页内轮询红条，不做 PWA Web Push，§2.7）；`F#2` = **试用按首次注册时间（账号维度）起算**，换设备不重置、迁移补偿 30 天 |
> 本文只做架构决策，**不含代码**。所有决策给「推荐 + 理由 + 备选 + 落地依据」，不确定项标 **★需验证**。
> 原型基线：`ui_mockup/index.html`（7949 行）+ `ui/web_api.py`（60+ pywebview 方法）+ `engine/*`（评分链 22279 行）

---

## 〇、编号约定（先读，避免串味）

本仓库存在**两套独立的编号体系**，C3 同时引用两者，必须显式区分：

| 体系 | 位置 | 内容 | 本文记法 |
|---|---|---|---|
| **F 阻塞清单** | `feature-matrix.md` **§四**（L104-114），9 条 | #1 F-802 离线推送／#2 F-1402 试用起算／#3 F-1403 绑定上限／#4 F-1404 计费时机／#5 F-501·F-701 计费时机／#6 F-502 扫描验收／#7 F-505 板块映射／#8 F-1001 数据源切换／#9 F-101 会话冲突 | `F#4`、`F#5` |
| **C1 待确认清单** | `h5-feasibility.md` **§4.5**（L414-423），6 条 | #1 授权模式／#2 回测·扫描成本模型／#3 数据源合规性／#4 是否承诺离线可用／#5 F-1301 以哪一版（已闭环）／#6 多设备并发编辑冲突 | `C1#1`、`C1#2` |

> **关键区分**：两套的 `#1~#3` 语义完全不同（`F#1`=离线推送 vs `C1#1`=授权模式）。
> C3 必处理的三条 = **`C1#1` / `C1#2` / `C1#3`**；C3 只列不给方案的六条 = **`F#4`~`F#9`**。
> 跨文档引用先例：`feature-matrix.md:111`「（h5 §4.5#2）」、`:129`「（h5 §4.5#3）」。

---

## 一、形态决策

### 1.1 基线结论

**形态已冻结：`H5 + 后端`（不引入混合壳）。** `C1#1` 授权模式已于 **2026-09-12 由用户决策选定分支 A**（账号 + 服务端设备绑定），**形态不再是"推荐"而是"已定"**；分支 B（混合壳）**已否决，转入存档**（见 §1.2 / §7.1）。

判据（来源 `h5-feasibility.md` §4.2，L366-382）：E 类 = **5** 条，落在 **3–8** 区间 → `H5 + 后端`。

| 分级 | 条数 | 占比 | F-ID |
|---|---|---|---|
| A 纯前端 | 15 | 23.8% | F-103、F-304、F-404、F-603、F-604、F-702、F-1105、F-1202、F-1203、F-1204、F-1302、F-1501、F-1502、F-1503、F-1602 |
| B 需后端 | 43 | 68.3% | 见 §3.3 逐条清单（**含 F-802，随 `F#1` 由 C 改判 B**） |
| C 需 PWA | 0 | 0% | —（原 F-802 已随 `F#1` 定案「不做 PWA Web Push」改判 **B**） |
| D 需混合壳 | 0 | 0% | —（**前提已落实：`C1#1` 已选分支 A** → D 恒为 0；若日后商业侧推翻，需重判并修订本表，见 §1.2） |
| E 不可行 | 5 | 7.9% | F-101、F-102、F-1403、F-1404、F-1405 |

E 类 5 条**全部是"防护性/防呆性"能力**（防多开、防崩溃不可见、防一码多机、防额度绕过、防源码泄露），无一涉及业务逻辑；核心业务（行情/K线、评分、诊断、扫描、回测、账本、配置）100% 落在 A/B/C。**不存在导致功能砍掉的 E**（`h5-feasibility.md:394`）。

### 1.2 形态分支：A 已选定 / B 已存档

> **状态（2026-09-12 更新）：`C1#1` 已由用户拍板 → 选定「分支 A」。**
> 分支 A 为**生效架构**，本文其余章节均按分支 A 展开；**分支 B 保留为存档 + 风险登记**（记录"若商业侧日后推翻绑定降级决策"时的架构代价），**不再作为并列候选**。

#### 分支 A ✅ 已选定（2026-09-12 用户决策）：授权改「账号 + 服务端设备绑定」→ 形态 = `H5 + 后端`

**① 技术栈差异项**

| 项 | 分支 A 取值 |
|---|---|
| 客户端形态 | 纯 Web（浏览器 / PWA），**无原生壳** |
| 构建链 | 单一：Vite 产出静态产物 → nginx/CDN |
| 鉴权载体 | JWT（Access + Refresh），设备标识存 HttpOnly Cookie |
| 原生插件 | **不需要** |
| 发布通道 | 仅 Web 部署；无 App Store / 应用商店审核链路 |

**② 后端能力差异项**

| 能力 | 分支 A 要求 |
|---|---|
| 账号体系 | **必须新建**：注册 / 登录 / 找回 / 注销 / 实名（若合规需要） |
| 设备绑定 | **必须新建**：设备表、绑定上限校验（**N = 2**，`F#3` 已拍板）、清 Cookie 后的重新绑定流程 |
| 异常登录检测 | **必须新建**：同账号多地并发 → 告警 / 踢出（**与 `F#9` 会话踢旧同源逻辑**） |
| Ed25519 激活码 | **整体退役**（`license/license_manager.py:292-320` 验签路径废弃）；仅保留对**存量已售激活码**的一次性核销迁移，核销后转账号授权 |
| 授权状态源 | 服务端 `license` 表（F-1401「授权状态全部由服务端返回」） |
| 额度计量 | 服务端**订阅 / 试用有效性判定**（`F#4`/`F#5` 已拍板：包月 / 包年订阅，**不按次计量**；试用期同样不扣减次数）。原「服务端 DB 原子扣减次数」仅作为**历史口径**登记，H5 不再实现为按次扣减（与 A/B 分支相同，`C1#1` 不影响此项——授权状态本来就必须服务端化，`h5-feasibility.md:391`） |

**③ 数据模型差异项**

| 表 | 关键列 | 说明 |
|---|---|---|
| `account` | `id` / `identifier`(手机·邮箱) / `pwd_hash` / `status` / `created_at` | 账号主表 |
| `device` | `id` / `account_id` / `device_token_hash` / `ua` / `bound_at` / `last_seen_at` / `revoked_at` | 设备绑定，唯一约束 `(account_id, device_token_hash)` |
| `license` | `account_id` / `tier` / `status` / `expires_at` / `trial_first_use_at` / `sub_cycle`('monthly'\|'yearly') | **订阅制（`F#4`）：不建 `quota_daily`/`quota_used`/`quota_reset_at` 计次列**；试用起算基准见 `F#2`（**✅ 已定案 = 首次注册时间，账号维度**） |
| ~~`machine_id`~~ | **不下发、不存储** | F-104 响应字段 `machine_id` → `device_id`（`feature-matrix.md:16`） |

**④ 平台差异差异项**

| 桌面能力 | 分支 A 替代 | 强度评估 |
|---|---|---|
| 机器指纹（`uuid.getnode()` MAC → sha256，`license/license_manager.py:137-143`） | 账号 + 服务端下发 device token | **强度下降**：清 Cookie / 换浏览器即视为新设备；**无法防"一码多机"**，只能靠绑定上限 + 异常检测 |
| F-1403 分级 | 维持 **E**（`feature-matrix.md:66`） | — |
| F-1406 面板 | 「机器码（只读+复制）」区块 → 「设备列表（名称/绑定时间/解绑）」 | 文案口径变更 |

#### 分支 B 🚫 已否决（存档备查）：保留机器指纹 → 形态 = `H5 + 后端 + 混合壳（Capacitor/Tauri）`

**① 技术栈差异项**

| 项 | 分支 B 取值 | 与 A 的差异 |
|---|---|---|
| 客户端形态 | Web 产物 + **Capacitor**（iOS/Android）或 **Tauri**（桌面） | **+1 套壳工程** |
| 构建链 | Vite → Web 产物 → **原生壳打包**（Capacitor CLI / Tauri CLI） | 多一条构建/签名/发布链 |
| 原生插件 | **必须**：设备标识插件、安全存储（Keychain/Keystore）、推送插件 | A 不需要 |
| 发布通道 | **App Store / 应用商店 / 签名分发**（含审核周期与合规材料） | **与 A 的关键成本差** |
| 桌面侧 | 若用 Tauri 可**同时**保留桌面分发形态 | A 只有 Web |

**② 后端能力差异项**

| 能力 | 分支 B 要求 | 与 A 的差异 |
|---|---|---|
| 账号体系 | **仍需**（订阅/续费/多端），但**不是授权的唯一锚点** | A 中账号是唯一锚点 |
| 设备绑定 | 改由**壳提供的稳定标识**承担 | A 依赖服务端 token |
| Ed25519 激活码 | **保留**：`license/license_manager.py:292-320` 的验签逻辑**搬到服务端**（公钥服务端持有，验签在服务端完成；客户端不再持有公钥＝提升防破译强度） | A 中整体退役 |
| 服务端验签接口 | **新建**：激活码 payload 含机器标识 → 服务端验签 → 首次绑定落库 | A 无此接口 |
| 额度计量 | 服务端**订阅 / 试用有效性判定**（**同 A**；`F#4`/`F#5` 已拍板为订阅制，不按次计量） | 无差异 |
| 核心算法托管 | 后端托管（**同 A**，F-1405 仍为 E） | 无差异 |

**③ 数据模型差异项**

| 表 | 关键列 | 与 A 的差异 |
|---|---|---|
| `device` | `id` / `account_id`(可空) / `machine_id_hash` / `platform` / `first_seen_at` / `bound_at` / `revoked_at` | 唯一键从 `device_token_hash` 改为 **`machine_id_hash`**；支持无账号的先激活后绑账号 |
| `activation_code` | `code_hash` / `ed25519_payload` / `issued_at` / `redeemed_at` / `bound_machine_id_hash` | **A 中不存在此表** |
| `license` | 同上 + `bound_machine_id_hash` | 授权与机器绑定强关联 |

**④ 平台差异差异项**

| 项 | 分支 B 现实 | 强度评估 |
|---|---|---|
| 壳提供的设备标识 | Android：`Settings.Secure.ANDROID_ID`（Android 8+ 按签名+用户隔离，且**恢复出厂即变**）；iOS：`identifierForVendor`（**同厂商 App 全卸载后会变**）；Tauri：可读 OS 机器码 | **仍非硬件级指纹**——达不到桌面 `uuid.getnode()`（MAC）的稳定性，但**高于**浏览器 Cookie 方案 |
| F-1403 分级 | **E → D**（`feature-matrix.md:66` 记为 E→替代；分支 B 下需改判 D） | 见下方"统计联动" |
| `os.startfile` / 原生保存对话框 | 壳可提供原生桥接（可选的额外收益） | 但**非必需**：浏览器等价替代已足够（`a[download]` / 在线预览） |
| 推送 | 壳的原生推送通道（APNs/FCM）可**免去** PWA/iOS 16.4+ 限制 | 可缓解 `F#1`（F-802 离线推送）的阻塞 |

> **⚠️ 统计联动**
> `feature-matrix.md` §三 现为 **A=15 / B=43 / C=0 / D=0 / E=5**（**2026-09-12 随 `F#1` 更新：B 42→43、C 1→0，F-802 由 C 改判 B**）。
> **`D=0` 的前提是「`C1#1` 选分支 A」**——该前提已落实 → **D=0 / E=5 保持成立，F-1403 维持 E（不升 D）**。
> **`C=0` 的前提是「`F#1` 定案不承诺离线推送」**——该前提亦已落实 → **全仓不再有 C 类条目**（§1.1 上表已同步）。
> （存档备查：若日后改选分支 B，需 D 0→1、E 5→4；F-1404 / F-1405 是否一并升 D 需重判——初判仍为 E，因**授权状态必须服务端判定**、核心算法必须后端托管，壳无法替代。）

#### 两分支对比与共同项

| 维度 | 分支 A | 分支 B |
|---|---|---|
| 形态 | H5 + 后端 | H5 + 后端 + 混合壳 |
| 后端是否可省 | 否 | **否**（Python 引擎无论如何都必须上后端） |
| 客户端工程量 | 低 | 高（+壳工程 + 插件 + 发布链） |
| 防"一码多机" | 弱（靠绑定上限） | 中（壳标识 + 服务端验签） |
| 附带收益 | — | 原生推送 / 原生桥接（可选） |
| E 类条数 | 5 | **4**（+D=1） |
| **共同项（两分支完全一致）** | 技术栈（除壳）、后端能力中 9/11 项、数据模型中的配置/账本/行情部分、平台差异 §5 全部 10 条 | 同左 |

> **结论（已生效）**：后端是两分支的**共同底座**，混合壳是**增量**。`C1#1` 选定分支 A 后：
> ① **形态冻结为 `H5 + 后端`，不引入混合壳**；分支 B 的追加项（服务端 Ed25519 验签 + `activation_code` 表 + 壳工程）**不实施**；
> ② 后端按分支 A 架构实施，**9/11 项能力不受该决策影响**；
> ③ **`F#3`（绑定设备上限 N）由"分支内参数"升级为必须拍板项**——分支 A 下「第 N+1 台设备绑定被拒」的验收断言以前置 N 为条件（见 §八）。

### 1.3 备选形态（非推荐，登记备查）

| 备选 | 适用场景 | 代价 | 来源 |
|---|---|---|---|
| 备选 1：`H5 + 后端 + 轮询`（不上 SSE/Web Push） | 灰度期用户少、可接受 1.5s 轮询延迟、不要求离线通知 | 放弃 F-802 离线推送（降级 webhook + 页内轮询红条） | `h5-feasibility.md:380` |
| 备选 2：`H5 + 后端 + 混合壳` | **已否决**（2026-09-12：`C1#1` 选分支 A，本触发条件未成立） | 见 §1.2 分支 B 存档 | `h5-feasibility.md:381` |
| 备选 3：`H5 + 后端 + PWA 离线缓存` | 强需求"断网看历史" | 只能缓存最近 N 只；全市场扫描/回测**必须联网**；受 iOS Safari 存储驱逐限制 | `h5-feasibility.md:382` |

---

## 二、技术栈选型（12 项逐一决策）

### 2.1 前端框架

- **推荐：Vue 3 + Vite**
- **理由**：① 现有前端是 7949 行**单文件命令式 DOM 操作**（`ui_mockup/index.html`），模板语法与现有 HTML 结构最接近，迁移是"搬运 + 数据绑定"而非重写；② 8 配置子页签 + 4 关于页签 + 侧栏视图切换的层级结构（`index.html:1357-1365`、`:1879-1902`）用组件化收益明显；③ ECharts 有成熟 Vue 封装；④ Vite 构建产物体积可控。
- **备选**：React 18（团队熟悉 React 时优先，但 JSX 与现有 HTML 差异更大）；Svelte（产物最小，适合移动端，生态较小）；**继续无框架 + 原生 JS**（最小改造：只把 `window.pywebview.api.*` 的 26 处调用点换成 HTTP 客户端——`grep -c pywebview ui_mockup/index.html` = 26，关键桥接 5 处）。
- **落地依据**：`h5-feasibility.md` §5.1（L435-439）；`feature-matrix.md` §六 通用基础设施行（L152「HTTP/WS/SSE 客户端层（替换现有 26 处 `window.pywebview.api.*`…）」）。
- **必须一并处理的迁移项**：`html2canvas` 从 CDN（`index.html:7944`）改本地依赖；Google Fonts（`:12`）改本地或明确 fallback（否则脱网时决策卡样式漂移，影响 F-304）；`api = window.pywebview.api`（`:4626-4627`）替换为 HTTP/SSE 客户端层。

### 2.2 UI 库

- **推荐：Element Plus + 自建样式层（Tailwind 可选，不叠加）**
- **理由**：① Element Plus 与 Vue 3 同源（同一生态、同一文档体系），迁移期"组件替换"成本最低；② 现有 UI 是 **TradingView 风格深色骨架（#131722）+ A 股红涨绿跌（#ef5350/#26a69a）**，Element Plus 的 CSS 变量体系（`--el-*`）可直接覆盖为项目配色，**不需要**重写组件；③ 项目大量使用表格（扫描结果每页 200 行虚拟滚动、信号表、情绪记录表）、表单（8 子页签 49 条控件）、对话框（情绪弹窗/决策卡/确认框）——恰好是 Element Plus 的强项；④ 不选 Ant Design：其设计语言偏中后台浅色，与现有深色盘口风格冲突更大。
- **备选**：Ant Design Vue（组件更全但风格冲突大）；Tailwind 纯样式（灵活但表格/表单需自建，工作量反升）；**沿用现有原生 CSS**（7949 行内联样式，迁移中会与框架作用域冲突）。
- **落地依据**：`feature-matrix.md` §六——全市场扫描行「**结果虚拟滚动表格（分页 200/页）**」、复盘/情绪账本行「信号表（乐观更新 + 分页游标）」「情绪记录表（二次确认删除 + 「加载更多」）」、量化配置行「配置页 8 子页签组件」。
- **注意**：现有配色规范（骨架 `#131722`、红涨 `#ef5350`、绿跌 `#26a69a`）为**项目约定值，必须逐字节保留**，不得改用组件库默认色。

### 2.3 状态管理

- **推荐：Pinia**
- **理由**：① Vue 3 官方推荐，与 §2.1 同源；② 现有状态是"一个巨大的全局 `quantData` 对象 + 各 pane 的本地 DOM 状态"（`ui/config_mapper.py` 双向映射），Pinia 的 `store` 天然对应"一份 `quantData` 多 pane 共享"；③ 需要跨视图共享的状态有明确清单：方案（`currentSchemeName`）、用法模式（`usage` basic/advanced，被前端门控 + 后端 `_is_basic_mode()` 双端消费）、任务态（诊断/扫描/回测的 SSE 进度）、会话角色（`active` / `revoked`，`F#9` = 踢旧）。
- **备选**：Zustand（若选 React）；Redux Toolkit（样板重、收益不明显）；`provide/inject` + `reactive`（够用但缺 devtools 与持久化插件）。
- **落地依据**：`feature-matrix.md` §六 量化配置行（「**配置页 8 子页签组件**…数据由 F-602 读写」，8 页签共享一份数据）；F-604 行「8 子页签 + 4 关于页签 + 侧栏视图切换的层级结构」（`:40`）；F-101 行「新会话胜出、旧会话被强制登出」（`:13`，`F#9` = 踢旧）。

### 2.4 图表

- **推荐：ECharts 5.x（复用）**
- **理由**：现有 4 处 `echarts.init` 可直接搬运（K线图、扫描结果分布、回测指标、监控图表），配置项零改动；Vue 封装成熟。
- **备选**：Lightweight Charts（K线专用、体积小，但需重写其余 3 处图表）；Chart.js（能力不足以承接 K 线 + 多指标叠加）。
- **落地依据**：`h5-feasibility.md:437`「现有 4 处 `echarts.init`（`:2854`、`:4998`、`:7537`、`:7549`）改造成本低」；`feature-matrix.md` §六 行情/K线行「**K线图组件（复用 ECharts，先改造 4 处 `echarts.init`）**」（L139）。

### 2.5 路由

- **推荐：Vue Router 4（History 模式）**
- **理由**：需承载 **7 主视图**（行情/分析/诊断/扫描/模型配置/回测/复盘+情绪账本，另加设置、关于）+ 配置页 8 子页签（嵌套路由）+ 4 关于子页签；History 模式使 F-101「同账号单活动会话」可用 URL 维度做只读提示。
- **备选**：Hash 模式（部署简单，无需 nginx fallback，但 URL 难看且不利于分享）；自建视图切换（沿用现有 `page-tab` 根层结构，可作过渡期方案）。
- **落地依据**：`feature-matrix.md` §六 各模块行 + 通用基础设施行「前端路由」（L152）；`h5-feasibility.md:437`「侧栏视图切换的层级结构」。
- **注意**：现有 `page-tab` 控件与 footer **固定在根层之外、内容区独立滚动**——该布局约束需在路由 layout 层保留，不要改成整页滚动。

### 2.6 本地存储

- **推荐：IndexedDB（通过 Dexie 封装）为主，OPFS 仅用于决策卡等二进制**
- **理由**：① 需缓存"最近查看的 N 只 K 线 + 报告"，IndexedDB 容量与结构化查询能力足够；② 明确**不能**按 §2.11 的桌面缓存规模设计——桌面实测 `cache/kline` = **206MB / 10325 个 pkl**（其中 `qfq_daily_*.pkl` **5121 个**），浏览器不可承载（`h5-feasibility.md:44-47`）；③ OPFS 在 iOS Safari 支持不完整，仅用于决策卡 PNG 等下载前暂存。
- **备选**：OPFS 为主（Chromium 优，Safari 差）；`localStorage`（仅存配置小项，5MB 上限，不可承载 K 线）。
- **落地依据**：`feature-matrix.md` §五 末行「后端对象存储 / DB + 前端 OPFS」（L131）；`h5-feasibility.md` §0.2 实测数据（L44-47）。
- **★需验证**：浏览器侧缓存条数上限（建议初版"最近 20 只 × 日K"，实测单只 300 根日K的序列化体积后定档）。

### 2.7 PWA

- **推荐：不启用完整 PWA（`F#1` 已定案：不承诺离线推送）；仅静态资源化与可安装标签页（不做 Service Worker push 事件）**
- **理由**：PWA 在原设计中**只为 F-802（离线推送）服务**；`F#1` 定案后该需求撤销 → 仅保留 `manifest.json` 的「安装到主屏」轻量体验与静态资源化收益，**不注册 `push` / `notificationclick` 事件监听、不做 VAPID 订阅**；通知送达改走 **webhook（企微/钉钉/飞书）+ 页内轮询红条**（§3.2 能力 7）。
- **备选**：**如需 Web Push 可作 P2 后的增量能力** —— 届时按 Workbox（预缓存 + 运行时缓存 + `push` 事件标准模板）实现，避免自建 SW 踩生命周期坑；或自建 Service Worker（最小实现：仅 `manifest.json` + `push`/`notificationclick` 两个事件监听，不做离线缓存）。
- **落地依据**：`feature-matrix.md:47` F-802「**[B]** …通知送达走 **webhook（企微/钉钉/飞书）** + 页内轮询红条（`F#1` 已定案：不做 PWA Web Push）」；`feature-matrix.md` §五「系统托盘 / 关 app 后收通知」（L134，**待随 `F#1` 同步**）。
- **约束**：`F#1` 已于 **2026-09-12 定案 = 不承诺离线推送** → 原三项 PWA 约束（iOS 需 **16.4+** 且「添加到主屏幕」、用户拒通知即失效）**不再构成验收阻塞**；仅当未来启用 Web Push 增量能力时重新评估。

### 2.8 后端框架

- **推荐：FastAPI + Uvicorn**
- **理由**：① 引擎是**纯同步 Python**（评分链 22279 行、`ui/web_api.py` 60+ 方法），FastAPI 同步/异步混用改造成本最低，`engine/*` 可**直接 import** 无需包装成服务；② 自带 Pydantic 校验（承接现有 `_validate_quant_before_save` 的校验语义，`ui/config_mapper.py`）；③ 自带 OpenAPI（便于 C3 与前端对齐接口契约）；④ **原生支持 SSE**（`StreamingResponse`），正好承接 F-403/F-502/F-703 的进度推送。
- **备选**：Flask + gunicorn（更简单，缺异步与 SSE 便利性）；Django（若需完整 admin/账号/权限体系——F-1401/1402/1406 的账号需求确实偏重，Django auth 可省不少工作，但会与"直接 import 引擎"的轻量形态冲突）；Litestar / Starlette（更轻）。
- **落地依据**：`h5-feasibility.md` §5.2（L441-446）；`feature-matrix.md` §六 通用基础设施行。

### 2.9 任务队列

- **推荐：Celery + Redis**
- **理由**：① 诊断（F-402）、扫描（F-502）、回测（F-701/F-703）、监控（F-801）都是**典型长任务**，均需"进度 + 取消 + 偶尔暂停"；② Celery 原生支持 `revoke`（取消）与状态查询，直接映射现有 `cancel_diagnosis`（`ui/web_api.py:4072-4079`）、`cancel_market_scan`（`:5201-5212`）、`pause_market_scan`（`:5214-5232`）、`cancel_backtest`（`:5961-5972`）；③ 回测需**独立 worker 镜像**（防 OOM 拖垮 API），Celery 队列/路由天然支持；④ 提供 beat 调度器用于 F-801 的定时任务。
- **备选**：RQ（更轻，够用则优先，但缺 beat 与优先级队列）；**APScheduler**（仅用于 F-801 监控定时，与长任务队列职责分开）；自建线程池 + Redis（最小改造，可复用现有 `_diag_worker`/`_scan` 逻辑，但多副本下任务状态需自己外置）。
- **落地依据**：`h5-feasibility.md` §5.3（L448-452）；`feature-matrix.md` §五 行「后台守护线程 / 并行 worker → 后端定时任务 / 任务队列 + SSE」（L127）。
- **注意**：现有"暂停"是 `threading.Event`（`ui/web_api.py:4238`）、"取消"是内存标志（`:3210`），迁移后**必须变为跨副本可见**的状态（Redis/DB）。

### 2.10 实时推送

- **推荐：SSE（`text/event-stream`）**
- **理由**：① 诊断/扫描/回测进度都是**单向**推送（服务端→客户端），SSE 语义刚好；② 基于 HTTP，与现有基础设施（nginx/CDN/鉴权中间件）兼容，无需协议升级；③ 浏览器原生 `EventSource` 自动重连，正好替代现有 1.5s 轮询（`index.html:5328`）与 `setInterval`（`:7356`）；④ 现有 `get_backtest_progress` 的"日志限最近 300 条"（`ui/web_api.py:5823`）语义可直接映射为 SSE 事件流。
- **备选**：WebSocket（若未来需要双向/多人协同编辑）；**保留轮询**（改动最小，但 N 用户 × M 任务的请求量会放大 —— 见 F-403 风险）。
- **落地依据**：`h5-feasibility.md` §5.4（L454-458）；`feature-matrix.md` §六 —— 「**SSE 进度组件**」（诊断/扫描行）、「**SSE 日志组件（限 300 条）**」（回测行）、「SSE 断线重连 + 任务终态补偿」（通用基础设施行）。
- **注意**：需处理"任务已结束但客户端未收到终态事件"的补偿（现有靠 `_grace_count` 宽限，`ui/web_api.py:1810-1816`）。

### 2.11 数据库

- **推荐：PostgreSQL**
- **理由**：① 方案配置是**深度嵌套 JSON**（`quant_model.json` 的 `schemes`/`factor_profiles`/三级合并语义），PG 的 **`JSONB`** 保留灵活性 + 支持索引（GIN）；② 账本/信号/情绪记录是关系型数据（`signal`/`cooldown`/`emotion_record`），需要事务与行锁；③ PG 的 `SELECT ... FOR UPDATE`（行锁）与 `percent_rank()` 窗口函数正好替代现有"内存排序 + 手算 percentile"（`ui/web_api.py:4919-4938`，F-503 的排序/筛选/分页下推）。
- **备选**：MySQL 8（JSON 类型 + 成熟运维，但 JSONB 索引与窗口函数能力弱于 PG）；**SQLite**（单机/低并发期最小改造，可承载 F-1601/F-1101/F-1201 起步，但**长任务与并发写不可靠**，不满足 `F#9` 会话互斥与 profile 行锁需求）；MongoDB（文档结构贴合但放弃事务便利）。
- **缓存层**：**Redis**——承载行情/K线短 TTL 缓存、扫描结果热数据、**任务状态与分布式锁**（F-101 会话锁、F-502 任务状态、profile 写锁）。
- **落地依据**：`h5-feasibility.md` §5.5（L460-466）；`feature-matrix.md:38` F-602「**事务化写入**」、`:37` F-601「**行锁/乐观锁**防并发写」、`:44` F-704「走 DB 事务+版本号 CAS」。

### 2.12 部署

- **推荐：Docker Compose 起步，预留 K8s**
- **构成**：`nginx`（静态前端 + 反代 + **SSE 缓冲关闭**）+ `api`（FastAPI，可多副本）+ `worker-diagnosis`（诊断/扫描）+ `worker-backtest`（回测，独立资源限额）+ `redis` + `postgres` + 对象存储卷（`reports/` 产物，或接 S3/OSS）。
- **理由**：① 现有 PyInstaller 打包（`build_webview.spec`）整体作废，容器化最贴近"onedir 分发"的运维习惯；② 回测 worker 必须与 API **物理隔离**（回测是内存杀手，现有 `engine/machine_probe.py` 就是为估算内存而存在）；③ 多副本部署会立刻暴露现有"进程级全局状态"问题（`self._market_type` `ui/web_api.py:3029-3030`、`quant_config._cache`、`_diag_tasks`/`_scan_tasks`）→ **建议先单副本上线，先修全局状态再扩副本**。
- **备选**：单机 `gunicorn + systemd`（成本最低，适合灰度）；Serverless（**不适合**：长任务回测与守护监控与 FaaS 模型冲突）；边缘部署（不适合 Python 重计算）。
- **落地依据**：`h5-feasibility.md` §5.6（L468-473）；`feature-matrix.md:43` F-703「后端异步长任务 + **独立 worker 镜像**（防 OOM 拖垮 API）」、`:41` F-701 同。

### 2.13 全局硬约束（三项，必须写入部署清单）

| # | 约束 | 内容 | 依据 |
|---|---|---|---|
| 1 | **依赖版本锁定** | 服务端必须锁 `pandas==3.0.3` / `numpy==2.5.1` / `matplotlib==3.11.0` / `cryptography==49.0.0`（`requirements.txt`，已于本步骤实测确认） | `requirements.txt`（**已实测**） |
| 2 | **时区固定** | `Asia/Shanghai` 全局固定（容器 TZ + PG `timezone`）——F-203 期货夜盘归属、F-302 合约过期判定均依赖本地时间 | `feature-matrix.md:20/23`；`h5-feasibility.md:473` |
| 3 | **SSE 关闭 nginx `proxy_buffering`** | 否则事件流被缓冲，进度延迟或成批到达 | `h5-feasibility.md:473` |

> **⚠️ 对约束 1 的理由修正（重要，实测发现）**
> 任务书与 C1 §5.2 均以「`engine/data_layer.py:628-641` 已记录跨 pandas 版本 pickle 不兼容」作为锁版本理由。**本步骤回源码核实：该风险已部分被规避**——`data_layer.py` 于 **2026-08-02** 改存储格式为 `dict(orient='list')`（`payload = save_df.to_dict(orient='list')`），注释明确「dict 存原生 Python 类型，**跨 pandas 版本读写均兼容**」，正是为绕开 pandas 3.0 `StringDtype` 与 2.x `NDArrayBacked.__setstate__` 不兼容而改。
> **故锁版本的必要性依然成立，但理由应更正为：**
> ① **浮点/算法结果漂移**：评分链依赖 pandas/numpy 的 `rolling`/`ewm`/`rank` 等实现细节，版本变更可能使因子值产生微小漂移 → **z-score 与状态分档（阈值）跳变**，直接影响 F-301/F-303 输出一致性（这是比 pickle 更硬的理由）；
> ② **产物渲染一致性**：`matplotlib==3.11.0` 决定回测 PNG 的字体/布局（F-703/F-705 产物）；
> ③ **授权与凭据加密**：`cryptography==49.0.0` 决定凭据加密列与口令哈希兼容；**存量激活码一次性核销仍需 Ed25519 验签**（分支 A 下验签只在服务端、只在核销时触发）；
> ④ **缓存兼容的残余面**：`_qfq_flag` 前复权可信标记的判脏重抓逻辑仍跨版本敏感（标记语义不随版本变，但重建路径需回归验证）。
> 行号 628-641 系**注释所在区间**，非"不兼容仍存在"的证据 —— 引用时请指向 `data_layer.py` 存储格式注释（约 `:620-641`）并说明已规避状态。
> **★需验证**：跨 pandas 小版本（3.0.3 → 3.0.x）对因子值的实际漂移量，建议做一次同输入双版本对拍。

---

## 三、后端能力清单（11 项 → API 端点）

### 3.1 能力总表与 B 类唯一归属

> **口径说明**：`h5-feasibility.md` §4.4 的"覆盖 F-xxx"列包含 **A/C/E 类的消费面**（如 F-603 数据面、F-702 参数、F-1404 额度）。为保证「42 条 B 类逐条核对无遗漏」，下表给出**唯一归属**（每条 B 类恰好归属一项能力）。

| # | 能力 | **B 类唯一归属** | 条数 | 优先级 | 推荐实现（源自 C1 §4.4） |
|---|---|---|---|---|---|
| 1 | 行情/K线数据代理与缓存 | F-201、F-202、F-203、F-204、F-305、F-1001、F-1002 | 7 | **P0** | 代理 + 源站（腾讯/新浪/东财）适配层 + 磁盘/Redis 缓存 |
| 2 | 评分/决策引擎服务 | F-301、F-302、F-303 | 3 | **P0** | 直接 import `engine/*`；`ctx.decision` 单一真相源 |
| 3 | 任务队列与进度推送 | F-105、F-401、F-402、F-403、F-501、F-502、F-503、F-504、F-506、F-801 | 10 | **P0** | Celery + Redis 承载长任务；SSE 推进度；任务状态外置 |
| 4 | 回测执行与产物存储 | F-701、F-703、F-704、F-705 | 4 | P1 | 独立 worker 镜像 + 对象存储 + 产物 URL |
| 5 | 配置与方案持久化 | F-601、F-602、F-1301、F-1303、F-1601、F-1603 | 6 | **P0** | PG（scheme + factor_profile + user_setting）；事务化写入；行锁/CAS |
| 6 | 账本存储（纪律 + 情绪） | F-1101、F-1102、F-1103、F-1104、F-1201 | 5 | P1 | PG（signal / cooldown / emotion_record）；现价刷新批量化 |
| 7 | 通知网关 | F-901、F-902 | 2 | P1 | 后端 webhook 转发 + 凭据加密（**`F#1` 已定案：不做 Web Push / VAPID**） |
| 8 | 授权 / 账号与合规 | F-104、F-1401、F-1402、F-1406 | 4 | **P0** | 账号 + JWT + **订阅/试用有效性判定（订阅制，无按次扣减）** + 设备绑定（N=2） |
| 9 | 文档与静态资源 | —（F-1501~1503 全为 A 类） | 0 | P2 | 静态托管 CDN 或 `GET /api/docs`；保留内嵌兜底文本 |
| 10 | 错误日志汇聚 | —（F-102=E、F-103=A） | 0 | P2 | 后端结构化日志 + 前端上报接口 |
| 11 | 股票/合约池、板块映射（+扫描结果表） | F-505 | 1 | P2 | PG（instrument / sector_map / scan_result 表）+ 管理接口 |
| | **合计** | | **42** | | |

> **能力的服务面 ≠ 归属面**：能力 11 的表结构同时服务 F-501/F-502（扫描执行读池）；能力 2 的引擎被能力 3/4/6 复用；能力 3 的 SSE 管道服务能力 4/6 的进度。上表仅用于**覆盖率核对**。

### 3.2 API 端点清单

> 设计原则：**面向 H5 前端（RESTful + SSE）**，不是把桌面版 60+ pywebview 方法 1:1 平移。桌面端"每动作一方法"的形态合并为"资源 + 动作"两级。

#### 能力 1：行情/K线数据代理与缓存（P0）

| 方法 | 路径 | 输入 | 输出 | 覆盖 F-xxx |
|---|---|---|---|---|
| GET | `/api/market/indices` | — | `{indices[], advance, decline, ts}` | F-201 |
| GET | `/api/quote/{code}` | path: code | `{price, change_pct, volume, amount, time}` | F-202 |
| GET | `/api/quote/batch` | `?codes=a,b,c` | `[{code, price, change_pct}]`（**批量，替代逐条请求**） | F-1101、F-1201 的现价刷新消费面 |
| GET | `/api/kline` | `?code&period&days` | `{code, period, bars[{t,o,h,l,c,v}]}` ∥ 404/422 | F-203 |
| GET | `/api/search` | `?q=` | `[{code, name}]` ≤20 条 | F-204 |
| GET | `/api/quote-only` | `?code&period&days` | `{kline[], quote{}}`（**独立限流桶、不扣额度、不写报告**） | F-305 |
| GET | `/api/data-sources` | — | `{kline_source, futures_source, available[]}` | F-1001 |
| POST | `/api/data-sources/{source}/test` | path: source | `{ok, latency_ms, effective_source}` | F-1001、F-1002 |
| GET | `/api/futures/contracts` | `?symbol=rb0` | `[{code, name, expire_ym, multiplier}]` | F-1002 |
| PUT | `/api/data-sources` | `{kline_source?, futures_source?}` | `{ok}`（**`F#8` 已拍板：不允许普通用户切换** → 普通用户 403，仅运维/管理员可调；原「风险项」关闭） | F-1001 |

#### 能力 2：评分/决策引擎服务（P0）

| 方法 | 路径 | 输入 | 输出 | 覆盖 F-xxx |
|---|---|---|---|---|
| POST | `/api/analyze` | `{code, market_type:'stock'\|'futures', k_type:'日K', scheme?}` | `{reportData, report_text, discipline, entry_tier}` | F-301、F-302、F-303 |

> 引擎能力本身**不暴露独立端点**：诊断（F-402）、扫描（F-502）在 worker 内直接调用同一函数；前端**不得**另写评分逻辑（`ctx.decision` 是单一真相源，`engine/report_builder.py:40`）。

#### 能力 3：任务队列与进度推送（P0）

| 方法 | 路径 | 输入 | 输出 | 覆盖 F-xxx |
|---|---|---|---|---|
| POST | `/api/diagnosis` | `{text, market, direction, scheme_name}` | `{task_id}` | F-401 |
| GET | `/api/diagnosis/{id}` | path | `{status, summary{5组计数}, total}` | F-402、F-403 |
| **GET** | **`/api/diagnosis/{id}/events`** | **SSE**（`text/event-stream`） | **`progress` / `done` / `error` 事件流** | F-403 |
| POST | `/api/diagnosis/{id}/cancel` | path | `{status:'cancelled'}` | F-403 |
| POST | `/api/scan` | `{market:'stock'\|'futures', params{}}` | `{task_id}`（分钟级参数 4xx） | F-501 |
| **GET** | **`/api/scan/{id}/events`** | **SSE** | **`progress`（含分片/板块聚合节点）事件流** | F-502 |
| POST | `/api/scan/{id}/pause` ∥ `/resume` ∥ `/cancel` | path | `{status}` | F-506 |
| GET | `/api/scan/{id}/results` | `?offset&limit&sort&filter&rating` | `{rows[], total, offset}`（**剥离 `tech_snapshot`**） | F-503 |
| GET | `/api/scan/{id}/export.csv` | path | `text/csv; charset=utf-8-sig`（首字节 `EF BB BF`） | F-506 |
| GET | `/api/scan/latest` | `?market=` | `{scan_id, finished_at, stale, very_stale:false}` | F-504 |
| POST | `/api/backtest` | `{mode, run_params{}}` | `{task_id}`（非法 mode 4xx；初级 403） | F-701（详见能力 4） |
| **GET** | **`/api/backtest/{id}/events`** | **SSE**（日志限最近 300 条） | **`log` / `progress` / `done` 事件流** | F-105、F-703、F-705 |
| POST | `/api/backtest/{id}/cancel` | path | `{status:'cancelled'}`（协作式中断，≤1 只检查点） | F-105、F-705 |
| GET | `/api/monitor/config` ∥ `PUT` | `{interval_seconds, align_to_grid, sessions[], strategy}` | 配置读写 | F-801 |
| GET | `/api/monitor/state` | — | `{next_tick_at, in_session, last_run_at}` | F-801 |
| POST | `/api/heartbeat` | `{session_id, ts}` | `204`（后端 15s 未收到 → 写 `client_lost`） | F-102（消费面） |

> **SSE 事件契约（统一）**
> 事件类型：`progress` `log` `stage` `done` `error`；负载 `{task_id, status, current, total, pct, message, ts}`。
> 重连补偿：支持 `Last-Event-ID` 回放最近 N 条（N 由日志上限决定，回测为 300）。
> **终态补偿**：连接建立时若任务已终态，立即下发 `done`/`error` 并关闭连接（替代桌面版 `_grace_count` 宽限，`ui/web_api.py:1810-1816`）。
> 心跳：SSE 每 15s 发 `: keepalive` 注释行，防中间代理断流。

#### 能力 4：回测执行与产物存储（P1）

| 方法 | 路径 | 输入 | 输出 | 覆盖 F-xxx |
|---|---|---|---|---|
| GET | `/api/backtest/modes` | — | `[{mode, label, params_schema}]`（**参数映射的前端依据**） | F-701（F-702 消费面） |
| POST | `/api/backtest` | `{mode, pool:'full'\|'watchlist'\|148, hold:5-60, scan:1-20, ...}` | `{task_id}` | F-701 |
| POST | `/api/backtest/{id}/apply-ic` | `{scheme, factor_profile}` | `{ok, updated}`（**返回真实写入因子数**；并行写冲突 409） | **F-704** |
| GET | `/api/backtest/{id}/artifacts` | path | `[{name, type, url, size}]`（**URL 替代 base64 内联**） | F-703、F-705 |
| GET | `/api/backtest/{id}/artifacts/{name}` | path | 文件流（PNG `image/png` / CSV / JSON） | F-703、F-705 |

> **F-704 硬约束（必须实现为后端直更）**
> 路径：**后端直接更新 `factor_profiles[fp].factor_configs[f]`**，走 **DB 事务 + 版本号 CAS**，**不走桌面版 `save_factor_stats`**。
> 理由：`docs/完整规格_合并版.md` §8.3.5 已登记 **R1/R2 残余风险**——`save_current_scheme`（`engine/quant_config.py:460`）与 `save_scheme_config`（`:760`）仍是"覆盖式写 raw、不读 profile"，安全性 100% 依赖调用方预切分；H5 应重写为引擎能力。
> 验收（`feature-matrix.md:44`）：提交后 `factor_profiles[fp].factor_configs[f]` 实际更新；返回 `updated` = 真实写入因子数；并行写冲突返回 **409**。

#### 能力 5：配置与方案持久化（P0）

| 方法 | 路径 | 输入 | 输出 | 覆盖 F-xxx |
|---|---|---|---|---|
| GET | `/api/scheme` | — | `[{name, factor_profile, market, direction, period}]` | F-601 |
| POST | `/api/scheme` | `{name}` | `201` ∥ **409**（同名）；命名含 `-` / 超 50 字 → 4xx | F-601 |
| GET | `/api/scheme/{name}` | path | 完整方案（含 8 子页签所需的全部字段，见下方硬约束 3） | F-602、F-603（数据面） |
| **PUT** | **`/api/scheme/{name}`** | 完整 `scheme` + `factor_profile` 载荷 | `{ok, version, updated_factors[]}`；版本冲突 **409** | **F-602**、F-603（读写）、F-604（模式值） |
| DELETE | `/api/scheme/{name}` | path | `204` | F-601 |
| POST | `/api/scheme/import` | multipart 文件（3 格式兼容） | `{ok, name}` ∥ 回滚错误 | F-601 |
| GET | `/api/scheme/{name}/export` | path | JSON 文件流（`Content-Disposition`） | F-601 |
| GET | `/api/usage` | — | `{usage:'basic'\|'advanced'}`（**默认 basic**） | F-1601 |
| PUT | `/api/usage` | `{usage}` | `{ok}`；同时切换扫描排序口径 | F-1601 |
| GET | `/api/capabilities` | — | `{factors_enabled, backtest_enabled, futures_scan_enabled, ...}`（**门控清单后端下发**） | F-1603、F-604（消费面） |
| GET | `/api/wizard/steps` | — | 恰 4 步：`select_factors`/`set_direction`/`set_threshold`/`set_position` | F-1301 |
| GET | `/api/wizard/state` | — | `{first_run, wizard_shown}` | F-1303 |
| POST | `/api/wizard/step` | `{action, payload}` | `{ok, next}` | F-1301、F-1303 |

> **`PUT /api/scheme` 的事务语义（F-602 核心）**
> 桌面版是"校验 → 加载 → 建壳 → 因子段写 profile → `save_scheme_config`"，**原子性靠手动回滚**（`ui/web_api.py:2010-2013`/`:2053-2056`），且 `load_config(force_reload=True)` 是**进程级全局缓存**（`:1994`）。
> H5 必须改为：**方案壳 + profile 同事务写入**（任一步失败整体回滚、无残留）+ **缓存改请求级/租户级** + **profile 并发写行锁 / 版本号 CAS**。

#### 能力 6：账本存储（纪律 + 情绪）（P1）

| 方法 | 路径 | 输入 | 输出 | 覆盖 F-xxx |
|---|---|---|---|---|
| GET | `/api/ledger` | `?market&cursor&limit` | `{signals[], cooldowns[], cursor}`（**冷却全局跨市场**） | F-1101 |
| GET | `/api/ledger/summary` | — | 4 指标：纪律盈亏 / 执行数 / 情绪化差合计 / 未按纪律数 | F-1104 |
| POST | `/api/signal/{id}/execute` | `{executed, exec_price, exec_ratio}` | `{ok, emotion_diff}`（**乐观更新，失败回滚**） | F-1105（消费面：F-1103 判定） |
| GET | `/api/emotion` | `?cursor&limit=200` | `{records[], cursor}` | F-1201 |
| POST | `/api/emotion` | `{code, action, tag, op_price, ...}` | `{id}`（**`op_price` 服务端锁定，前端不可覆盖**） | F-1201 |
| DELETE | `/api/emotion/{id}` | path | `204`（前端二次确认） | F-1201 |
| GET | `/api/emotion/summary` | — | `{count, float_diff_total, top_tag}` | F-1202（消费面） |
| GET | `/api/emotion/tags` | — | 标签常量表（**单一来源，前后端不各算一套**） | F-1204（消费面） |

> 信号落册口径（F-1102/F-1103）：**只 3 入口**（分析 / 期货分析 / 监控）可新增 `signal`；**全市场扫描不落信号**（口径硬约束，需白名单校验防批量诊断误挂）。判定改为**结构化枚举**，不再依赖文案前缀匹配（现 `startswith('🔴 清仓')`，文案一改即失效）。

#### 能力 7：通知网关（P1）

| 方法 | 路径 | 输入 | 输出 | 覆盖 F-xxx |
|---|---|---|---|---|
| GET | `/api/notifier/config` | — | 各渠道 `{enabled, has_credential}`（**不回显明文凭据**） | F-902 |
| PUT | `/api/notifier/config` | `{channel, fields{}}` | `{ok}`（**只更新提供的 key**；凭据加密存储） | F-902 |
| POST | `/api/notifier/{channel}/test` | path | `{ok, latency_ms}`（**仅 enabled 渠道真发**；异步 + 超时） | F-902 |
| ~~POST / DELETE~~ | ~~`/api/push/subscribe`~~ | — | **不实施**（`F#1` 已定案：不做 PWA Web Push；通知送达改走 webhook 转发 + 页内轮询红条） | F-802（已关闭） |

> F-901（webhook 转发）**不暴露前端端点**：4 渠道（inapp / wechat / dingtalk / feishu）的 POST 由后端内部发起（现 `engine/notifiers/channels.py:51/81/109` 的 `requests.post` 整体搬到服务端），前端不接触凭据。

#### 能力 8：授权 / 账号与合规（P0）

| 方法 | 路径 | 输入 | 输出 | 覆盖 F-xxx |
|---|---|---|---|---|
| POST | `/api/auth/register` ∥ `/login` ∥ `/refresh` ∥ `/logout` | 见各接口 | JWT（Access + Refresh）+ HttpOnly device cookie | F-1401 |
| GET | `/api/app-info` | — | `{version, account, license, device_id}`（**不含 `machine_id`**） | F-104 |
| GET | `/api/license/info` | — | `{status, tier, expires_at, trial_first_use_at, trial_days_left}` | F-1401、F-1402 |
| GET | `/api/quota` | — | `{tier, status, expires_at, unlimited:true}`（**订阅制：无计次分母**；服务端下发授权有效期，替代原硬编码 `/10`） | F-1406 |
| GET | `/api/account/devices` | — | `[{device_id, ua, bound_at, last_seen_at, current}]` | F-1403 |
| DELETE | `/api/account/devices/{id}` | path | `204`（解绑；绑定数回落至上限以下） | F-1403 |
| POST | `/api/license/redeem` | `{activation_code, account_id}` | `{ok, tier}`（**存量激活码一次性核销**：服务端 Ed25519 验签 → 转账号授权后凭据立即作废；**不再需要 `machine_id` 参数**） | F-1403（存量迁移） |

> **授权准入与分支无关（`F#4`/`F#5` 已拍板 → 口径重写）**：无论 A/B，授权状态**必须服务端判定**（不可绕过），但计费模型为**订阅制（包月 / 包年）**，故 H5 **不实现按次扣减**：
> ① **订阅期内不限次**；② **试用期内同样不限次**（`F#4` 用户原话：「试用也不扣减次数」）；③ **无有效订阅且试用已到期 → 直接按「授权过期」拦截（403）**，不进入次数竞争；④ 原桌面口径「10 次/日」（`FREE_DAILY_LIMIT=10`）、「批量诊断整批扣 1 次」（`ui/web_api.py:3301-3303`）、「扫描/回测预扣不退」（`:4144`/`:5709`）**整体退役**，仅作迁移期历史口径登记。
> **简化收益**：原「DB 原子扣减（`UPDATE ... SET quota=quota-1 WHERE quota>0`）」的并发竞争与「扣了次数但任务没建」问题（R-16）**一并消解**——准入判定为**只读**，天然幂等。
> **★仍存 1 项未拍板**：订阅期内是否设**用量上限 / 公平使用条款**（防单账号长期满负荷跑扫描，关联 R-08 源站压力与规格 M 容量）。当前按「订阅期内不限次」实现；若需上限则加 `usage_limit` 列 + 独立限流桶，**不改接口形态**。
> F-1402 试用起算基准见 `F#2`：**✅ 已定案（2026-09-12）= 首次注册时间（账号维度），换设备不重置；桌面版老用户迁移后额外赠送 30 天**。`license.trial_first_use_at` 取**账号注册时间**，即 ③「到期拦截 403」的生效时点。

#### 能力 9：文档与静态资源（P2）

| 方法 | 路径 | 输入 | 输出 | 覆盖 F-xxx |
|---|---|---|---|---|
| GET | `/api/docs/{name}` | path: manual/eval/agreement/disclaimer | `{name, text, fallback:false}` ∥ 内嵌兜底文本 | F-1501~1503（全 A 类，无 B 条目） |

> **推荐静态资源化**（CDN，零后端压力）而非接口；但**必须把内嵌兜底文本一并静态化**（现 `ui/web_api.py:2252-2315` 是"找不到文件 → 内嵌兜底"的双层保障，属**法律文本，勿改写**）。渲染保持**纯文本**（若引 Markdown 解析需 sanitize，XSS 风险）。

#### 能力 10：错误日志汇聚（P2）

| 方法 | 路径 | 输入 | 输出 | 覆盖 F-xxx |
|---|---|---|---|---|
| POST | `/api/js-error` | `{msg, url, line, col, stack, session_id}` | `2xx`（替代写本机文件 `ui/web_api.py:6081-6093`） | F-103（A 类消费面） |
| GET | `/api/client-logs` | `?session_id&since` | `{events[]}`（含 `client_lost` 心跳超时事件） | F-102（E 类替代方案） |

#### 能力 11：股票/合约池、板块映射与扫描结果表（P2）

| 方法 | 路径 | 输入 | 输出 | 覆盖 F-xxx |
|---|---|---|---|---|
| GET | `/api/instruments` | `?market=stock\|futures` | 池数据（服务端持有；**是否下发全量码表需评估商业资产**） | F-501、F-502（服务面） |
| GET | `/api/sectors` | `?market&min_count=3` | `[{sector, count, strength, avg_score}]`（强弱阈值 30/20/10） | F-505 |
| GET | `/api/sector-map` ∥ `PUT` | `{mapping}` | 板块映射读写（**`F#7` 已拍板：保留用户自定义** → 按账号隔离；合并优先级 **用户映射 > 内置 26 板块**） | F-505 |

### 3.3 B 类 42 条覆盖核对表

> 逐条核对：**42 / 42 全覆盖，无遗漏、无重复归属**。

| # | F-ID | 功能 | 归属能力 | 落地端点 |
|---|---|---|---|---|
| 1 | F-104 | 版本与应用信息 | 8 | `GET /api/app-info` |
| 2 | F-105 | 回测子进程协议 | 3 | `GET /api/backtest/{id}/events`(SSE)、`POST /{id}/cancel` |
| 3 | F-201 | 行情跑马灯 | 1 | `GET /api/market/indices` |
| 4 | F-202 | 实时行情 | 1 | `GET /api/quote/{code}` |
| 5 | F-203 | K 线查询 | 1 | `GET /api/kline` |
| 6 | F-204 | 股票搜索 | 1 | `GET /api/search` |
| 7 | F-301 | 查询流程（A 股） | 2 | `POST /api/analyze` |
| 8 | F-302 | 期货分析流程 | 2 | `POST /api/analyze`（`market_type='futures'`） |
| 9 | F-303 | 报告内容 | 2 | `POST /api/analyze`（`reportData` + `report_text`） |
| 10 | F-305 | 仅行情查询 | 1 | `GET /api/quote-only`（独立限流桶、不扣额度） |
| 11 | F-401 | 诊断启动 | 3 | `POST /api/diagnosis` |
| 12 | F-402 | 诊断 worker | 3 | `GET /api/diagnosis/{id}` + SSE |
| 13 | F-403 | 进度与取消 | 3 | `GET /api/diagnosis/{id}/events`、`POST /{id}/cancel` |
| 14 | F-501 | 扫描启动 | 3 | `POST /api/scan` |
| 15 | F-502 | 扫描 worker | 3 | `GET /api/scan/{id}/events`(SSE) |
| 16 | F-503 | 结果读取/排序/筛选/分页 | 3 | `GET /api/scan/{id}/results` |
| 17 | F-504 | 扫描缓存 | 3 | `GET /api/scan/latest` |
| 18 | F-505 | 板块强度 | 11 | `GET /api/sectors`、`GET/PUT /api/sector-map` |
| 19 | F-506 | 导出/暂停/开始 | 3 | `GET /{id}/export.csv`、`POST /{id}/pause\|resume\|cancel` |
| 20 | F-601 | 方案管理 | 5 | `GET/POST/DELETE /api/scheme`、`/import`、`/export` |
| 21 | F-602 | 配置保存 | 5 | `PUT /api/scheme/{name}`（事务化） |
| 22 | F-701 | 回测模式 | 4 | `POST /api/backtest`、`GET /api/backtest/modes` |
| 23 | F-703 | 子进程编排 | 4 | SSE 进度 + `GET /{id}/artifacts` |
| 24 | F-704 | 应用因子 IC 结果 | 4 | `POST /api/backtest/{id}/apply-ic`（**后端直更 + CAS**） |
| 25 | F-705 | 前端回测交互 | 4 | SSE `log`（限 300）+ 产物下载 |
| 26 | F-801 | 监控守护 | 3 | `GET/PUT /api/monitor/config`、`GET /api/monitor/state` |
| 27 | F-901 | 渠道（4 渠道 webhook） | 7 | 内部实现（无前端端点） |
| 28 | F-902 | 配置与测试 | 7 | `GET/PUT /api/notifier/config`、`POST /{channel}/test` |
| 29 | F-1001 | A 股 K 线源 | 1 | `GET /api/data-sources`、`PUT /api/data-sources` |
| 30 | F-1002 | 期货 K 线源 | 1 | `POST /api/data-sources/{source}/test`、`GET /api/futures/contracts` |
| 31 | F-1101 | 账本数据与入口 | 6 | `GET /api/ledger` |
| 32 | F-1102 | 信号来源（只 3 项） | 6 | 后端白名单校验（无独立端点） |
| 33 | F-1103 | 落册判定 | 6 | 随 `POST /api/analyze` 响应 `discipline` 字段 |
| 34 | F-1104 | 账本统计 | 6 | `GET /api/ledger/summary` |
| 35 | F-1201 | 情绪记录数据与入口 | 6 | `GET/POST/DELETE /api/emotion` |
| 36 | F-1301 | 步骤模型与接口 | 5 | `GET /api/wizard/steps`、`POST /api/wizard/step` |
| 37 | F-1303 | 首启自动弹出与前端联动 | 5 | `GET /api/wizard/state` |
| 38 | F-1401 | 授权接口/机制/状态 | 8 | `GET /api/license/info`、`POST /api/auth/*` |
| 39 | F-1402 | 试用机制 | 8 | `GET /api/license/info`（`trial_first_use_at`） |
| 40 | F-1406 | 前端面板（授权） | 8 | `GET /api/app-info`、`GET /api/quota`、`GET /api/account/devices` |
| 41 | F-1601 | 存储与接口（用法模式） | 5 | `GET/PUT /api/usage` |
| 42 | F-1603 | 门控影响点 | 5 | `GET /api/capabilities` |

**覆盖率：42 / 42 = 100%**（A 类 15 条 / C 类 1 条 / E 类 5 条走各自替代方案，见 §五 与 §一）。

### 3.4 API 设计硬约束（四条）

| # | 约束 | 内容 | 依据 |
|---|---|---|---|
| 1 | **42 条 B 类全覆盖** | 见 §3.3，逐条核对 42/42，漏一条即不合格 | 本步骤核对 |
| 2 | **F-704 走后端直更** | 后端直接更新 `factor_profiles[fp].factor_configs[f]`，**不走桌面 `save_factor_stats`**；走 DB 事务 + 版本号 CAS | `feature-matrix.md:44`；`完整规格_合并版.md` §8.3.5（R1/R2） |
| 3 | **F-603 8 子页签走通用方案 API** | 8 子页签读写全部走 `PUT /api/scheme`（`GET /api/scheme/{name}` 读），**不单列 8 个端点**——8 页签数据结构共享同一份 `quantData`（`ui/config_mapper.py` 双向映射），单列端点会产生 8 份部分写、破坏事务语义 | `feature-matrix.md:39`；`完整规格` §5.1 |
| 4 | **面向 H5 设计，非 1:1 平移** | 合并桌面版"每动作一 pywebview 方法"为"资源 + 动作"两级；进度统一走 SSE；取消/暂停统一为任务状态机 | `feature-matrix.md:38`「手动回滚 → 真事务」、`:43`「进程/文件机制整体重写」 |

---

## 四、数据模型迁移

### 4.1 逐项迁移表

| 桌面存储 | H5 存储 | 迁移方式 | 风险 |
|---|---|---|---|
| `config/quant_model.json`（`schemes` + `factor_profiles` + 三级合并） | PG **`scheme` + `factor_profile` 表，配置体用 `JSONB`** | 一次性导入脚本：读 JSON → 拆 `schemes`（壳）/ `factor_profiles`（因子体）→ 入库；**保留 `_meta.period` 与 profile 型方案的写回语义**（§8.3.5） | **profile 并发写必须行锁 / 版本号 CAS**（R1/R2 同源风险）；`_merge_scheme_config` 三级合并（`factor_profile(基底) < 方案内联 < period_configs[period]`）需在服务端复用**同一实现**，不得重写 |
| `config/usage.json` | PG **`user_setting`** 表（按账号） | 迁移时全部落 **`basic`**（防老用户被静默升档） | **默认 basic 必须保持**——`_is_basic_mode()` 被大量后端逻辑消费（扫描排序、因子校验、期货/回测禁用） |
| `config/data_sources.json` | **服务端全局配置**（`system_config` 表或环境变量） | 不入用户维表；由运维配置 | **`F#8` 已拍板：普通用户不可切换** → 服务端全局配置即**最终形态**（非过渡形态），按用户维度表**不建**；`tencent` 无分钟级约束需保留 |
| `config/monitor.json` | PG **`monitor_config`** 表（按账号） | 每账号一行：`interval_seconds` / `align_to_grid` / `sessions[]` / `strategy` | 每用户 sessions 需**按维度调度**（股票 `[570,690]` 分钟区间 + 休市判定），调度器需支持多用户不同网格 |
| `config/notifiers.json` + `credentials.json` | PG（**凭据加密存储**） | 渠道开关入 `notifier_config`；凭据走 KMS / 环境变量加密列 | **明文 → 加密**；`GET` 接口**不得回显明文**；密钥不入库（KMS/环境变量隔离） |
| `config/discipline_journal.json` | PG **`signal` + `cooldown`** 表 | JSON 数组 → 关系行；`cooldown` 表**全局跨市场** | JSON 文件并发写丢数据 → DB 事务；**信号落册口径"只 3 入口"必须保持**（扫描不落册） |
| `config/emotion_journal.json` | PG **`emotion_record`** 表 | JSON 数组 → 关系行；`op_price` 加服务端写保护 | 同上并发写风险；**`op_price` 必须锁定不可改**（前端传入一律忽略） |
| `config/license.json` + `machine_id` + `.lstate` | PG **`account` + `device` + `license`** 表 + JWT | 授权状态**全部服务端**；本地状态文件整体废弃 | **已按分支 A 定稿**：设备唯一键 = `device_token_hash`（绑定上限 **N=2**）；`machine_id_hash` 与 `activation_code` 表**不建**（存量激活码走一次性核销，验签后凭据作废，仅留审计日志）。**`license` 表按订阅制建**（`tier`/`status`/`expires_at`/`sub_cycle`，**无 `quota_*` 计次列**，`F#4`）。`run_startup_check` / Ed25519 本地验签删除 |
| `config/watchlist.txt` / `watchlist_futures.txt` | PG **`watchlist`** 表（按账号隔离） | 文本行 → 关系行；股票/期货分表或加 `market` 列 | 关注池是用户私有数据，**必须按账号隔离**；上限需定义 |
| `cache/kline/qfq_daily_{code}.pkl`（**实测 206MB / 10325 个 pkl，其中 `qfq_daily_*` 5121 个**） | **后端磁盘 / 对象存储（复用同语义）** + Redis 短 TTL 热层 | **不迁移数据**（桌面缓存是本地产物，非用户数据）；服务端从源站**重新预热**；复用 `qfq_daily_{code}.pkl` 命名与"前复权可信标记"语义 | **不能塞进浏览器**（206MB 量级）；Redis 只做热层，冷层留磁盘；预热策略见 §7.2 |
| `reports/`（回测产物） | **对象存储（S3/OSS）** | 替代 `get_app_dir()/reports/`（`ui/web_api.py:5780-5782`）；产物以 **URL** 访问 | 替代 **base64 内联 PNG**（`:5906-5916`）——响应体积与内存占用；需统一产物 URL、跨副本单一来源 |
| `decision_cards/`（决策卡 PNG） | **后端对象存储** 或 **纯前端下载** | 推荐：`html2canvas` → `toBlob()` → `<a download>`（**不落服务端**）；若要分享则走后端存 URL | iOS Safari 老版本不支持 `toBlob` → 备"复制剪贴板"；文件名约定 `M-Bull_{code}_{YYYYMMDD}.png` |
| `monitor_state.json`（监控去重） | PG（**唯一键 `account_id + market + direction + code`**） | 运行时产物 → 关系表；去重键 `{market}:{direction}` 语义不变 | 并发触发重入需 DB 唯一约束兜底（现靠 `_tick` 重入保护，`ui/monitor.py:293-322`） |
| `sector_map.json`（板块映射，26 内置板块模板） | PG **`sector_map`** 表 | 内置 26 板块入系统行；用户自定义行按账号隔离 | **`F#7` 已拍板：保留用户自定义** → 按账号隔离表**落地**（非可选项）；**合并优先级 = 用户映射 > 内置 26 板块**（同 sector 键以用户值为准，未被用户覆盖的内置板块保留） |

### 4.2 五个关键迁移点（必须实现，否则功能不成立）

| # | 迁移点 | 做法 | 不做的后果 |
|---|---|---|---|
| 1 | **`quant_model.json` → PG JSONB + 行锁** | 壳（`scheme`）+ 体（`factor_profile`）分表；写走事务；**版本号 CAS** | F-601/F-602/F-704 并发写互相覆盖（桌面版已存在的手动回滚/覆盖式写 raw 缺陷被放大到多用户） |
| 2 | **两份 journal（纪律 + 情绪）→ 关系型表** | `signal`/`cooldown`/`emotion_record` 三表；写走事务 | F-1101/F-1201 并发写丢数据（JSON 全量覆写） |
| 3 | **K 线缓存 → 后端磁盘/Redis** | 复用 `qfq_daily_{code}.pkl` 语义 + **前复权可信标记**（`_qfq_flag`）；服务端预热 | 前端拉全量 K 线必被源站限流（5121 只 × 300 根） |
| 4 | **回测产物 → 对象存储** | URL 替代 `get_app_dir()/reports/`；PNG 用 `<img src=url>` 替代 base64 内联 | 响应膨胀（base64 体积 ≈ 原图 1.33×）、无法跨副本共享、F-705「打开产物」无实现 |
| 5 | **决策卡 → 后端对象存储 或 前端下载** | 优先纯前端 `<a download>`；分享场景才落对象存储 | 替代本机 `decision_cards/` 目录（`ui/web_api.py:2565`），否则 F-304 导出无落点 |

---

## 五、平台差异应对（10 条，逐条给方案）

> 来源：`feature-matrix.md` **§五 平台差异合并清单**（L118-132），逐条引用不重写。

| # | 桌面能力 | H5 方案 | 实现要点 | 风险 | 影响 F-xxx |
|---|---|---|---|---|---|
| 1 | 右键（上下文菜单） | 长按 / 显式操作按钮 | 行内操作改为**常驻图标按钮**（编辑/删除/登记），长按仅作补充；**长按不承担主路径** | 移动端长按与滚动冲突 | F-1105、F-1203、F-601 |
| 2 | 多窗口 / 单实例进程 | 路由 + 会话唯一 | 后端会话表（**Redis `SETNX` + TTL** / DB 唯一约束）+ 前端 `BroadcastChannel` 多标签提示；**承诺由"防多开"改"防并发写"** | 多标签/多设备无法阻止；**任务状态必须外置**，否则轮询落错副本 404 | F-101、F-401、F-501 |
| 3 | 全局快捷键 / 原生模态弹窗（MessageBox） | 页内 toast / dialog | 统一组件：`toast`（轻提示）/ `dialog`（需确认）；**崩溃弹窗改为后端 `client_lost` 日志 + 前端 toast** | H5 无原生弹窗概念，**全部改前端组件** | F-102、F-101 |
| 4 | 本地文件读写（`open(path,'w')` / `os.startfile`） | 文件选择器 `<input type=file>` / `Blob` 下载 / 在线预览 | 导入用 `<input type=file>` + `file.text()`（现有代码已如此）；导出用 `Blob` + `<a download>`；回测产物用 `<img>`/表格在线预览 | iOS Safari **无 File System Access**、`a[download]` 支持不完整 → **必须备"复制剪贴板"降级** | F-601、F-404、F-506、F-705、F-304 |
| 5 | 系统托盘 / 关 app 后收通知 | ~~**PWA + Web Push**（Manifest / Service Worker）~~ **webhook（企微/钉钉/飞书）+ 页内轮询红条**（`F#1` 已定案：不做 PWA Web Push） | 后端 webhook 转发 + 页内 toast 红条（**不注册 `push` 事件、不做 VAPID 订阅**） | ~~iOS 需 **16.4+** 且「添加到主屏幕」；**用户拒通知即失效**~~ → **随 `F#1` 定案不再构成阻塞** | F-802、F-801 |
| 6 | 后台守护线程 / 并行 worker | 后端定时任务 + 任务队列 + SSE | Celery beat（F-801 定时）+ 队列 worker（诊断/扫描）+ **独立回测 worker 镜像**；`auto_n_workers()` 的 `ctypes.windll.GlobalMemoryStatusEx`（`engine/machine_probe.py:46-73`）换 **`psutil` / cgroup 限额** | 「app 开着才生效」消失，但**要求后端 7×24**；并发写需外置任务状态与行锁 | F-801、F-402、F-502、F-703、F-105 |
| 7 | 机器指纹（`uuid.getnode()` MAC / 硬件密钥 / 注册表） | **账号 + 服务端设备绑定 + JWT**（**已选定 = 分支 A**） | 设备 token（HttpOnly Cookie，长效 + 可撤销）+ 绑定上限 **N=2**（`F#3` 已拍板）+ 异常登录检测（同账号多地并发 → 告警 / 踢出，与会话踢旧同源） | **绑定强度低于硬件指纹**：清 Cookie / 换浏览器即视为新设备 → 以**授权状态服务端判定为主防线**（即便绕过绑定也拿不到有效授权） | F-1403、F-1404、F-1401、F-1406、F-104 |
| 8 | 源站直连（腾讯/新浪/东财，无 CORS） | **服务端数据代理 + 缓存 + 限流** | 统一代理层 + 字段位序适配（现按腾讯 `fields[3]`=现价 解析）+ 磁盘缓存 + 限流退避 | 浏览器直连**必被 CORS 拦**；代理**放大源站请求**需限流与合规评估 | F-201/202/203/204/305、F-1001/1002 |
| 9 | 本地进程级全局缓存 / 内存任务字典 | **请求级/租户级缓存 + DB 外置状态** | `quant_config._cache` 改请求级；`_diag_tasks`/`_scan_tasks`（进程内存字典）改 DB 任务表；`self._market_type`（`ui/web_api.py:3029-3030`）改请求级上下文 | **多副本/多用户并发互相冲掉配置或串市场** → **必须先修全局状态再扩副本**（建议初版单副本） | F-602、F-401、F-501 |
| 10 | 本地持久化路径（`%LOCALAPPDATA%` / `get_app_dir()`） | **后端对象存储 / DB + 前端 OPFS** | 消费方全部改：`engine/error_log.py:35`、`ui/web_api.py:2565`（decision_cards）、`:2590`（diagnosis_reports）、`:5780-5782`（reports）、`:6084`/`:6109-6110`（logs） | 需**统一产物 URL、跨副本单一来源**；账本 JSON 改 DB 防并发丢数据 | F-304/404/506、F-703、F-1101/1201 |

> **横切项（源自 C1 §3.10，随上表落地）**：`ctypes.windll.kernel32.FreeConsole`（`main_webview.py:306-310`）→ **直接删除**（H5 无控制台概念）；`QUANT_SYSTEM_DIR` 环境变量（`engine/config.py:81-83`）→ 换形为"服务端配置项/部署参数"；`os._exit(0)`（`main_webview.py:294/326`）→ 需 **graceful shutdown**（worker 收 SIGTERM 后完成当前单元）；`sys.frozen` chdir（`:149-150`）→ 不适用（服务端绝对路径）。

---

## 六、风险清单

### 6.1 平台差异类风险（源自 `feature-matrix.md` §五 逐条）

| # | 风险 | 影响 F-xxx | 概率 | 影响 | 缓解方案 |
|---|---|---|---|---|---|
| R-01 | 移动端**长按与滚动冲突**，行内操作不可达 | F-1105、F-1203、F-601 | 高 | 中 | 行内操作改**常驻图标按钮**，长按仅作补充；移动端断点下按钮尺寸 ≥44px |
| R-02 | 多标签/多设备**无法阻止**，任务状态落错副本 → 轮询 404 | F-101、F-401、F-501 | 高 | 高 | 任务状态**外置到 Redis/PG**；SSE 连接带 `task_id` 与副本无关；**初版强制单副本** |
| R-03 | 无原生弹窗 → **需审核的产品降级**（如"已在运行"提示改 toast） | F-101、F-102 | 高 | 低 | 统一 toast/dialog 组件；文案明确"仅供提示" |
| R-04 | iOS Safari **无 File System Access**、`a[download]` 支持不完整 → 导入/导出失败 | F-601、F-404、F-506、F-705、F-304 | 中 | 高 | 导入用 `<input type=file>`（全平台可用）；导出**必须备"复制剪贴板"降级**；决策卡 `toBlob` 失败 → canvas `toDataURL` + 新窗口另存 |
| R-05 | **iOS 16.4+ 且需「添加到主屏幕」**，用户拒通知即失效 | F-802、F-801 | 高 | 中 | 三级降级链：Web Push → **webhook（企微/钉钉/飞书）** → 页内轮询红条；PWA 安装引导做成新手任务（**`F#1` 已定案：不承诺离线推送 → 本风险消除**） |
| R-06 | 后端**必须 7×24**，调度器宕机即丢监控信号 | F-801、F-402、F-502、F-703、F-105 | 中 | 高 | Celery beat + 持久化调度；任务补偿重放；监控状态以 DB 为准（非内存） |
| R-07 | 绑定强度低于硬件指纹，**可被"换浏览器换设备"绕过**（分支 A 已选定 → 本风险为**接受态**，非待消除项） | F-1403、F-1404、F-1401 | 高 | 中 | 绑定上限 **N=2（`F#3` 已拍板）** + 异常登录检测 + **授权状态服务端判定**（不可绕过是关键防线） |
| R-08 | 服务端代理**放大源站请求**（5121 只 × 300 根），可能被限流/封禁 | F-201/202/203/204/305、F-1001/1002 | 高 | 高 | 磁盘缓存 + Redis 热层 + **令牌桶限流 + 指数退避 + 熔断切源**（已落到**可插拔适配层**，见 §7.3）；扫描强制复用热缓存。⚠️ **合规风险未消除**：`C1#3` 决策为"先用免费 API + 可插拔"，属**已知接受风险** |
| R-09 | 多副本/多用户并发**互相冲掉配置或串市场** | F-602、F-401、F-501 | 高 | 高 | 全局状态全部外置（`quant_config._cache`、`_diag_tasks`/`_scan_tasks`、`self._market_type`）；**先修全局状态再扩副本** |
| R-10 | 产物/账本**跨副本不一致**、账本 JSON 并发丢数据 | F-304/404/506、F-703、F-1101/1201 | 中 | 高 | 产物统一对象存储 + URL；账本改 DB 事务；禁用任何本地路径写入 |

### 6.2 API 设计类风险（本步骤识别）

| # | 风险 | 影响 F-xxx | 概率 | 影响 | 缓解方案 |
|---|---|---|---|---|---|
| R-11 | **SSE 断线**（移动端切后台/网络切换）导致进度丢失，任务已结束但前端不知 | F-403、F-502、F-703、F-705 | 高 | 中 | `EventSource` 自动重连 + `Last-Event-ID` 回放最近 N 条；**连接建立时先查任务终态**并立即补发 `done`；15s `keepalive` 注释行 |
| R-12 | SSE 被中间层**缓冲**（nginx/CDN/企业代理）→ 进度成批到达或超时 | F-403、F-502、F-703 | 中 | 中 | nginx `proxy_buffering off`（**硬约束**）；响应头 `X-Accel-Buffering: no`；`Cache-Control: no-cache` |
| R-13 | **DB 行锁 / CAS 竞争**：profile 并发写冲突率高，用户反复 409 | F-601、F-602、F-704 | 中 | 中 | 版本号 CAS + **冲突返回 409 并携带最新版本**；前端提示"配置已在其他设备修改，请刷新"；写操作幂等化 |
| R-14 | **对象存储成本失控**：回测产物（多模式 × 多标的 × 多次运行）无限制累积 | F-703、F-705、F-304 | 中 | 中 | 产物 TTL（建议 30 天）+ 生命周期策略；单任务产物大小上限；用户级配额；PNG 落盘前压缩 |
| R-15 | **K 线缓存预热窗口不足**：开盘后全市场扫描从冷缓存启动，耗尽源站配额。**注：`F#6` 的扫描验收以「命中服务器热缓存」为前提 → 本风险是 F#6 验收的直接前置** | F-501、F-502、F-203 | 中 | 高 | 收盘后预热（见 §7.2）；扫描任务**优先读热缓存**，缺缓存标的降级跳过并标记；预热进度可视 |
| R-16 | ~~额度原子扣减与任务创建非原子 → 扣了额度但任务没建（或反之）~~ **已消解** | F-1404、F-501、F-701 | — | — | **风险关闭（`F#4`/`F#5` 已拍板为订阅制）**：无按次扣减 → 不存在"扣了次数但任务没建"。替代关注点 = 订阅状态**只读判定**与任务创建的时序（不写计数 → 天然幂等，无需补偿事务） |
| R-17 | **`PUT /api/scheme` 全量写**（8 页签合并载荷）产生**最后写覆盖**：两设备同时编辑不同页签，后提交者覆盖前者 | F-601、F-602 | 中 | 高 | 版本号 CAS（乐观锁）为主；如需字段级合并，按 `factor_profile` 粒度加行锁；**不引入三方合并**（复杂度高于收益） |
| R-18 | 前端**误写评分逻辑**导致"核心结论"与"多空信号"分叉 | F-303、F-301 | 低 | 高 | 前端**只渲染** `reportData`/`report_text`（后端 `ctx.decision` 单一真相源）；code review 卡点；接口契约冻结 |
| R-19 | **下游时区未统一**导致夜盘 K 线归错交易日、期货合约过期判定错 | F-203、F-302 | 中 | 高 | 容器 `TZ=Asia/Shanghai` + PG `timezone='Asia/Shanghai'` + 应用层统一 `ZoneInfo`；**禁止**混用 UTC 本地化 |
| R-20 | **依赖版本漂移**（尤其 pandas/numpy）导致因子值微漂、状态分档跳变 | F-301、F-303、F-502、F-703 | 中 | 高 | 锁 `pandas==3.0.3`/`numpy==2.5.1`/`matplotlib==3.11.0`/`cryptography==49.0.0`；CI 加"双版本对拍"回归（★需验证漂移量，见 §2.13） |

---

## 七、阻塞决策分支设计（`C1#1` ✅已决 / `C1#2` ✅已决 / `C1#3` ✅已决）

> **决策进度（2026-09-12）**：
> ✅ **`C1#1` 授权模式** → **分支 A**（账号 + 服务端设备绑定）→ **形态冻结为 `H5 + 后端`**；
> ✅ **`C1#3` 数据源合规** → **先用免费 API + 数据源可插拔适配层**；
> ⏳→✅ **`C1#2` 成本模型** → **已拍板选定「规格 M」（中等并发，300–500 DAU）**（2026-09-12；规格 S 转存档兼实施前置阶段，见 §7.2）。

### 7.1 `C1#1` 授权模式（决定形态）

**完整架构差异已在 §1.2 给出**（技术栈 / 后端能力 / 数据模型 / 平台差异四个维度，A、B 两分支逐项对照）。此处仅补决策要点：

| 决策点 | 分支 A ✅ 已选定（生效） | 分支 B 🚫 已否决（存档） |
|---|---|---|
| 形态 | H5 + 后端 | H5 + 后端 + 混合壳（Capacitor/Tauri） |
| F-1403 分级 | 维持 **E** | **E → D** |
| E 类条数 | 5 | **4**（feature-matrix §三 需同步修订：D 0→1、E 5→4） |
| 授权锚点 | 账号 + 服务端 device token | 壳提供的机器标识 + 服务端 Ed25519 验签 |
| 新增后端能力 | 账号体系 / 设备表 / 异常登录检测 | 账号体系 + **服务端验签接口**（`POST /api/license/redeem`）+ `activation_code` 表 |
| 存量激活码 | **一次性核销迁移**后退役 | **继续有效**（服务端验签） |
| 发布链路 | 仅 Web | Web + App Store / 应用商店审核 |
| 一码多机防护 | 弱（绑定上限 + 异常检测） | 中（机器标识 + 验签） |
| 额外成本 | 低 | 高（壳工程 + 插件 + 签名发布 + 审核周期） |
| **两分支共同点** | 后端均为必需；**授权状态均须服务端判定**；核心算法均须后端托管 | 同左 |

**启动建议**：后端按**分支 A 架构先行**（两分支共同底座占 9/11 项能力）；若 `C1#1` 选 B，仅追加"服务端验签 + `activation_code` 表 + 壳工程"三项，**不推翻已有后端设计**。

**状态：✅ 已选定分支 A（2026-09-12 用户决策）。形态冻结为 `H5 + 后端`。**

**决策落地清单（分支 A 生效后必须执行）**

| # | 事项 | 说明 |
|---|---|---|
| 1 | 形态冻结 | `H5 + 后端`；**混合壳不做**；`build_webview.spec` 打包链整体作废 |
| 2 | 授权体系 | 新建 `account` / `device` / `license` 表；Ed25519 **本地**验签删除；存量激活码走**一次性核销**。**`license` 表按订阅制建**（无 `quota_*` 计次列，`F#4`） |
| 3 | **`F#3` 已拍板 → N = 2** | 绑定设备上限 **N = 2** 为验收断言「第 3 台被拒 403」的前置参数（见 §八）；`device` 表唯一约束 + 绑定计数校验同步落地 |
| 4 | `feature-matrix.md` | §三 分布表**无需修订**（D=0 / E=5 成立）；F-1403 维持 **E**（不升 D） |
| 5 | 设备绑定实现 | `device_token_hash` 为唯一键；HttpOnly Cookie + 可撤销；清 Cookie → 重新绑定流程 |

### 7.2 `C1#2` 回测 / 扫描成本模型

**✅ 已决策（2026-09-12 用户拍板）：选定「规格 M」（中等并发）。** 两套规格仍并列保留——规格 S 转**存档**（作为「单副本灰度期」与 M 的实施前置阶段），规格 M 为**目标部署规格**。

#### 规格 S：小规模灰度（目标 ≤50 DAU / ≤5 并发任务）—— **未采用，存档；同时是规格 M 的实施前置阶段**

| 项 | 配置 | 说明 |
|---|---|---|
| `api` | 2 vCPU / 4 GB × **1 副本** | 单副本规避 R-09（全局状态未修完前不扩副本） |
| `worker-diagnosis`（诊断+扫描） | 4 vCPU / 8 GB × 1 | 并发线程建议 **8**（`ThreadPoolExecutor`，现桌面为 `min(auto_n_workers(), 16)`） |
| `worker-backtest` | **8 vCPU / 32 GB × 1（独立节点）** | 回测是内存杀手；须与 API 物理隔离 |
| `redis` | 1 vCPU / 2 GB | 任务状态 + 锁 + 行情短 TTL |
| `postgres` | 2 vCPU / 8 GB / 100 GB SSD | 单实例 |
| 对象存储 | 按量 | 产物 TTL 30 天 |
| **缓存预热策略** | **每日 15:30 后增量预热一次**；首次全量预热独立执行 | 复用 `qfq_daily_{code}.pkl` 语义；★需实测全量预热耗时 |
| 并发上限 | 单用户同时任务 **1**；全局同时任务 **3**（诊断/扫描）+ **1**（回测） | 队列 FIFO |
| 单次全市场扫描耗时上限 | **≤30 min**（命中热缓存） | ★需实测 |
| 单次全市场回测耗时上限 | **≤4 h**（超时终止并保留中间产物） | 规格 F-701 述"全市场数小时"，★需实测 |
| 适用期 | 灰度 / 内测 / 首批付费用户 | — |

#### 规格 M：中等并发（目标 300–500 DAU / 30–50 并发任务）—— ✅ **已选定（2026-09-12）＝ 目标部署规格**

| 项 | 配置 | 说明 |
|---|---|---|
| `api` | 4 vCPU / 8 GB × **4 副本** | **前置条件：R-09 全局状态已全部外置** |
| `worker-diagnosis` | 8 vCPU / 16 GB × **2–4**（按队列深度） | 自动扩缩容 |
| `worker-backtest` | 16 vCPU / 64 GB × **2–4**（弹性） | 独立节点池 |
| `redis` | 4 vCPU / 16 GB | 任务状态 + 分布式锁 + 缓存热层 |
| `postgres` | 8 vCPU / 32 GB / 1 TB SSD + **只读副本** | 只读副本承接 F-503 结果查询（`percent_rank()` 窗口函数） |
| 对象存储 | 按量 + 生命周期策略 | 产物 TTL 30 天；用户级配额 |
| **缓存预热策略** | **每日 2 次全量**（15:30 收盘、08:30 盘前）+ **盘中增量**（仅活跃标的，分钟级） | 预热频率提升以保 R-15 |
| 排队 / 配额 | Celery 优先级队列（**付费高等级优先**）；单用户并发任务 **≤2**；全局 30–50 | **`F#4`/`F#5` 已拍板（订阅制、不计次）→ 配额按「并发数」而非「剩余次数」控制**；订阅期内不限次，单用户并发 ≤2 作为公平使用约束（对应 §3.2 能力 8 注的 ★用量上限待定项） |
| 单次全市场扫描耗时上限 | **≤30 min**（前提：命中服务器热缓存） | **`F#6` 已拍板**：验收样本 = 服务器 K 线缓存全量池（≈5215 只），数据侧不触发源站抓取；★耗时上限仍为占位值，P1 用实测校准 |
| 单次全市场回测耗时上限 | **≤2 h**（缩短以保障队列吞吐） | 同上 |
| 适用期 | 正式运营 | — |

> **★需实测的三项（决定两规格能否成立）**：
> ① 单机全市场回测实测耗时（规格述"数小时"，无精确值）；
> ② 单只 300 根日 K 的缓存体积与全量预热耗时（实测 `cache/kline` 206MB / 5121 个 `qfq_daily_*.pkl`，但**预热速率未测**）；
> ③ 内存峰值与 `estimate_cache_per_process_bytes` 估算的偏差（`engine/machine_probe.py:160-161` 的 `file_bytes * mult + _PER_PROC_BASE`）。
> **状态：✅ 已选规格 M（2026-09-12 用户决策）。**
> **落地路径（重要，不因选 M 而省略）**：规格 M 的 `api` 行为 **4 副本**，其前置条件是 **R-09 全局状态全部外置**（`quant_config._cache` / `_diag_tasks` / `_scan_tasks` / `self._market_type`）。故实施顺序仍为 **规格 S 单副本灰度 → 完成全局状态外置 → 扩至规格 M（4 副本）**；`C1#2` 的决策改变的是**目标容量**，不改变这条前置依赖。
> ★需实测的三项（决定规格 M 的容量参数能否成立）仍待量化：① 单机全市场回测耗时；② 全量预热速率；③ 内存峰值与 `estimate_cache_per_process_bytes` 的偏差。

### 7.3 `C1#3` 数据源合规性 —— ✅ 已决策（2026-09-12）

**决策：先用免费 API（腾讯 / 新浪 / 东财，即现状源），并把数据源做成「可插拔」适配层 —— 未来可整体替换为正规授权源，上层零改动。**

> **定位说明（必须显式登记）**：本决策**不消除合规风险**，而是**接受**它并**降低未来的切换成本**。免费源在服务端集中代理分发，其条款风险与下文"路径 1"完全相同；「可插拔」买到的是**迁移自由度与时间**，**不是合规豁免**。
> → **`F#8`（数据源是否允许普通用户切换）已拍板为「不允许」**（§八）；法务条件成熟时仍需复核源站条款。

#### 7.3.1 可插拔数据源层设计（本决策的核心产出）

**接口契约（统一协议，所有源实现同一接口）**

| 方法 | 语义 | 说明 |
|---|---|---|
| `capabilities()` | → `{periods[], markets[], adjusted:'qfq'\|'none'\|'hfq', minute_level, rate_limit}` | **源的自我描述**；上层据此路由（如腾讯无分钟级 → 自动走新浪） |
| `fetch_kline(code, period, days)` | → `bars[{t,o,h,l,c,v}]` | 日 / 周 / 分钟 K 线 |
| `fetch_quote(codes[])` | → `[{code, price, change_pct, volume, amount, time}]` | 批量行情 |
| `search(q)` | → `[{code, name}]` | 搜索 |
| `fetch_contracts(symbol)` | → `[{code, expire_ym, multiplier}]` | 期货合约（可选能力） |
| `health()` | → `{ok, latency_ms, last_error}` | 供熔断器判定 |

**注册与配置（配置驱动，非硬编码）**

- 每个源以**注册项**声明：`{source_id, module, priority, markets, enabled}`；
- 优先级形成**降级链**：主源连续失败 / 超时 → 自动切下一源 → 熔断冷却后回切；
- **新增源 = 新增一个 adapter 实现 + 注册一条配置**，不改上层、不重新发版；
- 切换通过**配置变更**完成（运维动作，非代码动作）。

**四条硬性落地要求**

| # | 要求 | 原因 |
|---|---|---|
| 1 | **复权口径必须随源声明** | 现状日K / 周K 均为**前复权**：默认主源=通达信（本地自算，除权周与腾讯服务端 0.1%~0.94% 偏差），腾讯源=服务端 qfq（两源无除权日逐日偏差 0.000%）。`_qfq_flag` 可信标记机制依赖「前复权」这一口径。新浪源为**未复权**，落独立 store 不可混用。换源若只有不复权 / 后复权 → **历史因子值会漂移**，必须能识别并触发**整段重抓 + 因子值回归**，**严禁混用** |
| 2 | **缓存键需含「源标识 + 复权口径」** | 否则换源后旧缓存被误用——当前 pkl 命名 `qfq_daily_{code}.pkl` **不含源标识**。建议扩展为 `{source}_{adj}_{period}_{code}.pkl`；★需评估与现有 5121 个存量 pkl 的共存 / 迁移路径 |
| 3 | **限流 / 退避 / 熔断是适配层职责** | 每个源独立令牌桶；调用方只依赖统一接口，不感知源差异 |
| 4 | **源不可用时的降级行为必须明确** | 行情面板显示 `--` 且不阻塞（F-201 语义）；扫描遇缺缓存标的 → **跳过并计数**，不静默失败 |

#### 7.3.2 免费源（**当前决策，生效**）—— 即原「路径 1 服务端代理」

| 项 | 设计 |
|---|---|
| 代理层 | 统一 `source_adapter`：入参 `code/period/days` → 出参标准化 `bars[]`；源站差异（腾讯字段位序 `fields[3]`=现价、新浪分钟级、东财期货 `secid`）全部封在适配层内 |
| 限流退避 | **per-source 令牌桶** + 指数退避 + **熔断切源**（连续失败 → 切备用源）；扫描任务读写配额独立 |
| 缓存策略 | 磁盘层（日K/周K，复用 `qfq_daily_{code}.pkl` 语义 + 前复权可信标记）+ Redis 热层（分钟级 30–60s TTL） |
| 出口 | 可能需**多出口 IP** 分散请求；带宽成本随并发线性增长 |
| **合规风险** | ① 源站（腾讯/新浪/东财）接口条款是否允许**服务端代理分发**；② 本地单机使用 vs 服务端集中分发的**法律性质不同**；③ 源站限流随用户数放大 |
| 工作量 | **低**（复用现有解析逻辑，整体搬服务端） |
| 依赖 | 法务意见 + 源站频率上限认可 |

#### 7.3.3 正规授权源（**未来可替换，暂不采购**）—— 即原「路径 2 自建行情源」

| 项 | 设计 |
|---|---|
| 数据来源 | 采购**正规行情授权**（行情商 / 交易所授权数据商） |
| 适配层 | **整体重写**：字段映射、历史数据回补、**复权算法自实现**（现依赖腾讯前复权，换源后需自算并保证与历史缓存口径一致——直接影响因子值可复现性） |
| 成本模型 | 按席位 / 按流量 / 按用户数计费（**需采购报价**）；叠加自建接入与存储成本 |
| 稳定性 | 授权源 SLA 通常**优于**未公开接口，长期运维更可控 |
| **合规风险** | **低**（授权范围内分发） |
| 工作量 | **高**（适配层重写 + 复权算法 + 历史回补 + 因子值回归验证） |
| 依赖 | 采购决策 + 预算 |

#### 7.3.4 两路径对比（切换成本已由可插拔设计归零）

| 维度 | 路径 1 服务端代理 | 路径 2 自建行情源 |
|---|---|---|
| 合规风险 | **高**（需法务确认条款） | 低 |
| 工作量 | 低 | 高 |
| 因子值连续性 | 高（沿用现有前复权口径） | **需回归验证**（复权算法换代 → 历史因子值可能漂移） |
| 成本结构 | 带宽 + 出口 IP | 授权费 + 自建 |
| 与 `F#8` 关系 | 若允许用户切换源，代理压力进一步放大 | — |

> **状态：✅ 已决策（2026-09-12）—— 免费 API + 可插拔适配层生效**（对应上表左列）。
> **残留两项**：① 源站条款合规风险 → **接受态**，法务条件成熟时复核；② 换源时的复权口径与缓存键迁移 → 见 §7.3.1 要求 1/2（★需验证存量 pkl 迁移路径）。
> **已关闭一项**：`F#8` 数据源切换权限 → **✅ 已拍板为「不允许普通用户切换」**（§八）。→ 代理压力**不随用户数放大**（R-08 的"用户数放大"分量消解，仅剩"用户总量放大"分量，由规格 M 的容量与预热策略承担）。

---

## 八、C4 前待拍板项 —— ✅ 已全部拍板（2026-09-12 用户决策，10 条关闭）

> **状态更新**：原「6 条只列不给方案」（`F#4`~`F#9`）+ **升级项 `F#3`** + **`C1#2`** 已于 **2026-09-12** 由用户一次性拍板，**8 条关闭**；其后 `F#1` / `F#2` 亦于同日冻结 → **合计 10 条关闭，本节无开放项**。本节由「待拍板清单」转为**决策登记表**。

| # | 项 | 决策结果 ✅ | 影响 F-xxx | 落地影响 |
|---|---|---|---|---|
| **`C1#2`** | 成本模型（规格 S / 规格 M） | **规格 M**（300–500 DAU / 30–50 并发任务） | 全局部署 | 按 §7.2 规格 M 配置；**R-09 前置不因选 M 而消失**——实施路径仍为「规格 S 单副本灰度 → 全局状态全部外置 → 扩至 4 副本」 |
| `F#1` | F-802 是否承诺离线推送 | **不承诺**（webhook（企微/钉钉/飞书）+ 页内轮询红条，**不做 PWA Web Push**） | F-802、F-801 | §2.7 PWA 改为「不启用完整 PWA，仅静态资源化 + 可安装标签页」；F-802 C1 分级 **C → B**；`feature-matrix.md` §三 分布表 **B 42→43 / C 1→0**；**R-05 风险消除** |
| `F#2` | F-1402 试用起算基准 | **首次注册时间（账号维度）**；换设备不重置；桌面版老用户迁移后额外赠送 30 天 | F-1402 | `license.trial_first_use_at` = **账号注册时间**（§1.2 数据模型 / §3.2 能力 8 注）；「到期拦截 403」生效时点确定；迁移需登记桌面老用户并追加 30 天 |
| `F#3` | 绑定设备上限 N | **N = 2** | F-1403、F-1406 | 验收断言 = 「第 3 台设备绑定被拒 403」；`device` 表 `(account_id, device_token_hash)` 唯一约束 + 绑定计数校验 ≤ 2 |
| `F#4` | 额度计费时机 | **订阅制（包月 / 包年），不按次计量；试用期同样不扣减次数** | F-1404、F-501、F-701 | 「预扣不退 vs 成功后计费」之争**消解**：H5 无按次扣减。准入改为「订阅/试用有效性判定 + 到期拦截 403」；`license` 表不建 `quota_*` 计次列（见 §3.2 能力 8 注） |
| `F#5` | 扫描 / 回测计费时机 | **同上（与 `F#4` 同源）** | F-501、F-502、F-701 | 扫描/回测提交**不预扣、不计次**；任务创建与授权状态解耦 → **R-16 风险关闭** |
| `F#6` | 全市场扫描验收样本规模与耗时上限 | **样本 = 服务器 K 线缓存全量池（≈5215 只 × 300 根日K）；验收前提 = 命中服务器热缓存**（数据在服务端，不触发源站抓取）；**耗时上限沿用 ≤30 min** | F-502 | 「样本规模」不再是瓶颈变量（数据侧已在服务端）；验收关注点转为**计算链路**（用户选定策略方案 → 服务端基于缓存数据算分）。★耗时上限仍为占位值，P1 用实测校准（前置见 R-15） |
| `F#7` | 是否保留用户自定义板块映射 | **保留** | F-505 | `sector_map` 按账号隔离表**落地**（非可选项）；合并优先级 = **用户映射 > 内置 26 板块**（同 sector 键以用户值为准，未被覆盖的内置板块保留） |
| `F#8` | 数据源是否允许普通用户切换 | **不允许** | F-1001 | 服务端全局配置（运维动作）即**最终形态**；`PUT /api/data-sources` **收权限**（普通用户 403），`GET` 保留只读；按用户维度表**不建** |
| `F#9` | 会话冲突处置策略 | **踢旧**（新会话胜出，旧会话强制下线） | F-101 | 验收由 `role='readonly'` 改为「旧会话被强制登出（后续心跳/请求返回 401）」；**复用 F-102 心跳通道（5s）下发 revoke**，无需新增推送通道 |

> **无开放项（2026-09-12）**：原「2 条待拍板项」已全部关闭 ——
> - `F#1`（F-802 是否承诺离线推送）= **不承诺** → 走 webhook + 页内轮询红条，**不做 PWA Web Push**（见 §2.7 与表内 `F#1` 行）。
> - `F#2`（F-1402 试用起算基准）= **首次注册时间（账号维度）**，换设备不重置、迁移补偿 30 天（见 §3.2 能力 8 注与表内 `F#2` 行）。

---

## 九、完成后自检

| # | 自检项 | 结果 | 说明 |
|---|---|---|---|
| 1 | 技术栈 12 项是否全部决策？ | ✅ **12/12** | §2.1–§2.12 逐项给「推荐 + 理由 + 备选 + 落地依据」，另加 §2.13 全局硬约束 3 项 |
| 2 | 后端 API 清单是否覆盖全部 42 条 B 类？逐条核对，输出覆盖率 | ✅ **42/42 = 100%** | §3.3 逐条核对表（42 行，F-104 … F-1603），按**唯一归属**核对，无遗漏无重复；归属条数校验 7+3+10+4+6+5+2+4+0+0+1 = **42** |
| 3 | 数据迁移表是否覆盖 feature-matrix §5 全部项？ | ✅ **15/15 逐项有结论** | §4.1 覆盖 `quant_model.json`/`usage.json`/`data_sources.json`/`monitor.json`/`notifiers+credentials`/`discipline_journal`/`emotion_journal`/`license+machine_id+.lstate`/`watchlist*`/`cache/kline`/`reports/`/`decision_cards/`/`monitor_state.json`/`sector_map.json`，见 §4.1 表；另 §4.2 提炼 5 个关键迁移点。**无空项** |
| 4 | 两个阻塞决策（`C1#1`/`#2`/`#3` 三条）是否都给了分支？`C1#1` 的 A/B 两分支是否都写了完整架构差异？ | ✅ **3/3 曾给分支，3/3 已决策** | 初版：`C1#1` §1.2 给 A/B 四维度完整差异；`C1#2` §7.2 两套部署规格；`C1#3` §7.3 两路径差异。**2026-09-12 更新**：`C1#1` → ✅ 选定**分支 A**（形态冻结，B 存档）；`C1#3` → ✅ 选定**免费 API + 可插拔适配层**（§7.3.1 接口契约 / 注册机制 / 四条落地要求）；`C1#2` → ✅ 选定**规格 M**（规格 S 转存档兼实施前置阶段，§7.2） |
| 5 | 是否引用了 feature-matrix 的组件清单与平台差异清单？ | ✅ **逐条引用未重写** | 组件清单：§2.1/2.2/2.3/2.4/2.7/2.9/2.10 的"落地依据"均引用 `feature-matrix.md` §六 对应行（L139-152），含「SSE 进度组件」「结果虚拟滚动表格」「配置页 8 子页签组件」「HTTP/SSE 客户端层（26 处 pywebview）」等原文表述；平台差异清单：**§五 整表逐条照搬**（含影响 F-xxx 与原文风险描述），未凭记忆重写 |
| 6 | 「C4 前待拍板项」是否完整列出？ | ✅ **10/10 已拍板** | §八 由「待拍板清单」转为**决策登记表**：`C1#2`（规格 M）+ `F#1`（不承诺离线推送）+ `F#2`（试用按首次注册时间）+ `F#3`（N=2）+ `F#4`/`F#5`（订阅制）+ `F#6` + `F#7` + `F#8` + `F#9` 逐条给「决策结果 / 影响 F-xxx / 落地影响」；**无开放项** |
| 7 | 决策状态是否已同步冻结？ | ✅ **已冻结 12/12** | `C1#1` → **分支 A 生效**（§1.1 / §1.2 / §1.3 / §7.1）；`C1#3` → **免费 API + 可插拔生效**（§7.3 + §3.2 能力 1 + R-08）；`C1#2` → **规格 M 选定**（§7.2 + 速览表 + §八）、`F#1` → **不承诺离线推送**（§2.7 + R-05 + 速览表 + §八）、`F#2` → **首次注册时间**（§1.2 数据模型 + §3.2 能力 8 注 + 速览表 + §八）、`F#3` → **N=2**、`F#4`/`F#5` → **订阅制**、`F#6`/`F#7`/`F#8`/`F#9` → 见 §八。**无未冻结项** |

**补充自检（本步骤主动发现并处理的两项）**

| # | 项 | 处理 |
|---|---|---|
| A1 | **编号体系错位** | 任务书正文的 "`#1` 授权模式 / `#2` 成本模型 / `#3` 数据源合规" 来自 `h5-feasibility.md` §4.5，而 "`#4`~`#9`" 来自 `feature-matrix.md` §四 —— 两套编号被拼成 1–9。本文以**〇章「编号约定」**显式区分（`C1#n` vs `F#n`），并按正确映射执行：C3 必处理 = `C1#1`/`C1#2`/`C1#3`；只列不给方案 = `F#4`~`F#9`（**该 6 条已于 2026-09-12 全部拍板，见 §八**） |
| A2 | **一处引用的理由已失效** | 任务书「服务端必须锁依赖版本（`engine/data_layer.py:628-641` 已记录跨 pandas 版本 pickle 不兼容）」——**实测发现该风险已于 2026-08-02 通过改存储格式（`dict(orient='list')`）规避**，注释明确"跨 pandas 版本读写均兼容"。锁版本结论仍成立，但**理由已更正为 4 条**（浮点/算法漂移、matplotlib 产物一致性、cryptography 验签、`_qfq_flag` 残余面），见 §2.13 修正块。**未沿用失效论据** |
| A3 | **分支 B 的统计联动** | ✅ **已随决策关闭**：`C1#1` 选定分支 A → `feature-matrix.md` §三 的 **D=0 / E=5 无需修订**，F-1403 维持 E。分支 B 的联动规则（D→1、E→4）转存档备查（§1.2） |

**结论：7/7 通过（另附 3 项主动发现）。**

---

*C3 架构设计 **v1.2**（2026-09-12，含当日两轮决策冻结）。依据 `docs/h5-feasibility.md`（C1）+ `docs/feature-matrix.md`（C2）产出。**已全部冻结（12 项）**：`C1#1` 授权模式 = **分支 A**（形态 = `H5 + 后端`）；`C1#3` 数据源 = **免费 API + 可插拔适配层**；`C1#2` 成本模型 = **规格 M**；`F#1` = **不承诺离线推送**（webhook + 页内轮询红条，不做 PWA Web Push）；`F#2` = **试用按首次注册时间（账号维度）起算**；`F#3` 绑定上限 = **N=2**；`F#4`/`F#5` = **订阅制（包月/包年，不按次计量，试用不扣次数）**；`F#6` 扫描验收 = **服务器缓存全量池**；`F#7` = **保留用户自定义板块映射**；`F#8` = **不允许普通用户切换数据源**；`F#9` = **会话踢旧**。**无未冻结项。**本文不含代码；接口契约以 §3.2 为准。*

---

## 决策记录（追加）

- 2026-09-12：F#1 定案【不承诺离线推送】——webhook + 页内轮询；不做 PWA Web Push；R-05 风险消除
- 2026-09-12：F#2 定案【首次注册时间】——账号维度起算，换设备不重置；迁移补偿 30 天
