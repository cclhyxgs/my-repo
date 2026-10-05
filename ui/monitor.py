# -*- coding: utf-8 -*-
"""L3 监控守护（形态 A：app 内后台线程）。

设计要点：
  - 复用现有「自选诊断」(web_api.start_diagnosis)：诊断产出里天生带
    'triggeredConditions'（已触发的条件），无需另建信号层。
  - 按市场分组、各自按交易时段扫描：A 股(9:30-11:30/13:00-15:00)、
    期货日盘、期货夜盘（夜盘 21:00 起——形态 A 下需 app 开着才生效，属已知边界）。
  - 每个市场可独立配置：enabled（开关）、k_type（诊断周期，A股/期货可不同）、
    sessions（交易时段区间）。全部从 config/monitor.json 读取，不硬编码。
  - 扫描节拍 interval_seconds 可配；align_to_grid=True 时对齐到整点/整刻网格。
  - 触发判定：triggeredConditions 非空且不是「无触发兜底文案」即视为触发。
  - 去重：对比上次扫描的触发状态，仅「新增/变化」才推送，避免反复打扰。
  - 推送经 L4 Notifier 注册表分发到启用的渠道（应用内气泡/微信/Server酱等）。

时段、间隔、分市场 k_type/开关均从 config/monitor.json 读取，缺省用内置 DEFAULT_*。
"""

import json
import logging
import os
import threading
import time
from datetime import datetime

logger = logging.getLogger("M-Bull.monitor")

# ── 无触发的兜底文案（来自 web_api.analyze 的 triggered_conditions 默认值）──
_NO_TRIGGER_PHRASES = {"正常持有", "未触及建仓参考条件", "回避"}

# ── 默认交易时段（分钟区间 [start, end]，含端点；hm = hour*60+minute）──
# 可被 config/monitor.json 的 sessions 完整覆盖。
DEFAULT_SESSIONS = {
    "stock": [
        (570, 690),   # 9:30-11:30
        (780, 900),   # 13:00-15:00
    ],
    "futures_day": [
        (540, 615),   # 9:00-10:15
        (630, 690),   # 10:30-11:30
        (810, 900),   # 13:30-15:00
    ],
    "futures_night": [
        (1260, 1380), # 21:00-23:00（部分品种更晚，按需改 monitor.json）
    ],
}
DEFAULT_INTERVAL_SECONDS = 900  # 15 分钟
_MAX_DIAG_WAIT_SECONDS = 600    # 单次诊断轮询上限（10 分钟）


def _now_hm():
    t = datetime.now()
    return t.weekday(), t.hour * 60 + t.minute


def _has_trigger(triggered_conditions):
    """判定一只票是否真的触发了信号（排除无触发的兜底文案）。"""
    tc = (triggered_conditions or "").strip()
    if not tc:
        return False
    return tc not in _NO_TRIGGER_PHRASES


