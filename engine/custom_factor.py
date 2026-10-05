#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自定义公式因子引擎（全中文公式语言）

用户用通达信风格的中文公式自己定义指标，命名后作为因子参与打分/扫描/回测。

设计要点：
- 自研词法/递归下降解析（不用 eval / exec / ast.parse），语言本身没有循环语句，
  故不存在死循环风险；只有白名单内的算子可被调用，天然无代码注入面。
- 公式对整段 K 线求值，返回 numpy 序列（O(n)），由 factor_registry 包装成
  calc_fn(ctx, params) -> float（取序列末值）。
- 输出可以是连续数值（参与 z-score 归一化）或布尔条件（0/1）。

公式示例：
    牛熊线 := 均线(收盘价, 30);
    强度   := (收盘价 - 前值(收盘价, 5)) / 前值(收盘价, 5) * 100;
    输出   := 上穿(收盘价, 牛熊线) 且 强度 > 3;
"""

import re

import numpy as np

# ============================================================
# 常量
# ============================================================

MAX_FORMULA_LEN = 4000        # 公式字符上限
MAX_STATEMENTS = 60           # 语句数上限
MAX_ARGS = 4                  # 单算子参数个数上限
MAX_NEST_DEPTH = 24           # 表达式嵌套深度上限

# 原始数据序列（中文名 -> ctx 键 / 英文兼容名）
DATA_SERIES = {
    '收盘价': 'closes',
    '最高价': 'highs',
    '最低价': 'lows',
    '开盘价': 'opens',
    '成交量': 'volumes',
}
DATA_SERIES_EN = {
    'CLOSE': 'closes',
    'HIGH': 'highs',
    'LOW': 'lows',
    'OPEN': 'opens',
    'VOL': 'volumes',
}

# 逻辑词（中文 / 英文兼容）
LOGIC_AND = {'且', 'AND'}
LOGIC_OR = {'或', 'OR'}
LOGIC_NOT = {'非', 'NOT'}

OUTPUT_NAMES = {'输出'}          # 显式输出变量名

# 全角 -> 半角
_FULLWIDTH_MAP = {
    '（': '(', '）': ')', '：': ':', '；': ';', '，': ',', '、': ',',
    '　': ' ', '．': '.', '。': '.', '？': '?', '！': '!', '％': '%',
    '＝': '=', '＋': '+', '－': '-', '＊': '*', '／': '/', '＜': '<', '＞': '>',
    '｜': '|', '＆': '&', '＾': '^', '～': '~', '＃': '#', '＠': '@',
    '【': '[', '】': ']', '｛': '{', '｝': '}', '“': '"', '”': '"', '‘': "'", '’': "'",
}


def normalize_fullwidth(text):
    """全角标点/数字/字母 -> 半角（中文输入法默认全角，必须自动转换）。"""
    if not text:
        return ''
    out = []
    for ch in text:
        if ch in _FULLWIDTH_MAP:
            out.append(_FULLWIDTH_MAP[ch])
            continue
        cp = ord(ch)
        # 全角数字 ０-９ / 字母 Ａ-Ｚ ａ-ｚ
        if 0xFF10 <= cp <= 0xFF19 or 0xFF21 <= cp <= 0xFF3A or 0xFF41 <= cp <= 0xFF5A:
            out.append(chr(cp - 0xFEE0))
            continue
        out.append(ch)
    return ''.join(out)


# ============================================================
# 错误类型
# ============================================================

class FormulaError(Exception):
    """公式编译/求值错误，带行列定位（供前端标红）。"""

    def __init__(self, message, line=1, col=1):
        super().__init__(message)
        self.message = message
        self.line = int(line)
        self.col = int(col)

    def to_dict(self):
        return {'line': self.line, 'col': self.col, 'msg': self.message}


# ============================================================
# 词法分析
# ============================================================

_IDENT_RE = re.compile(r'[A-Za-z_\u4e00-\u9fa5][A-Za-z0-9_\u4e00-\u9fa5]*')
_NUM_RE = re.compile(r'\d+(\.\d+)?')

_TWO_CHAR_OPS = ('>=', '<=', '==', '!=', '<>', ':=')
_ONE_CHAR_OPS = set('+-*/><=(),;')


class Token:
    __slots__ = ('kind', 'value', 'line', 'col')

    def __init__(self, kind, value, line, col):
        self.kind = kind
        self.value = value
        self.line = line
        self.col = col

    def __repr__(self):
        return f'Token({self.kind},{self.value!r}@{self.line}:{self.col})'


def _strip_comments(text):
    """去掉 # 之后到行尾的注释（# 不会出现在标识符/数字中）。"""
    lines = []
    for ln in text.split('\n'):
        idx = ln.find('#')
        if idx >= 0:
            ln = ln[:idx]
        lines.append(ln)
    return '\n'.join(lines)


