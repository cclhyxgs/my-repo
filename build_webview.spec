# -*- mode: python ; coding: utf-8 -*-
# WebView 版打包配置（PyInstaller onefile，单文件 EXE）
# 用途：把 main_webview.py 及全部依赖/资源打成一个「dist/M-Bull.exe」，直接分发该单文件即可。
# 为什么 onefile：用户要求单文件分发（无需携带依赖文件夹）。历史上曾因「onefile+UPX」怀疑杀软
#   扫描临时解压的 .pyd 致卡顿，后改为 onedir；但最终定位真正的卡顿根因是诊断线程误 import tkinter
#   （已抽离到 ui.report_classify 修复），与打包格式无关，故可回收为 onefile。
# 为何仍关闭 UPX：onefile 每次启动把全部模块/DLL 解压到 %TEMP%/_MEIxxxxx，UPX 压缩的 .pyd 更
#   易被 Defender 等实时扫描，故排除 UPX（牺牲体积换启动稳定）。单文件体积偏大属预期。
# 为什么用 PyInstaller 而非 Nuitka：pywebview 6.x 在 Windows 的全部后端
# （winforms/edgechromium/mshtml）都依赖 pythonnet 的 clr 动态注入模块，
# Nuitka 无官方支持；PyInstaller 有 pythonnet/pywebview 官方 hook，可正确收集 .NET 桥接依赖。
# 运行：python -m PyInstaller build_webview.spec --noconfirm
# 产物：dist/M-Bull.exe（单文件），直接分发该 EXE 即可。

# ── 打包前强制清理环境污染（根治 seal 漂移 / 激活记录进包）───────
# 根因（2026-08-16 锁定）：
#   1. license/__pycache__ 残留跨版本 pyc（例如 3.13 + 3.14 混存），
#      PyInstaller Analysis 会优先捡起 .pyc，导致「seal 按源码 .py 算，
#      进包字节码按缓存 .pyc 编」→ 哈希失配 → fail-closed 正版锁死。
#   2. 开发机运行后 config/ 下有 license.json / machine_id，若有模块
#      在导入期通过 glob / pkgutil 扫描 config/，会把激活记录数据
#      作为 datas 收进包（虽非 seal 失配直接原因，但会把你机器的
#      授权状态带给终端用户 → 必须排除）。
#   3. build/ 目录残留上次 PYZ toc → 旧 _seal.py 被复用。
# 处理方式：Analysis 前先 rm -rf 三类污染。seal 仍走严格 fail-closed
# （反破译防护不丢），但保证输入侧绝对干净 = 不会误锁。
import shutil as _sh
_proj = SPECPATH
for _sub in ('build', '__pycache__'):
    _p = os.path.join(_proj, _sub)
    if os.path.isdir(_p):
        try:
            _sh.rmtree(_p)
            print('[build] 清理旧产物: %s' % _p)
        except Exception:
            pass
# 递归清所有子包 __pycache__（重点 license/ 与 ui/）
import glob as _gl
for _pc in _gl.glob(os.path.join(_proj, '**', '__pycache__'), recursive=True):
    try:
        _sh.rmtree(_pc)
        print('[build] 清 pycache: %s' % _pc)
    except Exception:
        pass
# 重建 build/build_webview 工作目录（rmtree build 后必须重新 mkdir，否则
# PyInstaller 在 create_base_library_zip 写 base_library.zip 时父目录不
# 存在 → FileNotFoundError）。PyInstaller 默认 workpath = cwd/build/<specstem>
os.makedirs(os.path.join(_proj, 'build', 'build_webview'), exist_ok=True)
# 把 config/ 从 datas 搜索的隐式候选里踢走：若 Analysis 阶段任何 hook
# 走 collect_data_files，把它限定在白名单包。这里改用显式排除目录标志：
#   → 真正生效的排除逻辑在下方 excludes + datas 白名单显式写法（已经是），
#     此处额外再删开发机 config/license.json / machine_id（注意是开发机
#     工作配置，不要动打包种子 engine/config/seed/），确保即便某 hook
#     走 os.listdir(CONFIG_DIR) 也看不到激活记录。
_dev_cfg = os.path.join(_proj, 'config')
if os.path.isdir(_dev_cfg):
    for _name in ('license.json', 'machine_id', 'watchlist.txt', 'watchlist_futures.txt',
                  'credentials.json', '.lstate', 'quant_model.json'):
        _f = os.path.join(_dev_cfg, _name)
        if os.path.exists(_f):
            try:
                os.remove(_f)
                print('[build] 移出开发机配置: %s' % _f)
            except Exception:
                pass

# ── license 自校验指纹（seal）──
# license/_seal.py 已在仓库中预生成并提交（与 license/ 校验侧源码字节码一致）。
# 打包直接使用该文件，无需重新生成。注意：修改 license 校验侧源码后需由作者侧
# gen_seal 工具重算指纹，否则冻结态会 fail-closed 锁死。
from PyInstaller.utils.hooks import collect_submodules

