# -*- coding: utf-8 -*-
"""股票池解析（从 backtest.py 抽出，解除 backtest_strategy / factor_ic_backtest 对 backtest 整包的依赖）。

仅依赖 engine 内部纯逻辑（data_layer / state / config），不 import backtest / ui，
因此可被 GUI 进程、回测子进程、因子 IC 进程共用。
"""

import logging
import os

from engine.config import WATCHLIST_FILE
from engine.data_layer import DataAPI
from engine.state import state

logger = logging.getLogger(__name__)


def _classify_board_by_code(raw_code):
    """根据股票代码前缀判断所属板块（P5板块中性化用）。

    规则：
        688xxx         → 科创板
        300xxx/301xxx  → 创业板
        60xxxx         → 沪市主板
        00xxxx         → 深市主板
        其他           → 其他
    """
    if raw_code.startswith('688'):
        return '科创板'
    elif raw_code.startswith(('300', '301')):
        return '创业板'
    elif raw_code.startswith('60'):
        return '沪市主板'
    elif raw_code.startswith('00'):
        return '深市主板'
    return '其他'


def get_stock_pool():
    """核心指数股票池（上证50+创业50+科创50）

    P5修复：第三个字段从指数标签（'上证50'/'科创50'/'创业50'）改为真实板块
    （'沪市主板'/'科创板'/'创业板'），便于板块中性化处理。
    """
    stocks = []
    sh50_codes = ['600028', '600030', '600036', '600050', '600089', '600111', '600150', '600183',
                  '600276', '600309', '600406', '600519', '600760', '600809', '600887', '600900',
                  '600930', '601012', '601088', '601127', '601166', '601211', '601288', '601318',
                  '601328', '601398', '601600', '601601', '601628', '601658', '601668', '601688',
                  '601728', '601857', '601888', '601899', '601919', '601988', '603019', '603259',
                  '603501', '603986', '603993', '688008', '688012', '688041', '688111', '688256',
                  '688981']
    kc50_codes = ['688008', '688009', '688012', '688027', '688036', '688041', '688047', '688065',
                  '688072', '688082', '688099', '688111', '688120', '688122', '688126', '688169',
                  '688183', '688187', '688188', '688213', '688220', '688223', '688249', '688256',
                  '688271', '688297', '688303', '688347', '688361', '688375', '688396', '688469',
                  '688472', '688498', '688506', '688521', '688525', '688538', '688568', '688578',
                  '688599', '688608', '688617', '688702', '688728', '688777', '688795', '688802',
                  '688981', '689009']
    cy50_codes = ['300014', '300015', '300017', '300033', '300058', '300059', '300073', '300115',
                  '300124', '300136', '300207', '300223', '300251', '300255', '300274', '300308',
                  '300316', '300339', '300346', '300373', '300390', '300394', '300395', '300408',
                  '300418', '300433', '300442', '300450', '300458', '300474', '300475', '300476',
                  '300496', '300502', '300548', '300604', '300620', '300724', '300748', '300750',
                  '300751', '300757', '300760', '300763', '300782', '300803', '300857', '301236',
                  '301308']
    for code in sh50_codes:
        stocks.append(('sh' + code, code, _classify_board_by_code(code)))
    for code in kc50_codes:
        stocks.append(('sh' + code, code, _classify_board_by_code(code)))
    for code in cy50_codes:
        stocks.append(('sz' + code, code, _classify_board_by_code(code)))
    seen = set()
    unique_stocks = []
    for item in stocks:
        sina_code = item[0]
        if sina_code not in seen:
            seen.add(sina_code)
            unique_stocks.append(item)
    return unique_stocks


def get_full_market_pool():
    """加载全市场A股股票池"""
    if not state.code_to_name:
        count = DataAPI.load_stock_list()
        if count == 0:
            logger.warning("加载全市场股票列表失败，返回空股票池")
            return []

    stocks = []
    for stock_code, stock_name in state.code_to_name.items():
        raw_code = stock_code.replace('sh', '').replace('sz', '')
        if not raw_code.isdigit() or len(raw_code) != 6:
            continue
        if raw_code.startswith(('5', '1', '2', '4', '8')):
            continue
        if raw_code.startswith('688'):
            board = '科创板'
        elif raw_code.startswith(('300', '301')):
            board = '创业板'
        elif raw_code.startswith('60'):
            board = '沪市主板'
        elif raw_code.startswith('00'):
            board = '深市主板'
        else:
            board = '其他'
        stocks.append((stock_code, raw_code, board))
    return stocks


def get_watchlist_pool():
    """从 config/watchlist.txt 加载自选股池，返回 (sina_code, raw_code, board) 元组列表。

    与「自选股诊断」输入格式一致：每行 `名称(或6位代码), 持仓价, 持仓天数`，
    首个字段为名称时经 DataAPI.get_stock_code 解析为代码（需先加载股票列表）。
    """
    if not state.name_to_code:
        count = DataAPI.load_stock_list()
        if count == 0:
            logger.warning("加载股票列表失败，自选股池返回空")
            return []

    stocks = []
    seen = set()
    try:
        if os.path.exists(WATCHLIST_FILE):
            with open(WATCHLIST_FILE, 'r', encoding='utf-8-sig') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    query = line.split(',')[0].strip()
                    if not query:
                        continue
                    # 6 位代码直接解析；否则按名称查询
                    if query.isdigit() and len(query) == 6:
                        raw_code = query
                    else:
                        code = DataAPI.get_stock_code(query)
                        if not code:
                            print(f"  [自选股池] 跳过无法识别: {query}")
                            continue
                        raw_code = code.replace('sh', '').replace('sz', '')
                    if raw_code in seen:
                        continue
                    seen.add(raw_code)
                    # 跳过非 A 股（指数/基金/可转债等）
                    if raw_code.startswith(('5', '1', '2', '4', '8')):
                        continue
                    if raw_code.startswith('6'):
                        sina_code = 'sh' + raw_code
                    elif raw_code.startswith(('0', '3')):
                        sina_code = 'sz' + raw_code
                    else:
                        continue
                    if raw_code.startswith('688'):
                        board = '科创板'
                    elif raw_code.startswith(('300', '301')):
                        board = '创业板'
                    elif raw_code.startswith('60'):
                        board = '沪市主板'
                    elif raw_code.startswith('00'):
                        board = '深市主板'
                    else:
                        board = '其他'
                    stocks.append((sina_code, raw_code, board))
    except Exception as e:
        print(f"  [自选股池] 读取失败: {e}")
    return stocks


def resolve_stock_pool(pool='148'):
    """把股票池参数解析为 (sina_code, raw_code, board) 列表。

    pool ∈ {'148'(默认), 'full', 'watchlist'}。
    '148'       → 上证50+创业50+科创50
    'full'      → 全市场 A 股
    'watchlist' → config/watchlist.txt 自选股列表
    """
    if pool == 'full':
        return get_full_market_pool()
    if pool == 'watchlist':
        return get_watchlist_pool()
    return get_stock_pool()
