# -*- coding: utf-8 -*-
"""期货品种池定义 — 主力合约元数据（合约乘数 / 保证金率 / 最小变动价位 / 交易所）。

与 stock_pool 对位：stock_pool 返回 (sina_code, raw_code, board)，
futures_pool 返回 (symbol, name, exchange, secid_prefix, multiplier, margin_rate, tick_size)。

数据来源：
  - 合约参数（乘数/保证金/ tick）基于各交易所 2024 年公开规则，近似值仅供回测，
    实盘以期货公司公告为准。
  - Eastmoney secid 前缀：113=SHFE 114=DCE 115=CZCE 8=CFFEX 42=INE

主力合约代码规则：
  - SHFE/DCE/INE: symbol + '0' (如 rb0, cu0, sc0)
  - CZCE: symbol.lower() + '0' (如 TA0, MA0 → ta0, ma0)
  - CFFEX: symbol + '0' (如 IF0, IH0)
"""

import logging
import os

from engine.config import WATCHLIST_FILE

logger = logging.getLogger(__name__)

# ── 交易所枚举 ──
SHFE = 'SHFE'   # 上海期货交易所
DCE  = 'DCE'    # 大连商品交易所
CZCE = 'CZCE'   # 郑州商品交易所
CFFEX = 'CFFEX' # 中国金融期货交易所
INE  = 'INE'    # 上海国际能源交易中心
GFEX = 'GFEX'   # 广州期货交易所

# ── Eastmoney secid 前缀 ──
SECID_PREFIX = {
    SHFE:  '113',
    DCE:   '114',
    CZCE:  '115',
    CFFEX: '8',
    INE:   '42',
    GFEX:  '142',
}