# pythonnet 全部子模块（含 .NET 运行时桥接），clr hook 由 pythonnet 包自带
pythonnet_hidden = collect_submodules('pythonnet')

# pytdx（通达信行情源，2026-09-19 接入为 A股日K 第一手前复权源）。
# engine/data_sources/tdx.py 里是**函数内延迟 import**（未安装时回退腾讯），
# PyInstaller 静态分析扫不到 ⇒ 必须显式收集全部子模块。漏登记会让 frozen
# 下的 ImportError 被 try/except 吞掉，表现为「装了 pytdx 却没生效、静默走腾讯」。
try:
    pytdx_hidden = collect_submodules('pytdx')
except Exception:
    pytdx_hidden = []

hiddenimports = ['clr', 'pythonnet'] + pythonnet_hidden + pytdx_hidden + [
        # WebView Windows 后端（不收集 gtk/cocoa/qt/cef，避免引入 gi 等跨平台依赖）
        # 仅保留 edgechromium（WebView2，Windows 10/11 自带）：单一干净窗口，杜绝
        # winforms/mshtml 回退在某些打包环境下弹出额外窗口/双窗口。win32 保留为
        # 兜底引用（不影响后端选择，gui 已强制 edgechromium）。
        'webview',
        'webview.platforms.edgechromium',
        'webview.platforms.win32',
        # 业务包（main_webview 内 from ui.web_api import WebAPI）
        'ui',
        'ui.web_api',
        # matplotlib 仅用无界面 Agg 后端（Web 版不打包 Tk/GUI 后端，否则会拖入 tkinter + _tcl_data）
        'matplotlib.backends.backend_agg',
        'cryptography.hazmat.primitives.asymmetric.ed25519',
        'engine.backtest',
        'engine.backtest_strategy',
        'engine.backtest_futures',
        'engine.backtest_cli',
        'engine.backtest_runner',
        'engine.backtest_launcher',
        'engine.backtest_protocol',
        # ── 自选诊断独有、全市场扫描不走的模块图（静态导入自 ui.web_api，
        #    这里显式声明以防 PyInstaller 漏收集动态/延迟导入导致 EXE 下异常路径）──
        'engine.trading_pipeline',
        'engine.trading_context',
        'engine.unified_scorer',
        'engine.unified_entry_logic',
        'engine.market_gate',
        'engine.stock_classifier',
        'engine.add_engine',
        'engine.reduce_engine',
        'engine.ghost_engine',
        'engine.indicators_advanced',
        'engine.report_builder',
        # report_data 供 AI 路径延迟 import（scan_prompt 取 scan_stats / AI 两层投喂），
        # 虽然经 analyze_service 顶层 import 可被间接收集，但延迟 import 一旦漏收集
        # 就是 ModuleNotFoundError 被 except 吞掉 → 排查成本极高，故显式登记。
        'engine.report_data',
        # 风险偏好测评（十题 → 四档风控参数）：仅供 UI 桥接延迟 import，显式登记防漏收集
        'engine.risk_profile',
        'engine.score_calculator_v2',
        'engine.factor_tech',
        'engine.scoring_core',
        'engine.exceptions',
        'engine.config',
        # ── 数据源适配层（data_sources 为延迟/动态导入，显式登记防漏收集）──
        'engine.data_sources',
        'engine.data_sources.base',
        'engine.data_sources.builtin',
        'engine.data_sources.registry',
        'engine.data_sources.tdx',
        'engine.futures_pool',
        'engine.futures_data',
        'license',
        'license.license_manager',
        # 授权硬门禁 + 自校验指纹（seal）：引擎层 enforce 与导入时自校验依赖二者；
        # _seal.py 已在仓库中预生成并提交（由作者侧 gen_seal 工具基于最终进包字节码生成）。
        'license.license_guard',
        'license._seal',
        'license._seal_spec',
        # 报告分类纯逻辑（无 tkinter 依赖）；web_api 诊断路径从 ui.report_classify
        # import _classify_for_report，避免拉入 ui.watchlist_diagnosis 的 tkinter 顶层依赖
        'ui.report_classify',
        # ── L1 数据源可配置化（data_layer 函数内延迟 import registry，显式声明防漏）──
        'engine.data_sources',
        'engine.data_sources.base',
        'engine.data_sources.builtin',
        'engine.data_sources.registry',
        # ── L3 监控守护 + L4 通知层（web_api/monitor 函数内延迟 import，显式声明防漏）──
        'ui.monitor',
        'engine.notifiers',
        'engine.notifiers.base',
        'engine.notifiers.channels',
        'engine.notifiers.registry',
        # ── 快速起步向导（web_api 函数内延迟 import）──
        'ui.onboarding_wizard',
        # ── 其余函数内延迟 import 的业务模块（modulegraph 静态分析可能漏，显式声明兜底）──
        'engine.data_layer',
        'engine.quant_config',
        'engine.indicators',
        'engine.factor_registry',
        # 否决项可插拔注册表：quant_config 顶层导入 + factor_tech._build_extreme_vetos
        # 函数内延迟导入（动态 import，静态分析可能漏，显式声明兜底）
        'engine.veto_registry',
        'engine.stock_pool',
        'engine.market_scan_core',
        'engine.machine_probe',
        'engine.error_log',
        'engine.parallel_utils',
        'engine.signal_rating',
        'engine.state',
        'engine.backtest_cancel',
        # 内置 K 线定时刷新：main_webview 里是**函数内延迟 import** 且包在
        # try/except 中（调度起不来不影响主流程）—— 一旦 PyInstaller 漏收集，
        # ImportError 会被 except 静默吞掉，表现为「功能没做」而非报错。
        # 故必须显式登记（2026-09-18）。
        'engine.kline_scheduler',
        'engine.kline_refresh',
        'server.core',
        'server.core.errors',
        'server.core.time',
        'server.core.logging',
    ]

