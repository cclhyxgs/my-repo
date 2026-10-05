# -*- coding: utf-8 -*-
"""报告分类（2026-09-13 由 `ui/report_classify.py::_classify_for_report` 上移，F-402）。

桌面端与 H5 服务端**共用本实现**（单一真相源），任何一侧改判定口径都会同时影响另一侧。
"""


def _classify_for_report(r):
    """将单只股票结果归入分类（与 report_builder.is_buy_action 判定一致）。

    关键一致性约束：
    - 「可建仓」只接受入场逻辑明确确认的动作（关注建仓 / 博反弹），
      避免分类器给的「博反弹」状态（仅 stock_score<-5，无额外验证）与
      入场逻辑否决后的「空仓观望」在参考条件上自相矛盾。
    - 博反弹状态但入场逻辑未确认（缺反转信号 / 动能衰竭）→ 归入「观察」等待确认。
    - 关注状态（用户已确认可建仓）即使入场逻辑给出「观望仓」仍保留在「可建仓」。
    """
    if r.get('error'):
        return 'error'
    if r.get('has_position') and r.get('entry_price', 0) > 0:
        return 'position'
    # 关注：用户确认可建仓（含观望仓细分）
    if r.get('status', '') == '关注':
        return 'buy'
    # 博反弹 / 恐慌反转：均为"已确认的反弹买入动作"，归入[建仓参考条件]；
    # 仅当入场逻辑未确认(空仓观望/观望仓)时才归入[观察]等待反转确认。
    # 注：此前只认 '博反弹'，漏掉平行的 '恐慌反转'，导致真正触发恐慌反转买入的标的
    # 被错分进[观察]且详情行误注"待反转确认"（核心汇总与详情动作自相矛盾，见 2026-07-28）。
    if r.get('status', '') == '博反弹':
        return 'buy' if r.get('entry_action', '') in ('博反弹', '恐慌反转') else 'watch'
    if r.get('status', '') == '回避':
        return 'avoid'
    return 'watch'