# ── 期货合约定义 ──
# (symbol, name, exchange, multiplier, margin_rate, tick_size, tick_value)
#   multiplier: 合约乘数（1手对应的数量单位）
#   margin_rate: 保证金比例（如 0.08 = 8%）
#   tick_size: 最小变动价位
#   tick_value: 每跳动一个 tick 的盈亏 = tick_size * multiplier
_FUTURES_DEFS = [
    # ── 有色金属 (SHFE) ──
    ('cu',  '沪铜',     SHFE,  5,    0.09, 10,    50),
    ('al',  '沪铝',     SHFE,  5,    0.08, 5,     25),
    ('zn',  '沪锌',     SHFE,  5,    0.08, 5,     25),
    ('pb',  '沪铅',     SHFE,  5,    0.09, 5,     25),
    ('ni',  '沪镍',     SHFE,  1,    0.12, 10,    10),
    ('sn',  '沪锡',     SHFE,  1,    0.10, 10,    10),
    ('ao',  '氧化铝',   SHFE,  20,   0.09, 1,     20),

    # ── 贵金属 (SHFE) ──
    ('au',  '黄金',     SHFE,  1000, 0.08, 0.02, 20),
    ('ag',  '白银',     SHFE,  15,   0.10, 1,    15),

    # ── 黑色系 (SHFE) ──
    ('rb',  '螺纹钢',   SHFE,  10,   0.08, 1,    10),
    ('hc',  '热轧卷板', SHFE,  10,   0.08, 1,    10),
    ('ss',  '不锈钢',   SHFE,  5,    0.10, 5,     25),
    ('wr',  '线材',     SHFE,  10,   0.08, 1,     10),

    # ── 能源/化工 (SHFE) ──
    ('fu',  '燃料油',   SHFE,  10,   0.10, 1,    10),
    ('bu',  '沥青',     SHFE,  10,   0.10, 2,    20),
    ('ru',  '橡胶',     SHFE,  10,   0.10, 5,    50),
    ('sp',  '纸浆',     SHFE,  10,   0.10, 2,    20),
    ('br',  '丁二烯橡胶', SHFE, 5,    0.10, 5,     25),

    # ── 原油及国际化品种 (INE) ──
    ('sc',  '原油',     INE,   1000, 0.12, 0.1,  100),
    ('lu',  '低硫燃油', INE,   10,   0.10, 1,    10),
    ('nr',  '20号胶',   INE,   10,   0.10, 5,    50),
    ('bc',  '国际铜',   INE,   5,    0.09, 10,   50),
    ('ec',  '集运指数', INE,   50,   0.12, 0.1,  5),

    # ── 农产品 (DCE) ──
    ('m',   '豆粕',     DCE,   10,   0.08, 1,    10),
    ('y',   '豆油',     DCE,   10,   0.08, 2,    20),
    ('p',   '棕榈油',   DCE,   10,   0.09, 2,    20),
    ('a',   '黄大豆1号', DCE,  10,   0.08, 1,    10),
    ('b',   '黄大豆2号', DCE,  10,   0.08, 1,    10),
    ('c',   '玉米',     DCE,   10,   0.08, 1,    10),
    ('cs',  '玉米淀粉', DCE,   10,   0.08, 1,    10),
    ('jd',  '鸡蛋',     DCE,   5,    0.09, 1,    5),
    ('rr',  '粳米',     DCE,   10,   0.08, 1,    10),
    ('lh',  '生猪',     DCE,   16,   0.12, 5,     80),

    # ── 煤焦钢 (DCE) ──
    ('j',   '焦炭',     DCE,   100,  0.10, 0.5,  50),
    ('jm',  '焦煤',     DCE,   60,   0.10, 0.5,  30),
    ('i',   '铁矿石',   DCE,   100,  0.10, 0.5,  50),

    # ── 化工 (DCE) ──
    ('l',   '聚乙烯',   DCE,   5,    0.09, 1,    5),
    ('v',   '聚氯乙烯', DCE,   5,    0.09, 1,    5),
    ('pp',  '聚丙烯',   DCE,   5,    0.09, 1,    5),
    ('eg',  '乙二醇',   DCE,   10,   0.09, 1,    10),
    ('eb',  '苯乙烯',   DCE,   5,    0.10, 1,    5),
    ('pg',  '液化石油气', DCE, 20,   0.10, 1,    20),

    # ── 林产品 (DCE) ──
    ('fb',  '纤维板',   DCE,   10,   0.10, 0.5,  5),
    ('bb',  '胶合板',   DCE,   500,  0.10, 0.05, 25),
    ('lg',  '原木',     DCE,   90,   0.10, 0.5,  45),

    # ── 化工 (CZCE) ──
    ('TA',  'PTA',      CZCE,  5,    0.08, 2,    10),
    ('MA',  '甲醇',     CZCE,  10,   0.09, 1,    10),
    ('SR',  '白糖',     CZCE,  10,   0.08, 1,    10),
    ('CF',  '棉花',     CZCE,  5,    0.08, 5,    25),
    ('SA',  '纯碱',     CZCE,  20,   0.10, 1,    20),
    ('FG',  '玻璃',     CZCE,  20,   0.10, 1,    20),
    ('OI',  '菜油',     CZCE,  10,   0.09, 1,    10),
    ('RM',  '菜粕',     CZCE,  10,   0.08, 1,    10),
    ('SF',  '硅铁',     CZCE,  5,    0.10, 2,    10),
    ('SM',  '锰硅',     CZCE,  5,    0.10, 2,    10),
    ('AP',  '苹果',     CZCE,  10,   0.10, 1,    10),
    ('UR',  '尿素',     CZCE,  20,   0.09, 1,    20),

    # ── 农产品/软商品 (CZCE) ──
    ('ZC',  '动力煤',   CZCE,  100,  0.50, 0.2,  20),
    ('WH',  '强麦',     CZCE,  20,   0.10, 1,    20),
    ('PM',  '普麦',     CZCE,  50,   0.10, 1,    50),
    ('RI',  '早籼稻',   CZCE,  20,   0.10, 1,    20),
    ('JR',  '粳稻',     CZCE,  20,   0.10, 1,    20),
    ('LR',  '晚籼稻',   CZCE,  20,   0.10, 1,    20),
    ('RS',  '菜籽',     CZCE,  10,   0.10, 1,    10),
    ('CY',  '棉纱',     CZCE,  5,    0.08, 5,    25),
    ('PK',  '花生',     CZCE,  5,    0.08, 2,    10),
    ('PX',  '对二甲苯', CZCE,  5,    0.09, 2,    10),
    ('SH',  '烧碱',     CZCE,  30,   0.09, 1,    30),
    ('PF',  '短纤',     CZCE,  5,    0.08, 2,    10),
    ('CJ',  '红枣',     CZCE,  5,    0.12, 5,    25),

    # ── 新能源 (GFEX 广期所) ──
    ('si',  '工业硅',   GFEX,  5,    0.09, 5,    25),
    ('lc',  '碳酸锂',   GFEX,  1,    0.12, 50,   50),
    ('PS',  '多晶硅',   GFEX,  3,    0.12, 5,    15),

    # ── 金融期货 (CFFEX) ──
    ('IF',  '沪深300',  CFFEX, 300,  0.12, 0.2,  60),
    ('IH',  '上证50',   CFFEX, 300,  0.12, 0.2,  60),
    ('IC',  '中证500',  CFFEX, 200,  0.14, 0.2,  40),
    ('IM',  '中证1000', CFFEX, 200,  0.14, 0.2,  40),
    ('T',   '10年期国债', CFFEX, 10000, 0.02, 0.005, 50),
    ('TF',  '5年期国债',  CFFEX, 10000, 0.012, 0.005, 50),
    ('TS',  '2年期国债',  CFFEX, 20000, 0.005, 0.002, 40),
    ('TL',  '30年期国债', CFFEX, 10000, 0.035, 0.01,  100),
]