def tokenize(text):
    """把公式文本切成 token 列表，末尾追加 EOF。"""
    src = normalize_fullwidth(text)
    src = _strip_comments(src)
    tokens = []
    i = 0
    line = 1
    col = 1
    n = len(src)
    while i < n:
        ch = src[i]
        if ch == '\n':
            line += 1
            col = 1
            i += 1
            continue
        if ch in ' \t\r':
            i += 1
            col += 1
            continue
        # 两字符运算符
        two = src[i:i + 2]
        if two in _TWO_CHAR_OPS:
            tokens.append(Token('OP', two, line, col))
            i += 2
            col += 2
            continue
        if ch in _ONE_CHAR_OPS:
            kind = 'OP' if ch not in '(),;' else {'(': 'LP', ')': 'RP', ',': 'COMMA', ';': 'SEMI'}[ch]
            tokens.append(Token(kind, ch, line, col))
            i += 1
            col += 1
            continue
        m = _NUM_RE.match(src, i)
        if m:
            tokens.append(Token('NUM', m.group(0), line, col))
            i = m.end()
            col += len(m.group(0))
            continue
        m = _IDENT_RE.match(src, i)
        if m:
            tokens.append(Token('IDENT', m.group(0), line, col))
            i = m.end()
            col += len(m.group(0))
            continue
        raise FormulaError(f'无法识别的字符 {ch!r}', line, col)
    tokens.append(Token('EOF', '', line, col))
    return tokens


# ============================================================
# AST 节点
# ============================================================

class Node:
    __slots__ = ()


class Num(Node):
    __slots__ = ('value',)

    def __init__(self, value):
        self.value = float(value)


class Ref(Node):
    __slots__ = ('name', 'line', 'col')

    def __init__(self, name, line, col):
        self.name = name
        self.line = line
        self.col = col


class Call(Node):
    __slots__ = ('func', 'args', 'line', 'col')

    def __init__(self, func, args, line, col):
        self.func = func
        self.args = args
        self.line = line
        self.col = col


class BinOp(Node):
    __slots__ = ('op', 'left', 'right')

    def __init__(self, op, left, right):
        self.op = op
        self.left = left
        self.right = right


class UnaryOp(Node):
    __slots__ = ('op', 'operand')

    def __init__(self, op, operand):
        self.op = op
        self.operand = operand


class BoolOp(Node):
    __slots__ = ('op', 'values')

    def __init__(self, op, values):
        self.op = op
        self.values = values


class Compare(Node):
    __slots__ = ('left', 'ops', 'comparators')

    def __init__(self, left, ops, comparators):
        self.left = left
        self.ops = ops
        self.comparators = comparators


class Assign(Node):
    __slots__ = ('name', 'expr', 'line', 'col')

    def __init__(self, name, expr, line, col):
        self.name = name
        self.expr = expr
        self.line = line
        self.col = col


# ============================================================
# 算子库（中文名 / 英文兼容名 -> 实现）
# ============================================================

def _rolling_window(a, n):
    """返回 shape=(len(a)-n+1, n) 的滑窗视图（n<=len 时）。"""
    if n <= 0 or len(a) < n:
        return None
    from numpy.lib.stride_tricks import sliding_window_view
    return sliding_window_view(a, n)


def _rolling(a, n, reducer):
    out = np.full(a.shape, np.nan, dtype=float)
    if n <= 0:
        return out
    if len(a) < n:
        return out
    win = _rolling_window(a, n)
    out[n - 1:] = reducer(win, axis=-1)
    return out


def _op_ma(a, n):
    return _rolling(np.asarray(a, dtype=float), int(n), np.mean)


def _first_finite(a):
    mask = np.isfinite(a)
    if not mask.any():
        return -1
    return int(np.argmax(mask))


