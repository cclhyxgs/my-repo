# -*- coding: utf-8 -*-
"""通知渠道注册表：读 config/notifiers.json 构建启用渠道，凭据按 credential_key 注入。

与 engine/data_sources/registry.DataSourceRegistry 同构：
- channels：渠道定义（impl / display_name / enabled / credential_key）
- 缺省兜底 DEFAULT_CHANNELS（应用内气泡默认开，其余默认关，保证与原日志推送行为一致）
- dispatch(event)：遍历启用渠道 send，单渠道异常仅告警不阻断
- describe()：供 UI 读取（不暴露凭据明文，只给 has_credential）
- reload()：保存后热更新，无需重启 app
"""
import json
import logging
import os

from engine.config import CONFIG_DIR
from engine.notifiers.base import Notifier
from engine.notifiers import channels as _channels

logger = logging.getLogger(__name__)

DEFAULT_CHANNELS = {
    "inapp": {"impl": "InAppNotifier", "display_name": "应用内气泡", "enabled": True},
    "wechat": {"impl": "WeChatBotNotifier", "display_name": "企业微信机器人",
               "enabled": False, "credential_key": "wechat_webhook_url"},
    "dingtalk": {"impl": "DingTalkNotifier", "display_name": "钉钉机器人",
                 "enabled": False, "credential_key": "dingtalk_webhook_url"},
    "feishu": {"impl": "FeishuNotifier", "display_name": "飞书机器人",
               "enabled": False, "credential_key": "feishu_webhook_url"},
}

_BUILTIN = {
    "inapp": _channels.InAppNotifier,
    "wechat": _channels.WeChatBotNotifier,
    "dingtalk": _channels.DingTalkNotifier,
    "feishu": _channels.FeishuNotifier,
}
_IMPL_TO_KEY = {
    "InAppNotifier": "inapp",
    "WeChatBotNotifier": "wechat",
    "DingTalkNotifier": "dingtalk",
    "FeishuNotifier": "feishu",
}


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        # 首次运行未配置通知是常态，不必刷 WARNING（不生成文件，沿用内置默认）
        logger.debug("通知配置 %s 不存在，使用内置默认", path)
        return None
    except Exception as e:
        logger.warning("通知配置读取失败 %s: %s，使用内置默认", path, e)
        return None


class NotifierRegistry:
    def __init__(self, channels, api=None, config_dir=None):
        # channels: dict[key] -> Notifier instance（已注入凭据/context）
        self._channels = channels
        self._api = api
        self._config_dir = config_dir

    @classmethod
    def load(cls, config_dir=None, api=None):
        config_dir = config_dir or CONFIG_DIR
        cfg_path = os.path.join(config_dir, "notifiers.json")
        cred_path = os.path.join(config_dir, "credentials.json")
        cfg = _load_json(cfg_path) or {}
        defs = cfg.get("channels") or dict(DEFAULT_CHANNELS)
        creds = _load_json(cred_path) or {}

        instances = {}
        for key, d in defs.items():
            inst = cls._build(key, d, creds, api)
            if inst is not None:
                instances[key] = inst

        # 兜底：保证默认渠道都存在（config 缺定义时）
        for key, impl in _BUILTIN.items():
            if key not in instances:
                try:
                    inst = impl()
                    if key == "inapp" and api is not None:
                        inst.set_context(api)
                    instances[key] = inst
                except Exception as e:
                    logger.warning("内置通知渠道 %s 实例化失败: %s", key, e)

        registry = cls(instances, api, config_dir)
        logger.info("已加载通知配置：%s",
                    {k: (v.name, "开" if v.enabled else "关") for k, v in instances.items()})
        return registry

    @classmethod
    def _build(cls, key, d, creds, api):
        impl_name = d.get("impl")
        pkey = _IMPL_TO_KEY.get(impl_name, impl_name)
        impl_cls = _BUILTIN.get(pkey)
        if impl_cls is None:
            logger.warning("未知通知渠道实现 %r（%s），跳过", impl_name, key)
            return None
        try:
            inst = impl_cls()
            inst.name = d.get("display_name", inst.name)
            inst.enabled = bool(d.get("enabled", False))
            if key == "inapp" and api is not None:
                inst.set_context(api)
            cred_key = d.get("credential_key")
            if cred_key:
                inst.set_credential(creds.get(cred_key))
        except Exception as e:
            logger.warning("通知渠道 %s 实例化失败: %s", key, e)
            return None
        return inst

    def dispatch(self, event):
        for key, inst in self._channels.items():
            if not getattr(inst, "enabled", False):
                continue
            try:
                inst.send(event)
            except Exception as e:
                logger.warning("[notify] 渠道 %s 推送异常: %s", key, e)

    def test(self, key, event):
        """测试单个渠道（供 UI 验证 webhook 可用）。成功返回 True，失败抛异常。

        仅「启用 + 凭据齐全」时才真正推送：与正常 dispatch 路径一致，避免「渠道关闭后测试仍推送」
        误导用户（用户反馈：关闭后点测试不应发出数据）。
        """
        inst = self._channels.get(key)
        if inst is None:
            raise RuntimeError("未知渠道 %s" % key)
        if not getattr(inst, "enabled", False):
            raise RuntimeError("%s 已关闭：请先启用该渠道（开关），再点测试" % inst.name)
        if inst.needs_credential and not inst.has_credential():
            raise RuntimeError("%s 未配置凭据" % inst.name)
        inst.send(event)
        return True

    def describe(self):
        out = []
        for key, inst in self._channels.items():
            out.append({
                "key": key,
                "display_name": inst.name,
                "enabled": bool(getattr(inst, "enabled", False)),
                "needs_credential": bool(getattr(inst, "needs_credential", False)),
                "has_credential": bool(inst.has_credential()),
            })
        return out

    def reload(self):
        """重新加载配置（保存后热更新，无需重启）。"""
        fresh = NotifierRegistry.load(self._config_dir, self._api)
        self._channels = fresh._channels
        self._api = fresh._api
        self._config_dir = fresh._config_dir
        logger.info("[notify] 配置已热重载")