# ── 行情条板块分类（2026-08-13 定稿：按常见分类，金融期货仅保留 IF）──
# 板块涨跌幅 = 板块内主力合约当日涨跌幅算术平均（新浪无期货板块指数接口，东财 push2 被 WAF 挡）
# 注意：CZCE 品种 symbol 为大写（TA/MA/SR/CF/SA/FG/OI/RM/SF/SM/AP/UR），匹配时做大小写归一
# 配套函数 all_sectors() / get_sector_contracts() 见文件底部（依赖 FuturesContract 类）
_FUTURES_SECTORS = {
    '股指期货':   ['IF', 'IH', 'IC', 'IM'],
    '利率债券':   ['T', 'TF', 'TS', 'TL'],
    '有色金属':   ['cu', 'al', 'zn', 'pb', 'ni', 'sn', 'bc'],
    '贵金属':     ['au', 'ag'],
    '能源化工':   ['sc', 'fu', 'lu', 'bu', 'ru', 'sp', 'nr',
                   'l', 'v', 'pp', 'eg', 'eb', 'pg', 'TA', 'MA', 'SA', 'UR'],
    '农产品':     ['m', 'y', 'p', 'a', 'c', 'cs', 'jd', 'SR', 'CF', 'OI', 'RM', 'AP'],
    '黑色系':     ['rb', 'hc', 'ss', 'j', 'jm', 'i', 'sf', 'sm', 'FG'],
}


def _build_symbol_sector_map() -> dict:
    """品种symbol(小写) → 中文板块名 的倒查表（用于期货扫描结果分组）。"""
    _map = {}
    for sector, syms in _FUTURES_SECTORS.items():
        for s in syms:
            _map[str(s).lower()] = sector
    return _map


# 品种symbol → 中文板块（大写symbol也在内，统一按小写匹配）
_SYMBOL_SECTOR_MAP = _build_symbol_sector_map()


def symbol_sector(symbol: str) -> str:
    """按品种symbol返回中文板块名；未归类时返回空串（由调用方兜底为交易所中文名）。"""
    return _SYMBOL_SECTOR_MAP.get(str(symbol or '').lower(), '')


