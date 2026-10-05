# M-Bull 前端 · UI 设计规范（混合提炼）

> 目标：以「近黑深蓝行情终端」为骨架（TradingView 系），借「企业级设计系统」的工程化细节（Arco / Ant Design 的间距、字号、圆角、层级），做**纯视觉层**规范。
> 约束遵循项目既定：不改信息架构、不改数据结构、不动交互逻辑、功能与既有实现严格对应。

## 来源对照

| 维度 | 来源 | 应用点 |
| --- | --- | --- |
| 背景/面板/文字色阶 | TradingView 深色终端 #131722 系 | 全局骨架 |
| 深色容器层级（bg-1..5） | TradingView + Arco dark | 卡片 / 面板 / 浮层 |
| 字号阶梯 | Arco Design token | 标题 / 正文 / 辅助 |
| 间距（4 的倍数） | Arco + Ant Design | 卡片内边距、栅格 |
| 圆角 / 阴影 / 按钮态 | Ant Design 4.x default.less | 按钮、输入、标签 |
| 等宽数字 & 涨跌底色块 | TradingView 专业行情惯例 | 价格 / 因子分 |

---

## 一、色板（CSS 变量）

骨架三色沿用项目既有约定（`theme.css` 已存在，逐字节保留），补充若干高级变量。

### 1.1 中性色 · 背景与面板

| Token | 值 | 用途 |
| --- | --- | --- |
| `--mbull-bg` | `#131722` | 整体背景（TradingView 骨架） |
| `--mbull-bg-soft` | `#1c2230` | 导航 / 次级背景 |
| `--mbull-panel` | `#1e222d` | 卡片面板（一级容器） |
| `--mbull-panel-2` | `#232a36` | 卡片内嵌区块（二级容器） |
| `--mbull-panel-3` | `#2a323f` | 悬浮 / hover 区 |
| `--mbull-border` | `rgba(255,255,255,0.08)` | 常规边框 |
| `--mbull-border-strong` | `rgba(255,255,255,0.14)` | 强调边框 / 底部导航激活描边 |

### 1.2 文字

| Token | 值 | 用途 |
| --- | --- | --- |
| `--mbull-text` | `#d1d4dc` | 主文本 |
| `--mbull-text-3` | `rgba(255,255,255,0.5)` | 次要信息（Arco text-3 dark） |
| `--mbull-text-dim` | `#787b86` | 辅助 / 置灰（TradingView 灰） |
| `--mbull-text-disabled` | `rgba(255,255,255,0.3)` | 禁用 |

### 1.3 语义色（红涨绿跌，A股惯例）

| Token | 值 | 用途 |
| --- | --- | --- |
| `--mbull-up` | `#ef5350` | 红 · 上涨 / 买入 / 正向 |
| `--mbull-up-bg` | `rgba(239,83,80,0.12)` | 红色块底色 |
| `--mbull-down` | `#26a69a` | 绿 · 下跌 / 卖出 / 负向 |
| `--mbull-down-bg` | `rgba(38,166,154,0.12)` | 绿色块底色 |
| `--mbull-accent` | `#2962ff` | 品牌强调 / 激活指示 |

> 底色调低饱和度、亮度（非纯 #f00/#0f0），匹配 TradingView「软化默认红绿」原则，降低长时盯盘眼疲劳。

---

## 二、字体

| Token | 值 |
| --- | --- |
| `--mbull-font` | `-apple-system, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif` |
| `--mbull-font-mono` | `'JetBrains Mono', 'Roboto Mono', Menlo, Consolas, 'Courier New', monospace` |

### 2.1 字号阶梯（Arco 参考）

| 层级 | 值 | 用途 |
| --- | --- | --- |
| caption | `12px` | 辅助文案 / 水印 |
| body-1 | `12px` | 次要信息 / 标签 |
| body-3 | `14px` | 正文 / 卡片体 |
| title-1 | `16px` | 卡片标题 |
| title-2 | `20px` | 区块标题 |
| title-3 | `24px` | 指标主值 |
| price-lg | `28–36px` | 大字号价格（加粗） |

### 2.2 数字呈现规则（行情终端）

- 所有**数字 / 价格 / 因子分 / 涨跌幅**使用 `--mbull-font-mono`，加 `font-variant-numeric: tabular-nums`（等宽对齐，跳数不抖）。
- 涨跌幅走高信息密度：粗体等宽 + 带红/绿底块。

---

## 三、间距（4 的倍数，Arco / AntD）

| Token | 值 |
| --- | --- |
| space-1 | `4px` |
| space-2 | `8px` |
| space-3 | `12px` |
| space-4 | `16px` |
| space-5 | `24px` |

- 卡片内边距：`16px`；卡片间纵向间距：`16px`；区块间距：`24px`。

---

## 四、圆角 / 阴影 / 描边（AntD 参考）

| Token | 值 |
| --- | --- |
| `--mbull-radius` | `8px`（卡片）|
| `--mbull-radius-sm` | `6px`（按钮 / 标签）|
| `--mbull-radius-lg` | `12px`（浮层）|
| 边框 | `--mbull-border` 1px |
| 按钮活跃 hov 态 | hover 提亮、click 加深（遵循显式 -1 阶语义） |

---

## 五、组件形态规范

### 5.1 底部导航（证券 App 惯例，红色激活指示）
- 实底背景：`--mbull-bg-soft`；顶描边 `--mbull-border-strong`。
- 激活项：主文字色 + 顶部 2px `--mbull-up`（红）激活条；未激活：`--mbull-text-dim`。
- (说明：导航形态的位移属布局调整，落地若需保留原顶部结构可仅做激活视觉增强，改动前与产品确认)

### 5.2 价格/涨跌幅块
- 容器：`padding: 2px 6px; border-radius: 4px; background: 上/下底块色`。
- 文本：`--mbull-font-mono`、粗体、tabular-nums。

### 5.3 卡片
- `background: --mbull-panel; border: 1px solid --mbull-border; border-radius: --mbull-radius`。
- 内嵌分组用 `--mbull-panel-2` 分层，避免大面积纯色平铺。

### 5.4 按钮 / 标签
- 主按钮 `--mbull-accent` 实底白字；次级描边 `--mbull-border-strong`。
- 涨/跌方向标签用对应语义底块。

---

## 六、落地映射

- `web/src/styles/theme.css`：补充上述变量的全局声明 + 等宽数字/底块的通用工具类（`.num`、`.pct-block`、`.up-bg`/`.down-bg`）。
- 各视图样式尽量复用变量，避免硬编码色值。
- 决策卡（`DecisionCard.vue`）为**白底截图导出专用**，**不套用本深色规范**，保持白底。