a = Analysis(
    ['main_webview.py'],
    # 显式把项目根加入模块搜索路径：server/ 是**纯 Python 顶层包**，PyInstaller 只在
    # sys.path 里找它。UI 侧全是函数内延迟 import，静态分析扫不到，必须靠 hiddenimports
    # 兜底；这里补 pathex 保证 hiddenimports 里的 'server.core.*' 能被解析到（否则
    # Analysis 会静默跳过该条目 → 打包出的 EXE 里仍然没有 server 包）。
    pathex=[SPECPATH],
    binaries=[],
    datas=[
        # 前端资源：index.html 通过 file:// 加载，echarts.min.js 需与 index.html 同目录
        ('ui_mockup/index.html', 'ui_mockup'),
        ('ui_mockup/echarts.min.js', 'ui_mockup'),
        # html2canvas 本地化（2026-09-15）：决策卡「保存为图片」原依赖 jsdelivr CDN，
        # 离线/受限网络下静默不可用；本文件必须与 index.html 同目录，否则该功能失效。
        ('ui_mockup/html2canvas.min.js', 'ui_mockup'),
        # 发行配置（空方案模板，不含本机工作配置，合规：去默认+强制自建）
        ('engine/config/quant_model.release.json', 'config/quant_model.json'),
        ('engine/config/sector_map.json', 'config'),
        # ⚠️ 以下种子一律指向 engine/config/seed/（空白模板），绝不打包本机 config/ 下
        #   的用户工作配置——含私有凭据（webhook）、自选列表、用户自定义监控/数据源设置。
        #   用户本机 config/*.json 只服务于开发运行，禁止进包分发。
        ('engine/config/seed/watchlist.txt', 'config/watchlist.txt'),
        # 数据源可配置化：内置三源类别/provider 映射 + 默认腾讯源（空白种子）
        ('engine/config/seed/data_sources.json', 'config/data_sources.json'),
        # L3 监控守护：默认配置（空白种子：全槽位关闭、无策略、900s）
        ('engine/config/seed/monitor.json', 'config/monitor.json'),
        # L4 通知层：渠道配置 + 凭据模板（全空——绝不带用户 webhook/SendKey 进包）
        ('engine/config/seed/notifiers.json', 'config/notifiers.json'),
        ('engine/config/seed/credentials.json', 'config/credentials.json'),
        ('cache/stock_list.json', 'cache'),
        # ⚠️ 不再打包独立回测行情缓存 pkl：回测现直接复用全市场扫描写入的
        #   cache/kline/qfq_daily_{code}.pkl（按代码维度），由扫描/回测自动维护，
        #   免手动更新种子包；首启联网即能回测（offline_mode=False 时）。
        ('favicon.ico', '.'),
        ('docs/使用说明书.md', 'docs'),
        ('docs/USER_AGREEMENT.md', 'docs'),
        ('docs/DISCLAIMER.md', 'docs'),
        ('docs/工具评测_用户视角.md', 'docs'),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'sympy', 'scipy', 'PyQt5', 'PySide6', 'PySide2', 'tkinter', 'tkinter.test', 'test',
        # 排除非 Windows 后端相关依赖
        'gi', 'gtk', 'webkit',
        'webview.platforms.gtk', 'webview.platforms.cocoa',
        'webview.platforms.android', 'webview.platforms.qt', 'webview.platforms.cef',
        # matplotlib 非 tk/agg 后端
        'matplotlib.backends.backend_qtagg',
        'matplotlib.backends.backend_qt5agg',
        'matplotlib.backends.backend_webagg',
        'matplotlib.backends.backend_gtk3agg',
        'matplotlib.backends.backend_gtk4agg',
        'matplotlib.backends.backend_wxagg',
        'matplotlib.backends.backend_nbagg',
        'matplotlib.backends.backend_cairo',
        'matplotlib.backends.backend_pgf',
        'matplotlib.backends.backend_ps',
        'matplotlib.backends.backend_svg',
        'matplotlib.tests',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='M-Bull',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='favicon.ico',
)