def _format_contract_code(symbol: str, exchange: str, contract_month: str) -> str:
    """生成具体合约代码（新浪 API 统一用 4 位年月码）。

    所有交易所格式一致：symbol + YYMM
      - rb  + 2510 → rb2510  (SHFE)
      - TA  + 2510 → TA2510  (CZCE, 交易所内部用3位TA510，但新浪API用4位TA2510)
      - IF  + 2510 → IF2510  (CFFEX)

    Args:
        symbol: 品种代码（如 'rb', 'TA'）
        exchange: 交易所常量（当前不影响格式，保留参数以备扩展）
        contract_month: 4 位年月字符串（如 '2510'）

    Returns:
        完整合约代码（如 'rb2510' 或 'TA2510'）
    """
    return f"{symbol}{contract_month}"


def make_specific_contracts(contracts: list, contract_month: str) -> list:
    """给已有合约列表附加具体交割月份，返回新的 FuturesContract 实例列表。

    Args:
        contracts: 原始合约列表（主力连续版）
        contract_month: 4 位年月（如 '2510'）

    Returns:
        list[FuturesContract]，每个合约的 main_code 指向具体月份合约
    """
    result = []
    for c in contracts:
        result.append(FuturesContract(
            c.symbol, c.name, c.exchange, c.multiplier, c.margin_rate,
            c.tick_size, c.tick_value, contract_month=contract_month,
        ))
    return result


class FuturesContract:
    """单个期货品种的合约元数据。

    contract_month=None → 主力连续（main_code = symbol+'0'）
    contract_month='2510' → 具体合约（main_code = 'rb2510' / 'TA2510'）
    注：新浪 API 统一用 4 位年月码，CZCE 交易所内部虽用 3 位(TA510)但 API 接受 4 位(TA2510)。
    """
    __slots__ = ('symbol', 'name', 'exchange', 'multiplier', 'margin_rate',
                 'tick_size', 'tick_value', 'secid_prefix', 'main_code',
                 'contract_month')

    def __init__(self, symbol, name, exchange, multiplier, margin_rate,
                 tick_size, tick_value, contract_month=None):
        self.symbol = symbol
        self.name = name
        self.exchange = exchange
        self.multiplier = multiplier
        self.margin_rate = margin_rate
        self.tick_size = tick_size
        self.tick_value = tick_value
        self.secid_prefix = SECID_PREFIX.get(exchange, '113')
        self.contract_month = contract_month
        # 主力连续: symbol+'0'；具体合约: _format_contract_code
        if contract_month:
            self.main_code = _format_contract_code(symbol, exchange, contract_month)
        else:
            self.main_code = f"{symbol}0"

    @property
    def secid(self):
        """Eastmoney secid（交易所前缀 + 合约代码）。"""
        return f"{self.secid_prefix}.{self.main_code}"

    @property
    def is_continuous(self):
        """是否为主力连续合约（非具体月份）。"""
        return self.contract_month is None

    def __repr__(self):
        tag = "主力" if self.is_continuous else f"合约{self.contract_month}"
        return (f"FuturesContract({self.symbol}/{self.name} {tag}, {self.exchange}, "
                f"乘数={self.multiplier}, 保证金={self.margin_rate:.0%}, "
                f"tick={self.tick_size})")

    def to_dict(self):
        return {
            'symbol': self.symbol,
            'name': self.name,
            'exchange': self.exchange,
            'multiplier': self.multiplier,
            'margin_rate': self.margin_rate,
            'tick_size': self.tick_size,
            'tick_value': self.tick_value,
            'secid': self.secid,
            'main_code': self.main_code,
            'contract_month': self.contract_month,
        }


# ── 全局品种注册表 ──
_REGISTRY: dict[str, FuturesContract] = {}


def _init_registry():
    for row in _FUTURES_DEFS:
        c = FuturesContract(*row)
        _REGISTRY[c.symbol.lower()] = c
        _REGISTRY[c.symbol.upper()] = c  # 大小写都注册，CZCE 用大写


_init_registry()


def get_contract(symbol: str) -> FuturesContract | None:
    """按品种代码获取合约元数据，大小写不敏感。"""
    return _REGISTRY.get(symbol)


