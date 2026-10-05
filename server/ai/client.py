# -*- coding: utf-8 -*-
"""DeepSeek 对话客户端。

通过 OpenAI 兼容 REST 端点 `POST {base_url}/chat/completions` 调用，
`requests` 直连，不引入额外 SDK（requirement 已锁 requests==2.34.2）。
"""

import logging
import os
import time

import requests

from server.ai import config as ai_config
from server.core import logging as srv_logging
from server.core.errors import ApiError

logger = srv_logging.get_logger(__name__)

BASE_URL = "https://api.deepseek.com/chat/completions"
TIMEOUT = 40  # 秒

_VISIBLE_MODELS = ai_config.VALID_MODELS


# ── AI 调用明细日志（成功也要记）──────────────────────────────────────
# 存在的缺口：原本只在「出错」时打日志，成功路径一行都没有 ⇒ 成功次数/耗时/场景
# 在日志里完全不可见，「AI 有时可用有时不可用」无法定位是 AI 链路还是本机网络
# （2026-09-27：其实是 20 分钟的出站网络故障，排查却花了三刻钟）。
#
# ⛔ root logger 在 main_webview.py 被设成 WARNING+（basicConfig(level=WARNING)），
#    且 console=False 打包下 print 会被丢弃 ⇒ 用 INFO 写 logger 等于什么都没记。
# ✅ 解法照抄 ui/web_api.py 的 _init_diag_logger()：独立 logger + propagate=False，
#    自含 FileHandler 落 %LOCALAPPDATA%/M-Bull/logs/M-Bull_ai.log（INFO，成败都记）。
# ⛔ 刻意只用标准库算路径，不 import 项目的 get_app_dir ——
#    server 层不得反向依赖 ui/engine（T8 分层，prompts.py 就是前车之鉴）。
_AI_LOG = None


def _ai_log_candidates():
    import tempfile
    base = os.environ.get('QUANT_SYSTEM_DIR')
    if not base:
        la = os.environ.get('LOCALAPPDATA')
        base = os.path.join(la, 'M-Bull') if la else os.path.join(tempfile.gettempdir(), 'M-Bull')
    return [os.path.join(base, 'logs', 'M-Bull_ai.log'),
            os.path.join(tempfile.gettempdir(), 'M-Bull_ai.log')]


def _ai_logger():
    global _AI_LOG
    if _AI_LOG is not None:
        return _AI_LOG
    lg = logging.getLogger('M-Bull.ai')
    lg.setLevel(logging.INFO)
    lg.propagate = False  # ⛔ 不向上冒泡到 root，否则会被 WARNING 级别过滤掉
    if not lg.handlers:
        for _p in _ai_log_candidates():
            try:
                os.makedirs(os.path.dirname(_p), exist_ok=True)
                fh = logging.FileHandler(_p, encoding='utf-8', delay=True)
                fh.setLevel(logging.INFO)
                fh.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
                lg.addHandler(fh)
                break
            except Exception:
                continue
    _AI_LOG = lg
    return lg


def available() -> bool:
    """是否已配置 key。"""
    return ai_config.get_ai_config()["enabled"]


