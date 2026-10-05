# -*- coding: utf-8 -*-
"""ORM 模型（能力 5）：`scheme`（方案壳）+ `factor_profile`（因子体）分表。

依据 app-architecture.md §4.1 逐项迁移表：`config/quant_model.json`（`schemes` +
`factor_profiles` + 三级合并）→ PG `scheme` + `factor_profile` 两表，配置体用 JSONB。

字段与 engine `quant_model.json` 的对应：
- `scheme` 行 = `schemes[name]`，其中 `_meta.{market,direction,period,mode}` 拆成独立列
  （便于按市场/方向/周期检索），`config` / `period_configs` 保留为 JSON 体。
- `factor_profile` 行 = `factor_profiles[name]`（`active_factors` / `factor_configs` /
  `score_scale` 三键整体存 `body`）。

乐观锁：两表各带 `version` 列（整数，从 1 起），CAS 写用 `WHERE version=:expected`。
时间列存 `server.core.time.now_iso()` 的 ISO 字符串（跨库一致、避免裸 datetime.now）。
"""

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 声明式基类。"""


def json_body_type():
    """JSON 体类型：生产 PG 落 JSONB，其余方言（测试 SQLite）落 JSON/TEXT。

    每次调用返回新实例——SQLAlchemy 的 TypeEngine 实例不建议跨列复用。
    """
    return JSON().with_variant(postgresql.JSONB(), "postgresql")


class Scheme(Base):
    """方案壳：一名一行。"""

    __tablename__ = "scheme"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(64), nullable=True)
    desc: Mapped[str] = mapped_column(Text, nullable=True)
    # _meta.* 拆列（检索用）
    market: Mapped[str] = mapped_column(String(16), nullable=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=True)
    period: Mapped[str] = mapped_column(String(16), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=True)
    # 引用的因子 profile 名（'stock' / 'futures' / 自定义）
    factor_profile: Mapped[str] = mapped_column(String(64), nullable=True)
    # 方案内联 config + 遗留 period_configs（三级合并的输入）
    config: Mapped[dict] = mapped_column(json_body_type(), nullable=False, default=dict)
    period_configs: Mapped[dict] = mapped_column(json_body_type(), nullable=False, default=dict)
    # 乐观锁版本号（CAS）
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class FactorProfile(Base):
    """因子体：`active_factors` / `factor_configs` / `score_scale` 三键整体存 `body`。"""

    __tablename__ = "factor_profile"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    body: Mapped[dict] = mapped_column(json_body_type(), nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


# ---------------------------------------------------------------- 能力 8：授权（F-1401，P2a）
# app-architecture.md §4.1：`account` / `device` / `license` 三表。
#   - 判定主体 = 账号维度（F#2 试用按首次注册时间）；响应不含 machine_id（F-1401 验收）。
#   - F#3 绑定上限 N=2：`device` 表收紧到 `(account_id, device_token_hash)` 唯一约束，
#     绑定计数由「未 revoked 的 device 行数」判定。
#   - F#4 订阅制：`license` 表不建 quota_* 计次列，只存授权状态与有效期。
# 时间列统一 `server.core.time.now_iso()`（R-19：禁裸 datetime.now）。


class Account(Base):
    """账号主表：一名一行。"""

    __tablename__ = "account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 登录标识（手机号 / 邮箱），唯一索引用于快速命中
    identifier: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    # self-describing PBKDF2 摘要：`pbkdf2:sha256:200000:<salt_hex>:<hash_hex>`
    pwd_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


class Device(Base):
    """设备绑定：HttpOnly cookie 明文 token 的 SHA-256 作唯一键。"""

    __tablename__ = "device"
    __table_args__ = (
        UniqueConstraint("account_id", "device_token_hash", name="uq_device_account_token"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"), index=True)
    device_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    ua: Mapped[str] = mapped_column(String(255), nullable=True)
    bound_at: Mapped[str] = mapped_column(String(32), nullable=False)
    last_seen_at: Mapped[str] = mapped_column(String(32), nullable=False)
    revoked_at: Mapped[str] = mapped_column(String(32), nullable=True)


class License(Base):
    """账号授权（1:1）。订阅制：无计次列，只存状态/档位/有效期与试用基准。"""

    __tablename__ = "license"

    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"), primary_key=True)
    tier: Mapped[str] = mapped_column(String(16), nullable=True)  # trial / monthly / yearly / lifetime
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="trial")
    expires_at: Mapped[str] = mapped_column(String(32), nullable=True)
    # F#2：试用起算 = 首次注册时间（账号维度），换设备不重置
    trial_first_use_at: Mapped[str] = mapped_column(String(32), nullable=True)
    sub_cycle: Mapped[str] = mapped_column(String(10), nullable=True)  # monthly / yearly


# ---------------------------------------------------------------- 能力 6：账本存储（纪律 + 情绪）
# app-architecture.md §4.1：`signal` + `cooldown` + `emotion_record` 三表。
#  - `cooldown` 全局跨市场、单例（pk=1）。
#  - 时间列统一 `server.core.time.now_iso()`（R-19：禁裸 datetime.now）。
#  - `execution` 存 JSON 体（`{executed, exec_price, exec_ratio, at}`），镜像桌面
#    discipline_log.set_execution；不拆列（回归宽松 .get() 兜底）。


class Signal(Base):
    """纪律信号：仅 3 入口（分析/期货分析/监控）落册；全市场扫描不落册（F-1102 硬约束）。"""

    __tablename__ = "signal"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[str] = mapped_column(String(32), nullable=False)
    code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=True)
    market: Mapped[str] = mapped_column(String(16), nullable=False, default="stock")
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="long")
    period: Mapped[str] = mapped_column(String(16), nullable=True)
    scheme: Mapped[str] = mapped_column(String(64), nullable=True)
    signal_type: Mapped[str] = mapped_column(String(16), nullable=False)  # buy/add/reduce/clear
    signal_label: Mapped[str] = mapped_column(String(32), nullable=True)
    trigger_price: Mapped[float] = mapped_column(nullable=False, default=0)
    factor_score: Mapped[float] = mapped_column(nullable=True)
    position_ratio: Mapped[float] = mapped_column(nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=True)
    execution: Mapped[dict] = mapped_column(json_body_type(), nullable=True)
    latest_price: Mapped[float] = mapped_column(nullable=False, default=0)
    latest_at: Mapped[str] = mapped_column(String(32), nullable=False)


class Cooldown(Base):
    """冷却状态（全局跨市场，单例 pk=1）。镜像桌面 cooldown_status 口径。"""

    __tablename__ = "cooldown"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # 恒为 1
    active: Mapped[bool] = mapped_column(default=False)
    reason: Mapped[str] = mapped_column(Text, nullable=True)
    started_at: Mapped[str] = mapped_column(String(32), nullable=True)
    until: Mapped[str] = mapped_column(String(32), nullable=True)
    bias_pct: Mapped[float] = mapped_column(nullable=False, default=0)
    threshold_pct: Mapped[float] = mapped_column(nullable=False, default=-8.0)
    duration_days: Mapped[int] = mapped_column(nullable=False, default=3)


class EmotionRecord(Base):
    """情绪化操作记录：`op_price` 服务端锁定（前端传入一律忽略），随现价刷新生效。"""

    __tablename__ = "emotion_record"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[str] = mapped_column(String(32), nullable=False)
    code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=True)
    market: Mapped[str] = mapped_column(String(16), nullable=False, default="stock")
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # buy/add/reduce/sell/clear
    action_cn: Mapped[str] = mapped_column(String(16), nullable=True)
    tag: Mapped[str] = mapped_column(String(16), nullable=False, default="其他")
    note: Mapped[str] = mapped_column(Text, nullable=True)
    op_price: Mapped[float] = mapped_column(nullable=False, default=0)  # 锁定，前端不可覆盖
    latest_price: Mapped[float] = mapped_column(nullable=False, default=0)
    latest_at: Mapped[str] = mapped_column(String(32), nullable=False)