class Monitor:
    """后台监控守护。由 web_api.start_monitor() 懒加载启动。

    api 需实现：load_watchlist(market) -> {'content': str}、
    start_diagnosis(text, market, k_type) -> {'task_id': str}、
    get_diagnosis_progress(task_id) -> {'done': bool}、
    以及实例字典 _diag_tasks[task_id]['results']。
    """

    def __init__(self, api, interval=None, state_path=None, config_dir=None):
        self.api = api
        self._stop = threading.Event()
        self._thread = None
        self._scanning = False

        # 配置目录 → 读 monitor.json
        self._config_dir = config_dir
        self.interval = interval if interval is not None else DEFAULT_INTERVAL_SECONDS
        # 分市场默认配置：两市场都开、日K、内置时段（被 monitor.json 覆盖）
        self.align_to_grid = True
        self.markets = {
            "stock": {"enabled": True, "k_type": "日K", "sessions": DEFAULT_SESSIONS["stock"], "strategy": None},
            "futures": {
                "enabled": True,
                "sessions": DEFAULT_SESSIONS["futures_day"] + DEFAULT_SESSIONS["futures_night"],
                "long": {"enabled": True, "k_type": "日K",
                         "sessions": DEFAULT_SESSIONS["futures_day"] + DEFAULT_SESSIONS["futures_night"],
                         "strategy": None},
                "short": {"enabled": True, "k_type": "日K",
                          "sessions": DEFAULT_SESSIONS["futures_day"] + DEFAULT_SESSIONS["futures_night"],
                          "strategy": None},
            },
        }
        self._load_config()

        # state 持久化路径：与 monitor.json 同放 config/ 目录（统一配置位置）。
        # 兼容旧版：根目录 monitor_state.json 存在时自动迁移（读旧→写新→删旧），避免丢去重状态。
        if state_path is None:
            try:
                from engine.config import get_app_dir
                _cfg_dir = os.path.join(get_app_dir(), "config")
                _new_path = os.path.join(_cfg_dir, "monitor_state.json")
                _old_path = os.path.join(get_app_dir(), "monitor_state.json")
                try:
                    os.makedirs(_cfg_dir, exist_ok=True)
                except Exception:
                    pass
                if os.path.isfile(_old_path) and not os.path.isfile(_new_path):
                    try:
                        import shutil as _sh
                        _sh.copy2(_old_path, _new_path)
                        os.remove(_old_path)
                        logger.info("[monitor] 已迁移 monitor_state.json 到 config/")
                    except Exception as e:
                        logger.warning("[monitor] monitor_state.json 迁移失败: %s", e)
                state_path = _new_path
            except Exception:
                state_path = os.path.join(os.getcwd(), "monitor_state.json")
        self._state_path = state_path
        self._state = self._load_state()

        # 通知注册表（L4）：读 config/notifiers.json，凭据注入 credentials.json。
        # 失败则 _notifiers=None，_push 回退到纯日志，不阻断监控。
        self._notifiers = None
        try:
            from engine.notifiers.registry import NotifierRegistry
            self._notifiers = NotifierRegistry.load(config_dir=self._config_dir, api=self.api)
        except Exception as e:
            logger.warning("[monitor] 通知注册表加载失败，仅日志推送: %s", e)

    # ── 配置 / 状态持久化 ──────────────────────────────────────────────
    def _load_config(self):
        try:
            if self._config_dir is None:
                try:
                    from engine.config import get_app_dir
                    self._config_dir = os.path.join(get_app_dir(), "config")
                except Exception:
                    return
            p = os.path.join(self._config_dir, "monitor.json")
            if not os.path.exists(p):
                logger.info("[monitor] 无 monitor.json，用内置默认配置")
                return
            with open(p, encoding="utf-8") as f:
                cfg = json.load(f) or {}
            self.interval = int(cfg.get("interval_seconds", self.interval))
            self.align_to_grid = bool(cfg.get("align_to_grid", self.align_to_grid))

            markets = cfg.get("markets")
            if markets:
                # 新 schema：markets.{stock,futures}.{enabled,k_type,sessions}；
                # 期货为多空双槽位 markets.futures.{enabled,long,short}（各含 strategy/k_type/sessions）
                parsed = {}
                for mk in ("stock", "futures"):
                    mcfg = markets.get(mk)
                    if not mcfg:
                        continue
                    default_sessions = (
                        DEFAULT_SESSIONS["stock"] if mk == "stock"
                        else DEFAULT_SESSIONS["futures_day"] + DEFAULT_SESSIONS["futures_night"]
                    )
                    if mk == "futures":
                        long_cfg = mcfg.get("long") or {}
                        short_cfg = mcfg.get("short") or {}
                        legacy_strategy = mcfg.get("strategy") or None
                        # 旧格式兼容：顶层 strategy → 归属 long（无法区分方向时默认多单）
                        if "long" not in mcfg and "short" not in mcfg and legacy_strategy:
                            long_cfg = {"strategy": legacy_strategy}
                        parsed[mk] = {
                            "enabled": bool(mcfg.get("enabled", True)),
                            "sessions": mcfg.get("sessions") or default_sessions,
                            "long": {
                                "enabled": bool(long_cfg.get("enabled", True)),
                                "k_type": long_cfg.get("k_type", "日K"),
                                "sessions": long_cfg.get("sessions") or default_sessions,
                                "strategy": long_cfg.get("strategy") or None,
                            },
                            "short": {
                                "enabled": bool(short_cfg.get("enabled", True)),
                                "k_type": short_cfg.get("k_type", "日K"),
                                "sessions": short_cfg.get("sessions") or default_sessions,
                                "strategy": short_cfg.get("strategy") or None,
                            },
                        }
                    else:
                        parsed[mk] = {
                            "enabled": bool(mcfg.get("enabled", True)),
                            "k_type": mcfg.get("k_type", "日K"),
                            "sessions": mcfg.get("sessions") or default_sessions,
                            # strategy：方案名（用户自选策略）；None=自动（按市场+方向解析）
                            "strategy": mcfg.get("strategy") or None,
                        }
                if parsed:
                    self.markets = parsed
                logger.info("[monitor] 已加载分市场监控配置 interval=%ss align=%s markets=%s",
                            self.interval, self.align_to_grid, list(self.markets.keys()))
            else:
                # 旧 schema 兼容：顶层 sessions + 两市场共用 k_type=日K
                legacy = cfg.get("sessions")
                if legacy:
                    self.markets = {
                        "stock": {"enabled": True, "k_type": "日K",
                                  "sessions": legacy.get("stock", DEFAULT_SESSIONS["stock"])},
                        "futures": {"enabled": True, "k_type": "日K",
                                    "sessions": legacy.get("futures_day", [])
                                    + legacy.get("futures_night", [])},
                    }
                logger.info("[monitor] 已加载(旧格式)监控配置 interval=%ss", self.interval)
        except Exception as e:
            logger.warning("[monitor] 读取 monitor.json 失败，用默认: %s", e)

    def _load_state(self):
        try:
            if os.path.exists(self._state_path):
                with open(self._state_path, encoding="utf-8") as f:
                    return json.load(f) or {}
        except Exception as e:
            logger.warning("[monitor] 读取 monitor_state 失败: %s", e)
        return {}

    def _save_state(self):
        try:
            d = os.path.dirname(self._state_path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self._state_path, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("[monitor] 保存 monitor_state 失败: %s", e)

    # ── 时段判定 ──────────────────────────────────────────────────────
    def _in_session(self, market):
        m = self.markets.get(market)
        if not m or not m.get("enabled"):
            return False
        wd, hm = _now_hm()
        if wd >= 5:
            return False  # 周末休市
        for (s, e) in m.get("sessions", []):
            if s <= hm <= e:
                return True
        return False

    # ── 生命周期 ──────────────────────────────────────────────────────
    def start(self):
        if self._thread and self._thread.is_alive():
            logger.info("[monitor] 监控线程已在运行")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="monitor-daemon")
        self._thread.start()
        logger.info("[monitor] 启动后台监控线程 interval=%ss", self.interval)

    def stop(self):
        self._stop.set()
        logger.info("[monitor] 监控线程已请求停止")

    # ── 主循环 ────────────────────────────────────────────────────────
    def _loop(self):
        logger.info("[monitor] 监控守护循环开始")
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.exception("[monitor] tick 异常: %s", e)
            self._sleep_until_next_tick()

    def _sleep_until_next_tick(self):
        """等待到下一个周期。

        - align_to_grid=True：对齐到「本地零点起的 interval 网格」，使扫描落在
          整点/整刻（如 15 分钟节拍 → 9:00/9:15/9:30…），便于用户预期。
        - align_to_grid=False 或 interval<=0：固定 wait(interval)。
        stop() 会立即唤醒，不分昼夜。
        """
        if self.interval <= 0:
            self._stop.wait(60)
            return
        if not self.align_to_grid:
            self._stop.wait(self.interval)
            return
        now = time.time()
        lt = time.localtime(now)
        secs_into_day = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
        delta = self.interval - (secs_into_day % self.interval)
        if delta <= 0:
            delta = self.interval
        self._stop.wait(delta)

    def _tick(self):
        if self._scanning:
            logger.debug("[monitor] 上次扫描仍在进行，跳过本次")
            return
        # 避开用户手动诊断运行期：监控是后台任务，不抢占手动诊断的资源/授权额度
        if self._has_manual_diag_running():
            logger.info("[monitor] 用户手动诊断进行中，本轮扫描跳过（不抢占）")
            return
        self._scanning = True
        try:
            for market, m in self.markets.items():
                if not m.get("enabled"):
                    continue
                if self._stop.is_set():
                    return
                if not self._in_session(market):
                    continue
                if market == "futures":
                    # 期货多空双槽位：各自独立扫描（direction 过滤自选中的对应方向标的）
                    for direction in ("long", "short"):
                        if self._stop.is_set():
                            return
                        slot = m.get(direction)
                        if not slot or not slot.get("enabled"):
                            continue
                        self._scan_market(market, slot.get("k_type", "日K"), slot.get("strategy"), direction=direction)
                else:
                    self._scan_market(market, m.get("k_type", "日K"), m.get("strategy"))
        finally:
            self._scanning = False

    def _has_manual_diag_running(self):
        """是否有用户手动发起的诊断任务仍在运行（监控应避开，不抢占资源/额度）。"""
        try:
            tasks = getattr(self.api, "_diag_tasks", {})
            for t in tasks.values():
                if isinstance(t, dict) and t.get("is_running") and t.get("source") != "monitor":
                    return True
        except Exception:
            pass
        return False

    # ── 单次市场扫描 ──────────────────────────────────────────────────
    def _scan_market(self, market, k_type, strategy=None, direction=None):
        try:
            wl = self.api.load_watchlist(market=market).get("content", "").strip()
        except Exception as e:
            logger.warning("[monitor] 读取自选失败(%s): %s", market, e)
            return
        if not wl:
            logger.debug("[monitor] %s 自选为空，跳过", market)
            return
        # 监控已去掉「自动」占位：槽位未选方案 = 未配置，跳过扫描（不再回落市场+方向解析）
        if not strategy:
            logger.warning("[monitor] %s%s 槽位未选方案，跳过扫描（请到「监控设置」选一个具体方案）",
                           market, (":" + direction if direction else ""))
            return

        try:
            ret = self.api.start_diagnosis(wl, market=market, k_type=k_type, scheme_name=strategy,
                                           direction=direction, source='monitor')
        except Exception as e:
            logger.warning("[monitor] 启动诊断失败(%s%s): %s", market, (":" + direction if direction else ""), e)
            return
        if not isinstance(ret, dict) or "task_id" not in ret:
            logger.warning("[monitor] start_diagnosis 返回异常(%s%s): %s", market, (":" + direction if direction else ""), ret)
            return

        task_id = ret["task_id"]
        # 轮询等待诊断完成
        waited = 0
        while waited < _MAX_DIAG_WAIT_SECONDS:
            if self._stop.is_set():
                return
            try:
                prog = self.api.get_diagnosis_progress(task_id)
            except Exception as e:
                logger.warning("[monitor] 查询诊断进度异常(%s): %s", market, e)
                return
            if prog.get("done"):
                break
            time.sleep(5)
            waited += 5
        else:
            logger.warning("[monitor] 诊断超时未结束(%s) task=%s", market, task_id)
            return

        task = getattr(self.api, "_diag_tasks", {}).get(task_id)
        if not task:
            logger.warning("[monitor] 找不到诊断任务(%s) task=%s", market, task_id)
            return

        results = task.get("results", [])
        logger.info("[monitor] %s 诊断完成 n=%d", market, len(results))
        self._process_results(market, results, direction=direction)

    # ── 提取触发 + 去重 + 推送 ────────────────────────────────────────
    def _process_results(self, market, results, direction=None):
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        # state key 区分方向：期货多单/空单各自独立去重（互不覆盖）
        state_key = f"{market}:{direction}" if direction else market
        fired = []           # (code, name, tc, result)
        current_state = {}   # code -> tc（空串表示无触发）

        for r in results:
            code = r.get("code")
            name = r.get("name", "")
            tc = (r.get("triggeredConditions") or "").strip()
            if _has_trigger(tc):
                current_state[code] = tc
                prev = self._state.get(state_key, {}).get(code)
                if prev != tc:
                    fired.append((code, name, tc, r))
            else:
                current_state[code] = ""

        # 更新并落盘 state（含清掉已不在列表的标的）
        self._state.setdefault(state_key, {})
        self._state[state_key].update(current_state)
        for code in list(self._state[state_key].keys()):
            if code not in current_state:
                del self._state[state_key][code]
        self._save_state()

        if fired:
            logger.info("[monitor] %s 本次触发 %d 条信号", state_key, len(fired))
            for code, name, tc, r in fired:
                self._push(market, code, name, tc, r, now, direction=direction)

    def _push(self, market, code, name, tc, r, now, direction=None):
        """把触发事件分发到所有启用的通知渠道（L4）；注册表不可用时回退纯日志。

        market 传原始市场（'stock'/'futures'），direction 单独传（期货多空），
        供 Notifier._format 渲染「期货·多单 信号」等结构化标题。
        """
        event = {
            "market": market,
            "direction": direction,
            "code": code,
            "name": name,
            "triggered": tc,
            "price": r.get("price"),
            "score": r.get("factorScore"),
            "pnl": r.get("pnl"),
            "time": now,
        }
        if self._notifiers is not None:
            self._notifiers.dispatch(event)
        else:
            logger.info(
                "[monitor-push][%s][%s] %s %s 触发：%s | 现价=%s 分=%s 盈亏=%s",
                market, now, code, name, tc,
                event["price"], event["score"], event["pnl"],
            )

    def reload_notifiers(self):
        """保存配置后热重载通知注册表（无需重启 app）。"""
        if self._notifiers is not None:
            try:
                self._notifiers.reload()
                logger.info("[monitor] 通知配置已热重载")
            except Exception as e:
                logger.warning("[monitor] 通知配置热重载失败: %s", e)

    def reload_config(self):
        """热重载 monitor.json（间隔/网格/分市场策略/开关），无需重启线程。

        监控线程在下一轮 _tick 自然读取 self.markets/self.interval 新值；
        正在进行的扫描不受影响。同时热重载通知注册表（渠道配置可能联动）。
        """
        try:
            self._load_config()
            if self._notifiers is not None:
                self._notifiers.reload()
            logger.info("[monitor] 配置已热重载 interval=%ss align=%s markets=%s",
                        self.interval, self.align_to_grid, list(self.markets.keys()))
        except Exception as e:
            logger.warning("[monitor] 配置热重载失败: %s", e)
