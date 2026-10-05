# -*- coding: utf-8 -*-
"""快速起步向导 · 共享步骤定义与完成态检测（UI 无关，纯读取）。

合规说明：
- 本模块**绝不写入任何配置参数**，只读取当前方案与回测报告文件来判断进度。
- 向导仅引导用户按 4 步顺序操作已有 UI 按钮，不预填、不替用户决策、
  不内置/推荐任何交易策略。每一步 instruction 不出现具体数值（避免暗示"推荐参数"）。
- 设计原则：**配置闭环 = 方案能跑诊断/监控所需的核心 4 块**（因子/方向/状态分界/仓位管理）。
  回测是独立验证工具（带未来函数/过拟合风险，不保证未来表现），**不属于配置闭环**——
  因此向导不含回测步骤；用户在「回测验证」面板可随时单独使用。
"""

import os
import glob

from engine import quant_config
from engine.config import get_app_dir


# 4 步定义（量化分析配置闭环：选工具→调方向→设判定标准→设仓位规则）。
# - instruction: 引导文案，不出现任何具体数值。
# - jump: 各 UI 渲染时用它定位"去操作"目标。
#     web_pane : PyWebView 模型配置子面板 paneId（q-manage/q-weight/q-threshold/q-position）
#     action   : open_settings（其它 action 类型已废弃：回测/IC 已从向导移除）
# - btn: "去操作"按钮上的动词
STEPS = [
    {
        'id': 'select_factors',
        'title': '① 选因子',
        'summary': '从因子库勾选你关注的因子',
        'instruction': '打开「模型配置 → 因子管理」，勾选至少 1 个因子（如均线多头排列、动量、RSI 等）。'
                       '勾选即启用，工具不会替你预设任何因子组合。因子是后续诊断/评分的"输入信号"——'
                       '选什么决定用什么来给标的打分。',
        'btn': '选因子',
        'jump': {'web_pane': 'q-manage', 'action': 'open_settings'},
    },
    {
        'id': 'set_direction',
        'title': '② 设方向（正向/反向）',
        'summary': '为每个因子设定正向 / 反向',
        'instruction': '切到「权重 & 方向」，为每个已启用因子点选方向（▲正向 / ▼反向）。方向决定该因子'
                       '数值越大代表越强还是越弱——请按你对因子的理解设定。方向错配会让评分方向相反，'
                       '务必逐个确认。',
        'btn': '设方向',
        'jump': {'web_pane': 'q-weight', 'action': 'open_settings'},
    },
    {
        'id': 'set_threshold',
        'title': '③ 状态分界',
        'summary': '设评分阈值决定标的状态分类',
        'instruction': '切到「状态分界」，为「强势 / 标准 / 试探 / 观望 / 反弹 / 恐慌反弹 / 顶部反转」这 7 个状态'
                       '分别设定评分阈值。诊断输出时一只票会按评分归到对应状态——强势对应建仓主区间，'
                       '观望对应暂不操作，恐慌反弹 / 顶部反转对应减仓或反转等。'
                       '阈值决定你"什么时候动"，方向决定"往哪个方向动"。',
        'btn': '设阈值',
        'jump': {'web_pane': 'q-threshold', 'action': 'open_settings'},
    },
    {
        'id': 'set_position',
        'title': '④ 仓位管理',
        'summary': '设各状态对应的建仓/加减仓比例',
        'instruction': '切到「仓位管理」，为「强势 / 标准 / 试探」等状态设定仓位比例（如强势建仓 X%、'
                       '标准试探仓、反弹反手仓等）。诊断时根据评分落到的状态给出建议仓位。'
                       '到「回测验证」可参考策略回测，但回测数据**不保证未来表现**（过拟合/未来函数风险），'
                       '仓位比例请按你的风险承受度自行设定。',
        'btn': '设仓位',
        'jump': {'web_pane': 'q-position', 'action': 'open_settings'},
    },
]


def _find_report(name):
    """在候选目录中查找回测报告文件，返回路径或 None。"""
    candidates = [get_app_dir(), os.getcwd()]
    qmf = getattr(quant_config, 'QUANT_MODEL_FILE', None)
    if qmf:
        candidates.append(os.path.dirname(qmf))
    seen = set()
    for d in candidates:
        if not d or d in seen or not os.path.isdir(d):
            continue
        seen.add(d)
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    # 兜底：两级递归（避免 onefile 下报告落在子目录）
    for d in candidates:
        if not d or not os.path.isdir(d):
            continue
        hits = glob.glob(os.path.join(d, '**', name), recursive=True)
        if hits:
            return hits[0]
    return None


def evaluate_state():
    """返回 {step_id: bool} 各步完成态。纯读取，不写盘。"""
    cfg = quant_config.load_config()
    active = (cfg or {}).get('active_factors') or []
    fcs = (cfg or {}).get('factor_configs') or {}
    thresholds = (cfg or {}).get('thresholds') or {}
    entry_params = (cfg or {}).get('entry_params') or {}

    done = {}

    # ① 选因子：有启用因子且对应配置项存在
    done['select_factors'] = bool(active) and all(f in fcs for f in active)

    # ② 设方向：每个启用因子 direction 为 +1/-1
    if active:
        done['set_direction'] = all(fcs.get(f, {}).get('direction') in (1, -1) for f in active)
    else:
        done['set_direction'] = False

    # ③ 状态分界：thresholds 含 7 个状态且各自有有效值
    required_status = ('strong', 'standard', 'test', 'pending', 'rebound', 'panic_rebound', 'top_reversal')
    done['set_threshold'] = all(
        thresholds.get(s) is not None and thresholds.get(s) != ''
        for s in required_status
    )

    # ④ 仓位管理：entry_params.positions 含 strong/standard/test/rebound/panic_rebound/top_reversal 且各自有有效值
    positions = (entry_params.get('positions') or {}) if isinstance(entry_params, dict) else {}
    required_pos = ('strong', 'standard', 'test', 'rebound', 'panic_rebound', 'top_reversal')
    done['set_position'] = all(
        positions.get(s) is not None and positions.get(s) != ''
        for s in required_pos
    )

    return done


def first_run():
    """是否首次启动（待配置态）。用于自动弹向导。"""
    return quant_config.load_config() is None


def overall_progress():
    """返回 (完成步数, 总步数)。"""
    done = evaluate_state()
    n = sum(1 for s in STEPS if done.get(s['id']))
    return n, len(STEPS)