def chat(prompt: str, system: str = "", model: str = None, temperature: float = None,
         scene: str = "") -> str:
    """调用一次 DeepSeek，返回模型正文。错误统一映射为 ApiError。

    temperature: 不传则沿用上游默认值；结构化输出（如方案配置 JSON）建议传 0.0~0.2
    以压低发散，保证白名单契约稳定。

    scene: 场景名，仅用于日志区分（7 个入口各不相同：ai_analyze / ai_diagnosis /
           ai_diagnosis_batch / ai_scan / ai_scan_dsl / ai_ledger / scheme_*）。
           ⛔ 必须传 —— 不传的话四种报错长得一字不差，事后根本分不清是哪个入口挂了。
    """
    cfg = ai_config.get_ai_config()
    _scene = scene or "未标注场景"
    _size = len(str(prompt or "")) + len(str(system or ""))
    # 失败明细两处都写：logger.error 落到 M-Bull_error.log（保持既有习惯），
    # _ai_logger() 落到 M-Bull_ai.log（与成功记录同一处，便于算成功率）
    def _fail(reason):
        _ai_logger().info("scene=%-20s FAIL %s elapsed=%.2fs prompt=%d字",
                          _scene, reason, time.time() - _t0, _size)

    if not cfg["enabled"]:
        logger.error("AI 未配置 Key，拒绝调用 scene=%s", _scene)
        _t0 = time.time()
        _fail("未配置Key")
        raise ApiError(
            "AI_NOT_CONFIGURED",
            "尚未配置 DeepSeek API Key，请到「模型配置」页填写后再试",
            status_code=400,
        )
    model = model or cfg["model"] or ai_config.DEFAULT_MODEL

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {"model": model, "messages": messages, "stream": False}
    if temperature is not None:
        payload["temperature"] = float(temperature)

    _t0 = time.time()
    try:
        resp = requests.post(
            BASE_URL,
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            json=payload,
            timeout=TIMEOUT,
        )
    except requests.Timeout:
        # ⛔ 原代码这一分支不打任何日志 ⇒ 走这里时现场完全不可见，
        #    只能看到 ApiError 冒泡后的结果（2026-09-27 排查成本的主要来源）
        logger.error("AI 调用超时 scene=%s model=%s timeout=%ss elapsed=%.2fs",
                     _scene, model, TIMEOUT, time.time() - _t0)
        _fail("超时")
        raise ApiError("AI_TIMEOUT", "AI 调用超时，请稍后重试", status_code=504)
    except requests.RequestException as exc:  # 网络/连接类错误
        logger.error("AI 请求失败 scene=%s elapsed=%.2fs 错误：%s",
                     _scene, time.time() - _t0, exc)
        _fail("网络异常:" + type(exc).__name__)
        raise ApiError("AI_UPSTREAM_ERROR", f"AI 服务连接失败：{type(exc).__name__}", status_code=502)

    _el = time.time() - _t0
    if resp.status_code == 401:
        logger.error("AI Key 无效 scene=%s elapsed=%.2fs", _scene, _el)
        _fail("Key无效")
        raise ApiError("AI_AUTH_FAILED", "DeepSeek API Key 无效或已失效，请重新配置", status_code=401)
    if resp.status_code == 429:
        logger.error("AI 被限流 scene=%s elapsed=%.2fs", _scene, _el)
        _fail("限流429")
        raise ApiError("AI_RATE_LIMIT", "DeepSeek 调用被限流（账户余额/并发），请稍后重试", status_code=429)
    if resp.status_code >= 400:
        logger.error("AI 非 2xx 响应 scene=%s elapsed=%.2fs HTTP %s：%s",
                     _scene, _el, resp.status_code, resp.text[:500])
        _fail("HTTP" + str(resp.status_code))
        raise ApiError("AI_UPSTREAM_ERROR", f"AI 上游返回错误（HTTP {resp.status_code}）", status_code=502)

    try:
        body = resp.json()
        msg = body["choices"][0]["message"]
        content = msg["content"]
        thought = len(str(msg.get("reasoning_content") or ""))
    except (ValueError, KeyError, IndexError, TypeError):
        logger.error("AI 响应解析失败 scene=%s elapsed=%.2fs：%s", _scene, _el, resp.text[:500])
        _fail("响应解析失败")
        raise ApiError("AI_UPSTREAM_ERROR", "AI 响应无法解析", status_code=502)

    # ✅ 成功也要留痕：以前成功路径一行日志都没有，导致「有时可用」既无法证实也无法证伪
    text = (content or "").strip()
    _ai_logger().info("scene=%-20s OK   elapsed=%.2fs model=%s prompt=%d字 回复=%d字 思考链=%d字",
                      _scene, _el, model, _size, len(text), thought)
    return text