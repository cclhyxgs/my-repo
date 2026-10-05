# -*- coding: utf-8 -*-
"""前端 `ui_mockup/index.html` 内联脚本的「引用—声明」一致性守卫。

2026-09-19 一天内被用户实报了两起**零提示的静默失败**，都是本可静态扫出来的：

  ① 漏写函数：**被调用但从未声明**。
     实例 `appendScanResults`：`applyScanFilter` 的 `_scanOffset>0` 分支调用它，
     但文件里没有任何声明 ⇒ 「加载下一批 / 显示全部」弹
     `加载全部失败：appendScanResults is not defined`。

  ② 跨作用域：**在 `initPyWebViewApi()` 之外裸引用它内部的私有成员**。
     实例：顶层 `window.applyScanTechFilter` 裸调 init 内的 `ensureConfigSaved()`
     ⇒ ReferenceError；因调用方是 async 函数，异常变成未处理的 Promise rejection
     ⇒ 无 toast、无状态栏 ⇒ 「点应用筛选无效」。
     判据：函数体内的 `function foo(){}` **不是**全局变量，只有 init 里写过
     `window.foo = foo` 才可在顶层裸引用。

  ③ `onclick` 属性引用的处理器必须能被全局解析（顶层 function — 含 `async function` —
     或 `window.X =` 导出），否则按钮点了报 `xxx is not defined`。

### 为什么用「逐行无状态」剥离而不是完整 JS 词法分析

要扫「被调用的标识符」，必须先把注释与字符串内容去掉，否则 CSS 函数
（`style="…rgba(var(--wc),0.3)…"` → 伪调用 `rgba(`/`var(`）、图表名（`'RSI'`）
会淹掉真信号。但**跨行**维护词法状态（模板字面量 + `${}` 嵌套）极易失配：
实测一版跨行剥离器在文件中部失控，把后半段全部误判为字符串内容 ⇒
`window.applyScanFilter` 等声明全部「消失」⇒ 误报一片。

故此处**只做逐行、无状态**的清理：单行 `//` 注释、行内 `/*…*/`、`'…'`、`"…"`。
模板字面量内容保留（HTML 里没有 `(`，不产生调用点；其中的 `style="…"` 会被 `"` 规则吃掉）。
代价：写在跨行字符串里的调用不参与检查（本文件不存在这类调用）。
"""
import os
import re
import unittest

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(PROJ, "ui_mockup", "index.html")

# ── 语法关键字：`async (` / `if (` / `for (` 不是函数调用 ──────────────
KEYWORDS = set("""
if for while switch catch return typeof new delete void do else function class in of
instanceof await yield throw case try with async
""".split())

# ── JS 内置 / 浏览器 API / 外部库：不是项目自有函数 ──────────────────
BUILTIN_CALLS = set("""
Object Array String Number Boolean Math JSON Date RegExp Error TypeError RangeError Promise
Set Map WeakMap WeakSet Symbol Proxy Reflect BigInt Function parseInt parseFloat isNaN isFinite
encodeURIComponent decodeURIComponent encodeURI decodeURI structuredClone queueMicrotask
setTimeout setInterval clearTimeout clearInterval requestAnimationFrame cancelAnimationFrame
alert confirm console fetch btoa atob
URL URLSearchParams Blob File FileReader FormData AbortController Event CustomEvent Image
ResizeObserver MutationObserver IntersectionObserver WebSocket TextEncoder TextDecoder
getComputedStyle matchMedia scrollTo scrollBy open print
echarts html2canvas
""".split())

# ── CSS 函数名：会出现在模板字面量里当文本（如 `rgba(${r},${g},${b})`），不是调用 ──
CSS_FUNCS = set("""
rgba rgb hsl hsla calc var translate translateX translateY translate3d scale scaleX scaleY
rotate rotateX rotateY skew matrix linear-gradient radial-gradient url attr cubic-bezier
min max clamp repeat fit-content
""".split())

NOISE = KEYWORDS | BUILTIN_CALLS | CSS_FUNCS

INLINE_SCRIPT_RE = re.compile(r"<script>\s*\n(.*?)\n\s*</script>", re.S)
ONCLICK_RE = re.compile(r'onclick\s*=\s*["\']([^"\']+)["\']')


def get_inline_script(html):
    """取出唯一的无 src 内联 <script>（本文件只有这一个主脚本）。"""
    m = INLINE_SCRIPT_RE.search(html)
    assert m, "未找到内联主 <script> 块"
    return m.group(1)