def _op_ema(a, n):
    """通达信口径：从首个有效值起递推，不要求满 N 根。"""
    a = np.asarray(a, dtype=float)
    n = int(n)
    out = np.full(a.shape, np.nan, dtype=float)
    if len(a) == 0 or n <= 0:
        return out
    start = _first_finite(a)
    if start < 0:
        return out
    alpha = 2.0 / (n + 1.0)
    out[start] = a[start]
    for i in range(start + 1, len(a)):
        prev = out[i - 1]
        if not np.isfinite(prev):
            prev = a[i - 1]
            if not np.isfinite(prev):
                continue
        out[i] = alpha * a[i] + (1 - alpha) * prev
    return out


def _op_sma(a, n, m):
    """Wilder 平滑：y[i] = (M*x[i] + (N-M)*y[i-1]) / N（从首个有效值起递推）。"""
    a = np.asarray(a, dtype=float)
    n = float(n)
    m = float(m)
    out = np.full(a.shape, np.nan, dtype=float)
    if len(a) == 0 or n <= 0:
        return out
    start = _first_finite(a)
    if start < 0:
        return out
    out[start] = a[start]
    for i in range(start + 1, len(a)):
        prev = out[i - 1]
        if not np.isfinite(prev):
            prev = a[i - 1]
            if not np.isfinite(prev):
                continue
        out[i] = (m * a[i] + (n - m) * prev) / n
    return out


def _op_llv(a, n):
    return _rolling(np.asarray(a, dtype=float), int(n), np.min)


def _op_hhv(a, n):
    return _rolling(np.asarray(a, dtype=float), int(n), np.max)


def _op_ref(a, n):
    a = np.asarray(a, dtype=float)
    n = int(n)
    out = np.full(a.shape, np.nan, dtype=float)
    if n <= 0:
        return a.copy()
    if len(a) > n:
        out[n:] = a[:-n]
    return out


def _op_sum(a, n):
    return _rolling(np.asarray(a, dtype=float), int(n), np.sum)


def _op_std(a, n):
    return _rolling(np.asarray(a, dtype=float), int(n), np.std)


