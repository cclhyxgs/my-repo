# -*- coding: utf-8 -*-
"""内置通知渠道实现。

- InAppNotifier  ：经 pywebview 窗口注入 JS 气泡（复用前端 mbullNotify），无窗口时回退日志。
- WeChatBotNotifier：企业微信群机器人 webhook（POST text）。
- DingTalkNotifier：钉钉群机器人 webhook（POST text；支持完整 URL 或纯 access_token，
  机器人安全设置请用「自定义关键词」模式，暂不支持加签 secret）。
- FeishuNotifier：飞书群机器人 webhook（POST text）。
"""
import json
import logging

from engine.notifiers.base import Notifier

logger = logging.getLogger("M-Bull.notifier")


class InAppNotifier(Notifier):
    name = "应用内气泡"
    needs_credential = False

    def send(self, event):
        msg = self._format(event)
        api = self._api
        win = api._window if (api and getattr(api, "_window", None) is not None) else None
        if win is not None:
            try:
                js = (
                    "if(window.mbullNotify){mbullNotify(%s);}"
                    "else if(window.showToast){showToast(%s);}"
                ) % (json.dumps(msg, ensure_ascii=False), json.dumps(msg, ensure_ascii=False))
                win.evaluate_js(js)
                return
            except Exception as e:
                logger.warning("[notify][inapp] 弹窗失败，回退日志: %s", e)
        logger.info("[notify][inapp] %s", msg)


class WeChatBotNotifier(Notifier):
    name = "企业微信机器人"
    needs_credential = True

    def send(self, event):
        url = (self.credential or "").strip()
        if not url:
            logger.warning("[notify][wechat] 未配置 webhook，跳过")
            return
        content = self._format(event)
        try:
            import requests
            resp = requests.post(url, json={"msgtype": "text", "text": {"content": content}},
                                 timeout=10)
            if resp.status_code == 200:
                logger.info("[notify][wechat] 推送成功")
            else:
                logger.warning("[notify][wechat] 推送失败 HTTP %s: %s",
                               resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("[notify][wechat] 推送异常: %s", e)


class DingTalkNotifier(Notifier):
    name = "钉钉机器人"
    needs_credential = True

    def send(self, event):
        cred = (self.credential or "").strip()
        if not cred:
            logger.warning("[notify][dingtalk] 未配置 webhook，跳过")
            return
        # 支持粘贴完整 webhook URL（https://oapi.dingtalk.com/robot/send?access_token=xxx）
        # 或纯 access_token（自动补全 URL）。机器人安全设置请选「自定义关键词」，
        # 加签（secret）模式暂不支持（需 timestamp+sign 动态签名）。
        if cred.startswith("http://") or cred.startswith("https://"):
            url = cred
        else:
            url = "https://oapi.dingtalk.com/robot/send?access_token=%s" % cred
        content = self._format(event)
        try:
            import requests
            resp = requests.post(url, json={"msgtype": "text", "text": {"content": content}},
                                 timeout=10)
            ok = False
            try:
                ok = (resp.json() or {}).get("errcode") == 0
            except Exception:
                ok = resp.status_code == 200
            if ok:
                logger.info("[notify][dingtalk] 推送成功")
            else:
                logger.warning("[notify][dingtalk] 推送失败 HTTP %s: %s",
                               resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("[notify][dingtalk] 推送异常: %s", e)


class FeishuNotifier(Notifier):
    name = "飞书机器人"
    needs_credential = True

    def send(self, event):
        url = (self.credential or "").strip()
        if not url:
            logger.warning("[notify][feishu] 未配置 webhook，跳过")
            return
        content = self._format(event)
        try:
            import requests
            resp = requests.post(url, json={"msg_type": "text", "content": {"text": content}},
                                 timeout=10)
            ok = False
            try:
                ok = (resp.json() or {}).get("code") == 0
            except Exception:
                ok = resp.status_code == 200
            if ok:
                logger.info("[notify][feishu] 推送成功")
            else:
                logger.warning("[notify][feishu] 推送失败 HTTP %s: %s",
                               resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("[notify][feishu] 推送异常: %s", e)