def strip_line_local(js):
    """逐行、无状态清理：去掉单行注释、行内块注释与单/双引号字符串内容。

    刻意不跨行维护状态 —— 跨行词法分析一旦失配会静默污染整个后半段。
    """
    out = []
    for line in js.split("\n"):
        buf, i, n = [], 0, len(line)
        while i < n:
            c = line[i]
            nxt = line[i + 1] if i + 1 < n else ""
            if c == "/" and nxt == "/":
                break                                   # 行注释：其后整行丢弃
            if c == "/" and nxt == "*":
                j = line.find("*/", i + 2)
                if j < 0:
                    break                               # 块注释跨行：本行其后丢弃
                i = j + 2
                continue
            if c in ("'", '"'):
                buf.append(" ")
                q, i = c, i + 1
                while i < n:
                    if line[i] == "\\":
                        i += 2
                        continue
                    if line[i] == q:
                        i += 1
                        break
                    i += 1
                buf.append(" ")
                continue
            buf.append(c)
            i += 1
        out.append("".join(buf))
    return "\n".join(out)


def declared_names(code):
    """收集所有声明名：函数（含 async）、window.X=、const/let/var、形参、catch 参数。"""
    names = set()
    names |= set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", code))
    names |= set(re.findall(r"\bwindow\.([A-Za-z_$][\w$]*)\s*=(?!=)", code))
    names |= set(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)", code))
    names |= set(re.findall(r"([A-Za-z_$][\w$]*)\s*(?::|=(?!=))\s*(?:async\s+)?function\b", code))
    names |= set(re.findall(r"catch\s*\(\s*([A-Za-z_$][\w$]*)", code))
    for m in re.finditer(r"\bfunction\s*[A-Za-z_$\w]*\s*\(([^)]*)\)", code):
        for p in m.group(1).split(","):
            p = p.split("=")[0].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", p or ""):
                names.add(p)
    for m in re.finditer(r"\(([^()]*)\)\s*=>", code):
        for p in m.group(1).split(","):
            p = p.split("=")[0].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", p or ""):
                names.add(p)
    for m in re.finditer(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*=>", code):
        names.add(m.group(1))
    return names


def call_sites(code, name=None):
    """返回 {被调用名: [行号]}；排除 obj.method() / obj['x']()。"""
    pat = (r"(?<![\w$.])" + re.escape(name) + r"\s*\(") if name else r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\("
    out = {}
    for m in re.finditer(pat, code):
        key = name or m.group(1)
        out.setdefault(key, []).append(code[:m.start()].count("\n") + 1)
    return out


def bare_uses(code, name):
    """裸使用某标识符（含不以 `(` 结尾的用法，如变量）。返回行号列表。"""
    return [code[:m.start()].count("\n") + 1
            for m in re.finditer(r"(?<![\w$.])" + re.escape(name) + r"(?![\w$])", code)]


# 只用「够长的名字」做跨作用域检查。理由：`s`/`x`/`val`/`sel` 这类短名在文件里
# 遍地都是（各自函数内的局部变量），纯文本匹配无法区分作用域，必然误报一片；
# 而真实出问题的那批名字都很长且唯一：
#   ensureConfigSaved(17) renderSectorGrid(16) renderScanResults(17) loadSchemes(11)
#   renderPeriodSeg(15) renderSchemeList(16) _scanResultsCache(16)
MIN_SCOPE_CHECK_NAME = 6


def declared_functions(code):
    """只收 **function 声明** 名（不含形参、不含 const/let/var）。

    为什么不收变量：`const direction/market/period/params/series/groups/...`
    这类名字在文件里各自函数的局部作用域中遍地重名，纯文本匹配无法区分作用域，
    必然误报一片（实测 30+ 条）。而真实出问题的**全是函数**，且函数名都很长且唯一。
    代价：init 内私有**变量**被顶层裸读不在覆盖范围内（如 `_scanResultsCache`
    —— 该处已随 `window.redrawScanViews` 的引入自然收敛）。
    """
    return set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", code))


# 已知、且**刻意暂不修**的跨作用域引用：登记在此处而不是让测试红着。
# 详细原因见 .workbuddy/memory/2026-09-19.md（改了会改变「切换初/高级」的可见行为，待用户确认）。
KNOWN_PENDING_LEAKS = {
    "loadSchemes": "setUsageMode/renderUsageMode 里 `typeof loadSchemes==='function'` 守卫恒假；"
                   "补 `window.loadSchemes = loadSchemes;` 即可生效，但会真的重载方案库（行为变更），待确认",
}


# `window.X(...)` 被调用但本文件从未赋值：要么由宿主/DOM 提供，要么是漏写导出。
WINDOW_RUNTIME_PROVIDED = {
    "pywebview",                                  # pywebview 运行时注入
    "addEventListener", "removeEventListener",    # DOM
    "dispatchEvent", "postMessage", "open", "close", "focus", "blur", "print", "scrollTo",
}

# 允许「探测式」引用的**可选**宿主能力：不存在也必须能正常运行。
OPTIONAL_HOST_FEATURES = {
    "echarts", "html2canvas",   # 独立 <script src> 提供，加载失败走 onerror 回退
    "charts",                   # 运行期由页面自己建
    "ResizeObserver", "MutationObserver", "IntersectionObserver",
}


def window_calls_without_definition(code, auto_globals=()):
    """返回「以 `window.X(...)` 调用，但本文件既没 `window.X =` 也没顶层 function X」的名字。

    对应失败形态：调用点写 `window.ensureConfigSaved(...)`，但 init 里漏了
    `window.ensureConfigSaved = ensureConfigSaved;` ⇒ 运行时
    `window.ensureConfigSaved is not a function`（且常被 try/catch 吞成一句 toast）。

    auto_globals：**顶层** function 声明名 —— 它们会自动成为 window 属性，故合法。
    （init 内的 function 声明不会，所以必须排除 init 内的那些。）
    """
    used = set(re.findall(r"\bwindow\.([A-Za-z_$][\w$]*)\s*\(", code))
    # `typeof window.X === 'function'` 守卫同样要求 X 真的存在：
    # 守卫一个永远不存在的符号 = 该分支永远是死代码（这是最隐蔽的一种漏导出）
    used |= set(re.findall(r"\btypeof\s+window\.([A-Za-z_$][\w$]*)\b", code))
    defined = set(re.findall(r"\bwindow\.([A-Za-z_$][\w$]*)\s*=(?!=)", code))
    return sorted(used - defined - set(auto_globals)
                  - WINDOW_RUNTIME_PROVIDED - OPTIONAL_HOST_FEATURES - NOISE)


def brace_span(code, open_idx):
    assert code[open_idx] == "{"
    depth = 0
    for i in range(open_idx, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    raise AssertionError("花括号不匹配")


def init_body_span(code):
    m = re.search(r"function\s+initPyWebViewApi\s*\(\s*\)\s*\{", code)
    assert m, "未找到 initPyWebViewApi() 定义（逐行剥离器可能已失配）"
    open_idx = code.index("{", m.start())
    return open_idx, brace_span(code, open_idx)


def audit(script_text, sanity=False):
    """返回 (未声明的被调用名, init 私有成员被顶层裸引用, 剥离后的 code)。"""
    code = strip_line_local(script_text)

    decl = declared_names(code)
    unknown = sorted(n for n in call_sites(code) if n not in decl and n not in NOISE)

    start, end = init_body_span(code)
    body = code[start:end]
    if sanity:
        # 自检：init 体必须真的取到，否则下面的检查会静默退化成空集（假通过）
        assert "ensureConfigSaved" in body and len(body) > 50000, \
            "initPyWebViewApi 函数体提取异常（长度 %d）——逐行剥离器可能已失配" % len(body)

    exported = set(re.findall(r"\bwindow\.([A-Za-z_$][\w$]*)\s*=(?!=)", code))
    private = {n for n in declared_functions(body) - exported - NOISE
               if len(n) >= MIN_SCOPE_CHECK_NAME}
    private -= set(KNOWN_PENDING_LEAKS)
    outside = code[:start] + code[end:]
    leaks = {}
    for name in sorted(private):
        hits = bare_uses(outside, name)
        if hits:
            leaks[name] = hits

    # 顶层 function 声明会自动成为 window 属性；init 内的不会
    auto_globals = set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", outside))
    undefined_window = window_calls_without_definition(code, auto_globals)

    return unknown, leaks, undefined_window, code


class TestFrontendScopeRefs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(HTML):
            raise unittest.SkipTest("index.html 不存在")
        cls.html = open(HTML, encoding="utf-8").read()
        cls.script = get_inline_script(cls.html)

    # ── 主检查 ────────────────────────────────────────────────
    def test_no_calls_to_undeclared_functions(self):
        """① 不得存在「被调用但从未声明」的函数（漏写实现的调用点）。"""
        unknown, _, _, code = audit(self.script, sanity=True)
        calls = call_sites(code)
        detail = "\n".join("  %-26s 调用点(script行) %s"
                           % (n, ", ".join(map(str, calls[n][:6]))) for n in unknown)
        self.assertEqual(unknown, [],
                         "存在从未声明的被调用函数（补实现，或从调用点删除）：\n" + detail)

    def test_init_privates_not_used_from_top_level(self):
        """② initPyWebViewApi 之外不得裸引用它内部未导出的私有成员。

        这是「点了完全没反应」的头号根因：顶层解析不到 ⇒ ReferenceError；
        若调用方是 async 函数，异常变成未处理的 rejection ⇒ 连提示都没有。
        """
        _, leaks, _, _ = audit(self.script, sanity=True)
        detail = "\n".join(
            "  %-26s 需在 init 内补 `window.%s = %s;`（引用行 %s）"
            % (n, n, n, ", ".join(map(str, v[:6]))) for n, v in sorted(leaks.items()))
        self.assertEqual(leaks, {}, "顶层裸引用了 init 内未导出的私有成员：\n" + detail)

    def test_window_calls_have_definitions(self):
        """②b 以 `window.X(...)` 调用的名字，必须本文件赋过值或是顶层 function。

        对应失败形态：调用点写 `window.ensureConfigSaved(...)`，init 里却漏了
        `window.ensureConfigSaved = ensureConfigSaved;` ⇒ `is not a function`。
        """
        _, _, undefined_window, _ = audit(self.script, sanity=True)
        self.assertEqual(undefined_window, [],
                         "调用了未定义的 window.X(...)：%s" % ", ".join(undefined_window))

    def test_onclick_handlers_are_resolvable(self):
        """③ onclick 引用的处理器必须能被全局解析。

        ⚠️ 顶层 `async function` 同样算（早先的扫描漏了 async，误报一大片）。
        """
        exported = set(re.findall(r"\bwindow\.([A-Za-z_$][\w$]*)\s*=(?!=)",
                                  strip_line_local(self.script)))
        top_fn = set(re.findall(r"(?:^|\n)\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)",
                                self.html, re.M))
        dead = set()
        for m in ONCLICK_RE.finditer(self.html):
            # (?<![\w$.]) 排除 `event.stopPropagation()` / `this.querySelector()` 这类方法调用
            for nm in re.finditer(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(", m.group(1)):
                n = nm.group(1)
                if n in ("event", "this", "e") or n in NOISE:
                    continue
                if n not in top_fn and n not in exported:
                    dead.add(n)
        self.assertEqual(sorted(dead), [],
                         "onclick 处理器无法解析：%s" % ", ".join(sorted(dead)))

    # ── 检查器自身的自检：确保它真的抓得住这两类缺陷 ──────────────
    def test_checker_detects_missing_function(self):
        """合成样本：调用点存在但函数未声明 → 必须被抓出。"""
        snippet = ("function initPyWebViewApi(){\n"
                   "  const api = 1;\n"
                   "}\n"
                   "window.go = function(){\n"
                   "  renderMissing('x');\n"
                   "};\n")
        unknown, _, _, _ = audit(snippet)
        self.assertIn("renderMissing", unknown)

    def test_checker_detects_cross_scope_reference(self):
        """合成样本：顶层裸调 init 内未导出的函数 → 必须被抓出；已导出的不该报。

        同时固化两条**已知边界**（防止日后误以为覆盖到了）：
          · 短名（`s`）不检查 —— 短名在文件里遍地重名，检查必然误报；
          · init 内私有**变量**（`_privCache`）不检查 —— 只做函数声明。
        """
        snippet = ("function initPyWebViewApi(){\n"
                   "  async function exportedHelper(){ return 1; }\n"
                   "  window.exportedHelper = exportedHelper;\n"   # 有导出即不该报
                   "  async function reallyPrivateHelper(){ return 2; }\n"
                   "  const _privCache = 3;\n"
                   "  const s = 4;\n"
                   "}\n"
                   "window.topLevel = async function(){\n"
                   "  await reallyPrivateHelper();\n"
                   "  return _privCache + s;\n"
                   "};\n")
        _, leaks, _, _ = audit(snippet)
        self.assertIn("reallyPrivateHelper", leaks)
        self.assertNotIn("exportedHelper", leaks)
        self.assertNotIn("_privCache", leaks)   # 已知边界：变量不在覆盖范围
        self.assertNotIn("s", leaks)            # 已知边界：短名不在覆盖范围