def _op_cross(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    a, b = np.broadcast_arrays(a, b)
    out = np.zeros(a.shape, dtype=bool)
    if len(a) < 2:
        return out
    prev_ok = np.isfinite(a[:-1]) & np.isfinite(b[:-1])
    cur_ok = np.isfinite(a[1:]) & np.isfinite(b[1:])
    out[1:] = (a[1:] > b[1:]) & (a[:-1] <= b[:-1]) & prev_ok & cur_ok
    return out


def _op_abs(a):
    return np.abs(np.asarray(a, dtype=float))


def _op_max(a, b):
    return np.maximum(np.asarray(a, dtype=float), np.asarray(b, dtype=float))


def _op_min(a, b):
    return np.minimum(np.asarray(a, dtype=float), np.asarray(b, dtype=float))


# 算子表：内部键 -> (实现, 参数个数(最小,最大), 中文显示名, 分类, 说明)
_OPERATOR_SPECS = [
    ('均线', _op_ma, (2, 2), '均线(MA)', '均线', 'N 日简单移动平均'),
    ('指数均线', _op_ema, (2, 2), '指数均线(EMA)', '均线', 'N 日指数移动平均'),
    ('平滑', _op_sma, (3, 3), '平滑(SMA)', '均线', 'Wilder 平滑：平滑(序列, N, M)'),
    ('最低N日', _op_llv, (2, 2), '最低N日(LLV)', '区间', '最近 N 日的最低值'),
    ('最高N日', _op_hhv, (2, 2), '最高N日(HHV)', '区间', '最近 N 日的最高值'),
    ('前值', _op_ref, (2, 2), '前值(REF)', '区间', 'N 日前的值'),
    ('求和', _op_sum, (2, 2), '求和(SUM)', '区间', '最近 N 日求和'),
    ('标准差', _op_std, (2, 2), '标准差(STD)', '区间', '最近 N 日标准差'),
    ('上穿', _op_cross, (2, 2), '上穿(CROSS)', '逻辑', 'A 从下方上穿 B'),
    ('绝对值', _op_abs, (1, 1), '绝对值(ABS)', '数学', '绝对值'),
    ('最大', _op_max, (2, 2), '最大(MAX)', '数学', '逐点取较大值'),
    ('最小', _op_min, (2, 2), '最小(MIN)', '数学', '逐点取较小值'),
]

# 英文兼容名
_OPERATOR_ALIASES = {
    'MA': '均线', 'EMA': '指数均线', 'SMA': '平滑',
    'LLV': '最低N日', 'HHV': '最高N日', 'REF': '前值',
    'SUM': '求和', 'STD': '标准差', 'CROSS': '上穿',
    'ABS': '绝对值', 'MAX': '最大', 'MIN': '最小',
}

OPERATORS = {}
OPERATOR_META = []
for _name, _fn, _arity, _display, _cat, _desc in _OPERATOR_SPECS:
    OPERATORS[_name] = {'fn': _fn, 'arity': _arity, 'display': _display}
    OPERATOR_META.append({
        'name': _name, 'display': _display, 'arity': list(_arity),
        'category': _cat, 'desc': _desc,
    })
for _alias, _cn in _OPERATOR_ALIASES.items():
    OPERATORS[_alias] = OPERATORS[_cn]
    OPERATOR_META.append({
        'name': _alias, 'display': OPERATORS[_cn]['display'], 'arity': list(OPERATORS[_cn]['arity']),
        'category': '英文兼容', 'desc': OPERATORS[_cn]['display'],
    })

# 保留字：不可作为变量名（「输出」除外——它是显式输出变量的合法名字）
RESERVED = ((set(DATA_SERIES) | set(DATA_SERIES_EN) | set(OPERATORS)
             | LOGIC_AND | LOGIC_OR | LOGIC_NOT) - OUTPUT_NAMES)


def get_operator_meta():
    """返回算子清单（供前端函数库面板渲染）。"""
    data_fields = [{'name': k, 'display': k, 'category': '原始数据', 'desc': f'{k}序列'}
                   for k in DATA_SERIES]
    data_fields += [{'name': k, 'display': k, 'category': '英文兼容', 'desc': f'={v}'}
                    for k, v in DATA_SERIES_EN.items()]
    return {'operators': OPERATOR_META, 'data': data_fields,
            'logic': ['且', '或', '非'], 'output': '输出',
            'comment': '#'}


# ============================================================
# 解析器
# ============================================================

_CMP_OPS = {'>', '<', '>=', '<=', '=', '==', '!=', '<>'}


class _Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    # -- 基础 --
    def _cur(self):
        return self.tokens[self.pos]

    def _advance(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def _expect(self, kind, value=None):
        tok = self._cur()
        if tok.kind != kind or (value is not None and tok.value != value):
            raise FormulaError(f'此处应为 {value or kind}，实际是 {tok.value!r}', tok.line, tok.col)
        return self._advance()

    # -- 入口 --
    def parse_program(self):
        stmts = []
        while self._cur().kind != 'EOF':
            if self._cur().kind == 'SEMI':
                self._advance()
                continue
            stmts.append(self._parse_statement())
            if self._cur().kind == 'SEMI':
                self._advance()
            elif self._cur().kind != 'EOF':
                tok = self._cur()
                raise FormulaError(f'语句结尾应为 ;，实际是 {tok.value!r}', tok.line, tok.col)
        if not stmts:
            raise FormulaError('公式为空', 1, 1)
        if len(stmts) > MAX_STATEMENTS:
            raise FormulaError(f'语句数超过上限 {MAX_STATEMENTS}', 1, 1)
        return stmts

    def _parse_statement(self):
        tok = self._cur()
        if tok.kind == 'IDENT' and self.pos + 1 < len(self.tokens) \
                and self.tokens[self.pos + 1].kind == 'OP' \
                and self.tokens[self.pos + 1].value == ':=':
            name = tok.value
            if name in RESERVED:
                raise FormulaError(f'{name} 是保留字，不能作为变量名', tok.line, tok.col)
            self._advance()
            self._advance()
            expr = self._parse_expr(0)
            return Assign(name, expr, tok.line, tok.col)
        return self._parse_expr(0)

    # -- 表达式（按优先级分层） --
    def _parse_expr(self, depth):
        if depth > MAX_NEST_DEPTH:
            tok = self._cur()
            raise FormulaError('表达式嵌套过深', tok.line, tok.col)
        return self._parse_or(depth)

    def _parse_or(self, depth):
        left = self._parse_and(depth + 1)
        values = [left]
        while self._cur().kind == 'IDENT' and self._cur().value in LOGIC_OR:
            self._advance()
            values.append(self._parse_and(depth + 1))
        if len(values) == 1:
            return left
        return BoolOp('或', values)

    def _parse_and(self, depth):
        left = self._parse_not(depth + 1)
        values = [left]
        while self._cur().kind == 'IDENT' and self._cur().value in LOGIC_AND:
            self._advance()
            values.append(self._parse_not(depth + 1))
        if len(values) == 1:
            return left
        return BoolOp('且', values)

    def _parse_not(self, depth):
        tok = self._cur()
        if tok.kind == 'IDENT' and tok.value in LOGIC_NOT:
            self._advance()
            return UnaryOp('非', self._parse_not(depth + 1))
        return self._parse_cmp(depth + 1)

    def _parse_cmp(self, depth):
        left = self._parse_add(depth + 1)
        ops = []
        comps = []
        while self._cur().kind == 'OP' and self._cur().value in _CMP_OPS:
            ops.append(self._advance().value)
            comps.append(self._parse_add(depth + 1))
        if not ops:
            return left
        return Compare(left, ops, comps)

    def _parse_add(self, depth):
        node = self._parse_mul(depth + 1)
        while self._cur().kind == 'OP' and self._cur().value in ('+', '-'):
            op = self._advance().value
            node = BinOp(op, node, self._parse_mul(depth + 1))
        return node

    def _parse_mul(self, depth):
        node = self._parse_unary(depth + 1)
        while self._cur().kind == 'OP' and self._cur().value in ('*', '/'):
            op = self._advance().value
            node = BinOp(op, node, self._parse_unary(depth + 1))
        return node

    def _parse_unary(self, depth):
        tok = self._cur()
        if tok.kind == 'OP' and tok.value in ('+', '-'):
            self._advance()
            return UnaryOp(tok.value, self._parse_unary(depth + 1))
        return self._parse_atom(depth + 1)

    def _parse_atom(self, depth):
        tok = self._cur()
        if tok.kind == 'NUM':
            self._advance()
            return Num(tok.value)
        if tok.kind == 'LP':
            self._advance()
            node = self._parse_expr(depth + 1)
            self._expect('RP')
            return node
        if tok.kind == 'IDENT':
            name = tok.value
            self._advance()
            if self._cur().kind == 'LP':
                self._advance()
                args = []
                if self._cur().kind != 'RP':
                    args.append(self._parse_expr(depth + 1))
                    while self._cur().kind == 'COMMA':
                        self._advance()
                        args.append(self._parse_expr(depth + 1))
                self._expect('RP')
                spec = OPERATORS.get(name)
                if spec is None:
                    raise FormulaError(f'未知函数 {name}（可用函数见左侧函数库）', tok.line, tok.col)
                lo, hi = spec['arity']
                if not (lo <= len(args) <= hi):
                    raise FormulaError(
                        f'{name} 需要 {lo if lo == hi else f"{lo}~{hi}"} 个参数，实际给了 {len(args)} 个',
                        tok.line, tok.col)
                return Call(name, args, tok.line, tok.col)
            return Ref(name, tok.line, tok.col)
        raise FormulaError(f'此处应为数值/变量/函数，实际是 {tok.value!r}', tok.line, tok.col)


def parse_formula(text):
    """解析公式文本，返回语句列表；失败抛 FormulaError。"""
    if text is None or not str(text).strip():
        raise FormulaError('公式为空', 1, 1)
    if len(str(text)) > MAX_FORMULA_LEN:
        raise FormulaError(f'公式长度超过上限 {MAX_FORMULA_LEN} 字符', 1, 1)
    tokens = tokenize(text)
    return _Parser(tokens).parse_program()


# ============================================================
# 求值
# ============================================================

def _to_array(v, n):
    if isinstance(v, np.ndarray):
        if v.shape == ():
            return np.full(n, float(v), dtype=float)
        return v
    return np.full(n, float(v), dtype=float)


def _to_mask(v, n):
    if isinstance(v, np.ndarray):
        if v.dtype == bool:
            return v if v.shape == (n,) else np.broadcast_to(v, (n,)).copy()
        arr = v.astype(float) if v.shape != () else np.full(n, float(v), dtype=float)
        return np.nan_to_num(arr, nan=0.0) != 0
    return np.full(n, bool(v), dtype=bool)


def _eval_node(node, env, n):
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Ref):
        if node.name in env:
            return env[node.name]
        key = DATA_SERIES.get(node.name) or DATA_SERIES_EN.get(node.name)
        if key is not None and key in env:
            return env[key]
        if key is not None:
            return np.full(n, np.nan, dtype=float)
        raise FormulaError(f'变量 {node.name} 未定义（变量须先用 := 定义）', node.line, node.col)
    if isinstance(node, Call):
        spec = OPERATORS[node.func]
        args = [_eval_node(a, env, n) for a in node.args]
        try:
            return spec['fn'](*args)
        except FormulaError:
            raise
        except Exception as e:
            raise FormulaError(f'{node.func} 计算失败：{e}', node.line, node.col)
    if isinstance(node, UnaryOp):
        val = _eval_node(node.operand, env, n)
        if node.op == '非':
            return np.logical_not(_to_mask(val, n))
        arr = _to_array(val, n)
        return arr if node.op == '+' else -arr
    if isinstance(node, BoolOp):
        masks = [_to_mask(_eval_node(v, env, n), n) for v in node.values]
        out = masks[0]
        for m in masks[1:]:
            out = np.logical_and(out, m) if node.op == '且' else np.logical_or(out, m)
        return out
    if isinstance(node, Compare):
        left = _to_array(_eval_node(node.left, env, n), n)
        result = None
        for op, comp_node in zip(node.ops, node.comparators):
            right = _to_array(_eval_node(comp_node, env, n), n)
            if op == '>':
                cur = left > right
            elif op == '<':
                cur = left < right
            elif op == '>=':
                cur = left >= right
            elif op == '<=':
                cur = left <= right
            else:  # '=' '==' '!=' '<>'
                eq = np.isclose(left, right, rtol=1e-9, atol=1e-12)
                cur = ~eq if op in ('!=', '<>') else eq
            result = cur if result is None else np.logical_and(result, cur)
            left = right
        return result
    if isinstance(node, BinOp):
        left = _to_array(_eval_node(node.left, env, n), n)
        right = _to_array(_eval_node(node.right, env, n), n)
        with np.errstate(divide='ignore', invalid='ignore', over='ignore'):
            if node.op == '+':
                return left + right
            if node.op == '-':
                return left - right
            if node.op == '*':
                return left * right
            return left / right
    raise FormulaError('表达式包含不支持的结构', 1, 1)


def _scalar_last(series):
    """取序列最后一个有限值；无有限值返回 0.0。"""
    if not isinstance(series, np.ndarray):
        try:
            v = float(series)
        except (TypeError, ValueError):
            return 0.0
        return v if np.isfinite(v) else 0.0
    arr = np.asarray(series, dtype=float).ravel()
    if arr.size == 0:
        return 0.0
    mask = np.isfinite(arr)
    if not mask.any():
        return 0.0
    return float(arr[np.nonzero(mask)[0][-1]])


class CompiledFormula:
    """编译后的公式：可对整段 K 线求值，也可取末值当因子。"""

    __slots__ = ('statements', 'variables', 'output_name')

    def __init__(self, statements, variables, output_name):
        self.statements = statements
        self.variables = variables
        self.output_name = output_name

    def _build_env(self, ctx):
        env = {}
        for key in ('closes', 'highs', 'lows', 'opens', 'volumes'):
            raw = ctx.get(key)
            if raw is None:
                raw = []
            env[key] = np.asarray(raw, dtype=float)
        return env

    def eval_series(self, ctx):
        """对整段 K 线求值，返回输出序列（numpy，长度=K线长度）。"""
        env = self._build_env(ctx)
        n = len(env['closes'])
        if n == 0:
            return np.zeros(0, dtype=float)
        result = None
        for stmt in self.statements:
            if isinstance(stmt, Assign):
                env[stmt.name] = _eval_node(stmt.expr, env, n)
                result = env[stmt.name]
            else:
                result = _eval_node(stmt, env, n)
        if self.output_name and self.output_name in env:
            result = env[self.output_name]
        arr = _to_array(result, n)
        if arr.dtype == bool:
            arr = arr.astype(float)
        return np.asarray(arr, dtype=float)

    def eval_scalar(self, ctx):
        """取输出序列末值（因子值）；异常/NaN 回落 0.0。"""
        try:
            return _scalar_last(self.eval_series(ctx))
        except FormulaError:
            raise
        except Exception:
            return 0.0


def compile_formula(text):
    """编译公式文本，返回 CompiledFormula；失败抛 FormulaError。"""
    statements = parse_formula(text)

    # 变量依赖校验：引用必须先定义（数据序列除外）
    defined = set()
    variables = []
    output_name = None
    for stmt in statements:
        if isinstance(stmt, Assign):
            if stmt.name in defined:
                raise FormulaError(f'变量 {stmt.name} 重复定义', stmt.line, stmt.col)
            defined.add(stmt.name)
            variables.append(stmt.name)
            if stmt.name in OUTPUT_NAMES:
                output_name = stmt.name
        _check_refs(stmt, defined)
    if output_name is None and variables:
        # 默认取最后一条「赋值语句」为输出
        output_name = variables[-1]
    return CompiledFormula(statements, variables, output_name)


def _check_refs(node, defined):
    if isinstance(node, Ref):
        if node.name in defined or node.name in DATA_SERIES or node.name in DATA_SERIES_EN:
            return
        raise FormulaError(f'变量 {node.name} 未定义（变量须先用 := 定义）', node.line, node.col)
    if isinstance(node, Assign):
        _check_refs(node.expr, defined)
        return
    if isinstance(node, Call):
        for a in node.args:
            _check_refs(a, defined)
        return
    if isinstance(node, BinOp):
        _check_refs(node.left, defined)
        _check_refs(node.right, defined)
        return
    if isinstance(node, UnaryOp):
        _check_refs(node.operand, defined)
        return
    if isinstance(node, BoolOp):
        for v in node.values:
            _check_refs(v, defined)
        return
    if isinstance(node, Compare):
        _check_refs(node.left, defined)
        for c in node.comparators:
            _check_refs(c, defined)
        return


def compile_to_factor(text):
    """把公式编译成 factor_registry 所需的 calc_fn(ctx, params) -> float。"""
    compiled = compile_formula(text)

    def _calc_fn(ctx, params=None):
        try:
            return compiled.eval_scalar(ctx)
        except Exception:
            return 0.0

    return _calc_fn


# ============================================================
# 校验（供 API / 编辑器）
# ============================================================

def validate_formula(text):
    """校验公式，返回 {'ok', 'errors', 'variables', 'output'}。"""
    try:
        compiled = compile_formula(text)
    except FormulaError as e:
        return {'ok': False, 'errors': [e.to_dict()], 'variables': [], 'output': None}
    except Exception as e:
        return {'ok': False, 'errors': [{'line': 1, 'col': 1, 'msg': str(e)}],
                'variables': [], 'output': None}
    return {'ok': True, 'errors': [], 'variables': compiled.variables,
            'output': compiled.output_name}


# ============================================================
# 预设模板（全中文）
# ============================================================

TEMPLATES = {
    'KDJ': (
        'RSV := (收盘价 - 最低N日(最低价, 9)) / (最高N日(最高价, 9) - 最低N日(最低价, 9)) * 100;\n'
        'K   := 平滑(RSV, 3, 1);\n'
        'D   := 平滑(K, 3, 1);\n'
        '输出 := 3 * K - 2 * D;'
    ),
    'MACD': (
        '快线 := 指数均线(收盘价, 12);\n'
        '慢线 := 指数均线(收盘价, 26);\n'
        'DIF  := 快线 - 慢线;\n'
        'DEA  := 指数均线(DIF, 9);\n'
        '输出 := (DIF - DEA) / 收盘价 * 100;'
    ),
    'RSI': (
        '涨 := 最大(收盘价 - 前值(收盘价, 1), 0);\n'
        '跌 := 最大(前值(收盘价, 1) - 收盘价, 0);\n'
        '输出 := 求和(涨, 14) / (求和(涨, 14) + 求和(跌, 14)) * 100;'
    ),
    '牛熊强度': (
        '牛熊线 := 均线(收盘价, 30);\n'
        '强度   := (收盘价 - 前值(收盘价, 5)) / 前值(收盘价, 5) * 100;\n'
        '输出   := 强度 + (收盘价 - 牛熊线) / 牛熊线 * 100;'
    ),
    '条件因子示例': (
        '牛熊线 := 均线(收盘价, 30);\n'
        '强度   := (收盘价 - 前值(收盘价, 5)) / 前值(收盘价, 5) * 100;\n'
        '输出   := 上穿(收盘价, 牛熊线) 且 强度 > 3;'
    ),
}


def get_templates():
    return [{'name': k, 'formula': v} for k, v in TEMPLATES.items()]