def parse_contract_code(query: str) -> tuple[FuturesContract | None, str | None]:
    """解析用户输入的合约查询串。

    支持三种格式：
      - 'rb' / 'TA'       → 主力连续（contract_month=None）
      - 'jd2609' / 'TA2510' → 具体合约（contract_month='2609' / '2510'）
      - '鸡蛋2609' / '沪锌2609' / '螺纹钢' → 中文名（含简称/别名）兜底

    Args:
        query: 用户输入（如 'jd2609', 'rb', '螺纹钢', '沪锌2609'）

    Returns:
        (FuturesContract, contract_month) 或 (None, None)
    """
    import re
    q = (query or '').strip()
    if not q:
        return None, None
    # 具体合约：1~4 位字母 + 4 位数字（年月）
    m = re.fullmatch(r'([a-zA-Z]{1,4})(\d{4})', q)
    if m:
        c = get_contract(m.group(1))
        if c:
            return c, m.group(2)
        return None, None
    # 主力连续
    c = get_contract(q)
    if c:
        return c, None
    # 中文名兜底：'鸡蛋2609'（带月份）或 '螺纹钢' / '沪锌'（纯中文名→主力连续）
    if re.search(r'[\u4e00-\u9fff]', q):
        c, month = parse_chinese_month_name(q)
        if c:
            return c, month
        for c2 in all_contracts():
            if _match_chinese_name(q, c2.name):
                return c2, None
    return None, None


# ── 中文名简称别名（搜索/自选解析容错）──
_CN_ALIASES = {
    '热卷': '热轧卷板', '塑料': '聚乙烯', '豆一': '黄大豆1号',
    '沪金': '黄金', '沪银': '白银', '金': '黄金', '银': '白银',
}


def _match_chinese_name(cn_part: str, name: str) -> bool:
    """中文名匹配规则：别名 → 全等/前缀 → 仅差1字的后缀（'锌'→'沪锌'、'金'→'黄金'）。

    '钢'→'螺纹钢'(差2)、'石'→'铁矿石'(差2) 这类较长名不命中，避免误匹配。
    """
    if _CN_ALIASES.get(cn_part) == name or cn_part == name or name.startswith(cn_part):
        return True
    return len(name) == len(cn_part) + 1 and name.endswith(cn_part)


def parse_chinese_month_name(query: str) -> tuple[FuturesContract | None, str | None]:
    """解析「中文名 + 4位月份」格式（如 '鸡蛋2609' / '沪锌2609' / '热卷2609'）。

    仅用于中文名开头输入（字母开头的 'jd2609' 走 parse_contract_code）。

    Returns:
        (FuturesContract, contract_month) 或 (None, None)
    """
    import re
    q = (query or '').strip()
    if not q:
        return None, None
    m = re.fullmatch(r'(.+?)(\d{4})', q)
    if not m:
        return None, None
    cn_part = m.group(1)
    month = m.group(2)
    for c in all_contracts():
        if _match_chinese_name(cn_part, c.name):
            return c, month
    return None, None


def all_contracts() -> list[FuturesContract]:
    """返回全部已注册合约（去重，按定义顺序）。"""
    seen = set()
    result = []
    for row in _FUTURES_DEFS:
        sym = row[0]
        key = sym.lower()
        if key not in seen:
            seen.add(key)
            result.append(_REGISTRY[key])
    return result


