# -*- coding: utf-8 -*-
"""L4 通知层包入口。

与 engine/data_sources 同构：具体渠道（应用内气泡 / 企业微信机器人 / Server酱）
实现 Notifier 抽象；NotifierRegistry 读 config/notifiers.json 构建启用渠道，
凭据按 credential_key 注入 config/credentials.json。Monitor 拿到 registry 后
dispatch(event) 即可把信号推到所有启用的渠道。
"""
from engine.notifiers.base import Notifier
from engine.notifiers.registry import NotifierRegistry

__all__ = ["Notifier", "NotifierRegistry"]
