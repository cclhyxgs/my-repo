# -*- coding: utf-8 -*-
"""L4 通知渠道抽象基类。

与 engine/data_sources/base.DataSource 同构：
- 具体渠道（应用内气泡 / 企业微信机器人 / Server酱）继承 Notifier 实现 send(event)。
- 凭据经 set_credential 注入；需要 app 窗口的渠道（应用内气泡）经 set_context(api) 注入。
- 所有异常在 dispatch 层被吞掉并告警，单渠道失败不阻断其它渠道。
"""
import logging

logger = logging.getLogger("M-Bull.notifier")


class Notifier:
    #: 渠道展示名（UI 显示）
    name = "未命名通知渠道"
    #: 是否需要凭据（webhook / SendKey 等）；UI 据此显示输入框
    needs_credential = False

    def __init__(self):
        self.enabled = True
        self.credential = None
        self._api = None

    def set_credential(self, cred):
        self.credential = cred

    def set_context(self, api):
        """注入 WebAPI 实例（应用内气泡需要 api._window 弹窗）。"""
        self._api = api

    def has_credential(self):
        return bool(self.credential)

    def send(self, event):
        """推送一条信号事件。

        event 字典字段：
            market   市场（'stock' / 'futures'）
            code     标的代码
            name     标的名称
            triggered 已触发的条件文案
            price    现价
            score    综合分
            pnl      盈亏
            time     触发时间字符串
        """
        raise NotImplementedError

    def _format(self, event):
        """把信号事件格式化为多行结构化文本（阶段1：全渠道统一）。

        输出示例：
            【期货·空单 信号】鸡蛋2609 (jd2609)
            触发：跌破建仓参考线 -> 空单加仓1档
            现价 3502.0   综合分 72   盈亏 +0.8%
            时间：2026-08-15 21:00
        market 兼容 'stock'/'futures'/'futures:long' 等历史取值；
        direction 缺失（旧事件/测试事件）时不显示方向段。
        """
        mkt = str(event.get("market") or "")
        if mkt.startswith("futures"):
            mkt_label = "期货"
            d = event.get("direction")
            if d in ("long", "short"):
                mkt_label += "·" + ("多单" if d == "long" else "空单")
        elif mkt.startswith("stock"):
            mkt_label = "A股"
        else:
            mkt_label = mkt or "信号"
        name = str(event.get("name") or "")
        code = str(event.get("code") or "")
        title = "【%s 信号】%s" % (mkt_label, name or code)
        if code and name and code != name:
            title += " (%s)" % code
        lines = [title]
        tc = event.get("triggered")
        if tc:
            lines.append("触发：%s" % tc)
        segs = []
        if event.get("price") is not None:
            segs.append("现价 %s" % event["price"])
        if event.get("score") is not None:
            segs.append("综合分 %s" % event["score"])
        if event.get("pnl"):
            segs.append("盈亏 %s" % event["pnl"])
        if segs:
            lines.append("   ".join(segs))
        if event.get("time"):
            lines.append("时间：%s" % event["time"])
        # 合规尾缀（商用必须）：对外渠道（企业微信/钉钉/飞书）推送内容带固定免责行，
        # 明确"仅为用户自设参考条件的状态播报，非投资建议"。
        lines.append("—— 仅为您设置的参考条件被满足的状态播报，不构成投资建议")
        return "\n".join(lines)