def get_pool(category: str = 'all') -> list[FuturesContract]:
    """按类别获取品种池。

    Args:
        category: 'all'=全部, 'metal'=有色金属, 'precious'=贵金属,
                  'black'=黑色, 'energy'=能源化工, 'agri'=农产品,
                  'chem'=化工(DCE+CZCE), 'financial'=金融期货
    Returns:
        list[FuturesContract]
    """
    EXCHANGE_MAP = {
        'metal':     {SHFE},
        'precious':  {SHFE},
        'black':     {SHFE, DCE},
        'energy':    {SHFE, INE},
        'agri':      {DCE, CZCE},
        'chem':      {DCE, CZCE, SHFE, INE},
        'financial': {CFFEX},
    }
    # 黑色系品种集合（用于 black 过滤）
    BLACK_SYMS = {'rb', 'hc', 'ss', 'j', 'jm', 'i'}
    PRECIOUS_SYMS = {'au', 'ag'}
    METAL_SYMS = {'cu', 'al', 'zn', 'pb', 'ni', 'sn'}
    AGRI_SYMS = {'m', 'y', 'p', 'a', 'c', 'cs', 'jd', 'SR', 'CF', 'OI', 'RM', 'AP'}
    ENERGY_SYMS = {'fu', 'bu', 'ru', 'sp', 'sc', 'lu', 'nr', 'bc'}

    if category == 'all':
        return all_contracts()

    syms = {
        'metal': METAL_SYMS,
        'precious': PRECIOUS_SYMS,
        'black': BLACK_SYMS,
        'energy': ENERGY_SYMS,
        'agri': AGRI_SYMS,
    }
    if category in syms:
        return [c for c in all_contracts() if c.symbol in syms[category]]
    if category == 'chem':
        # 化工 = DCE 化工 + CZCE 化工 + SHFE 化工
        CHEM_SYMS = {'l', 'v', 'pp', 'eg', 'eb', 'pg', 'TA', 'MA', 'SA', 'FG', 'UR'}
        return [c for c in all_contracts() if c.symbol in CHEM_SYMS]
    if category == 'financial':
        return [c for c in all_contracts() if c.exchange == CFFEX]
    return all_contracts()


def resolve_pool(spec: str) -> list[FuturesContract]:
    """把品种池参数解析为合约列表。

    Args:
        spec: 'all' / 'black' / 'metal' / 'agri' / 'chem' / 'energy' / 'financial'
              或逗号分隔的品种代码（如 'rb,cu,i,TA'）
    Returns:
        list[FuturesContract]
    """
    if spec == 'watchlist':
        return get_watchlist_futures_pool()
    if ',' in spec:
        result = []
        for s in spec.split(','):
            c = get_contract(s.strip())
            if c:
                result.append(c)
            else:
                logger.warning(f"未知期货品种: {s}")
        return result
    return get_pool(spec)


def get_watchlist_futures_pool(path=None) -> list['FuturesContract']:
    """解析期货自选池（watchlist_futures.txt），返回主力连续合约列表。

    每行一个品种，支持 'rb' / 'rb,螺纹钢' / '螺纹钢' / '螺纹钢 rb2610' / 'jd2609' 等
    （取首个逗号前 token；混合格式「中文名 合约码」先试最后一个 token 再试整串；
    中文名取其主力连续）。空文件/不存在返回 []。
    与 web_api.WATCHLIST_FILE_FUTURES 共用同一文件（config/watchlist_futures.txt）。
    """
    if path is None:
        path = os.path.join(os.path.dirname(WATCHLIST_FILE), 'watchlist_futures.txt')
    if not os.path.exists(path):
        return []
    result = []
    seen = set()
    with open(path, 'r', encoding='utf-8-sig') as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith('#'):
                continue
            tok = s.split(',')[0].strip()
            if not tok:
                continue
            # 混合格式（'螺纹钢 rb2610' / 'PTA ta2510'）：先试最后一个 token（合约码），
            # 失败再试整串（纯中文名/纯代码）。与 web_api._parse_watchlist 解析一致，
            # 避免「中文名+空格+代码」导致解析失败 → 池为空 → 回测报「期货池为空」。
            c = None
            _toks = [t for t in tok.split() if t]
            _candidates = [tok] + ([_toks[-1]] if len(_toks) > 1 else [])
            for _q in _candidates:
                c, _ = parse_contract_code(_q)
                if c:
                    break
            if c and c.symbol.lower() not in seen:
                seen.add(c.symbol.lower())
                result.append(c)
    return result


# ── 行情条板块配套函数（依赖 FuturesContract，定义在类之后）──

def all_sectors() -> list[str]:
    """返回行情条板块名列表（保持定义顺序）。"""
    return list(_FUTURES_SECTORS.keys())


def get_sector_contracts(sector: str) -> list[FuturesContract]:
    """返回板块内的主力连续合约列表（按品种池定义顺序，大小写归一匹配）。"""
    syms = {x.lower() for x in _FUTURES_SECTORS.get(sector, [])}
    return [c for c in all_contracts() if c.symbol.lower() in syms]
