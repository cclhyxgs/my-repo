"""机器规格自适应探测（零外部依赖，纯标准库实现）。

根据 CPU 逻辑核数 + 物理内存，算出一个**既尽量吃满核、又不会撑爆内存**的安全并行进程数，
在回测/扫描启动时自动应用，用户无需手动填写。

设计原则（严守工具「不依赖网络/第三方包」底线）：
- 仅用标准库；Windows 内存查询走 kernel32.GlobalMemoryStatusEx，
  Linux 读 /proc/meminfo，macOS 用 sysctl（需 subprocess）。
- 不引入网络 / 第三方包；打包后行为完全一致。
"""
import os
import sys


# —— 内存安全旋钮（可按需调整）——
# 给 OS + GUI + 浏览器预留：按总内存自适应。16GB 工作机 1.5GB 足够，8GB 留给 1.2GB。
# 预留太多会把 8~16GB 工作机的可用内存吃干，导致多核被压成 1 进程。
def _adaptive_os_headroom(total_bytes):
    total_gb = total_bytes / 1024 ** 3 if total_bytes > 0 else 16
    if total_gb >= 32:
        return 2.5 * 1024 ** 3
    elif total_gb >= 16:
        return 1.5 * 1024 ** 3
    elif total_gb >= 8:
        return 1.2 * 1024 ** 3
    else:
        return 1.0 * 1024 ** 3
_MEM_OS_HEADROOM = 1.5 * 1024 ** 3   # 默认 1.5GB（auto_n_workers 会按总内存覆盖）
# pickle 载入内存后膨胀倍率：根据可用内存自适应。
# 经验上 pickle 加载后约 1.5~2.5 倍；可用内存吃紧(<4GB)时用更紧的 1.5，
# 富裕(>=8GB)时用更宽的 2.0；空档区间用 1.8。避免 5199 池大缓存被过度估算压成 1 进程。
def _adaptive_expand_mult(avail_bytes):
    avail_gb = avail_bytes / 1024 ** 3
    if avail_gb < 4.0:
        return 1.5
    elif avail_gb < 8.0:
        return 1.8
    return 2.0
_CACHE_EXPAND_MULT = 2.0              # 默认值（auto_n_workers 会按可用内存覆盖）
_PER_PROC_BASE = 384 * 1024 ** 2      # 单进程 Python/numpy 解释器 + 因子计算临时数组基础占用
_PER_PROC_FLOOR = 384 * 1024 ** 2     # 单进程内存估算下限
_RESERVE_CORES = 1                     # 留 1 个核给 OS/GUI 保持响应
_HARD_CAP = 16                         # 进程数硬上限，防止极端情况


def _win_memory():
    """返回 (total_phys_bytes, avail_phys_bytes)，Windows 下用 GlobalMemoryStatusEx。"""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32

        class _MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

            def __init__(self):
                self.dwLength = ctypes.sizeof(self)

        stat = _MEMORYSTATUSEX()
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return int(stat.ullTotalPhys), int(stat.ullAvailPhys)
    except Exception:
        pass
    return 0, 0


def get_total_physical_memory():
    """返回物理内存总字节数；探测失败返回 0。"""
    try:
        if sys.platform == 'win32':
            total, _ = _win_memory()
            return total
        elif sys.platform == 'darwin':
            from subprocess import check_output
            out = check_output(['sysctl', '-n', 'hw.memsize']).decode().strip()
            return int(out)
        else:  # Linux
            with open('/proc/meminfo') as f:
                for line in f:
                    if line.startswith('MemTotal:'):
                        return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 0


def get_available_physical_memory():
    """返回当前可用物理内存字节数；尽量用 OS 接口，失败回退到总内存 50%。"""
    try:
        if sys.platform == 'win32':
            _, avail = _win_memory()
            return avail
        elif sys.platform == 'darwin':
            from subprocess import check_output
            total = int(check_output(['sysctl', '-n', 'hw.memsize']).decode().strip())
            return total // 2
        else:  # Linux
            with open('/proc/meminfo') as f:
                for line in f:
                    if line.startswith('MemAvailable:'):
                        return int(line.split()[1]) * 1024
    except Exception:
        pass
    return get_total_physical_memory() // 2


def _default_cache_path():
    """回测统一前复权缓存目录 cache/kline（按代码维度 qfq_daily_{code}.pkl）。

    旧版单一 244MB 的 backtest_data_cache_qfq.pkl 已废弃；现回测与全市场扫描
    共用 cache/kline/ 下的按代码 pkl，故内存估算改为对该目录内 qfq_daily_*.pkl 求总大小。
    """
    try:
        from engine.config import get_app_dir
        return os.path.join(get_app_dir(), 'cache', 'kline')
    except Exception:
        return os.path.join('cache', 'kline')


def estimate_cache_per_process_bytes(cache_path=None):
    """估算每个 worker 进程载入离线大盘缓存后的内存占用（字节）。
    
    每个 spawn worker 进程需独立把离线缓存 pickle 载入自己的内存，
    主进程也会载一份。pickle 载入内存后体积通常膨胀到文件 1.5~2.5 倍，
    再加 Python/numpy 解释器与因子计算临时数组的基础占用。
    膨胀倍率根据当前可用内存自适应：吃紧时用 1.5，富裕时用 2.0。
    """
    path = cache_path or _default_cache_path()
    file_bytes = 0
    try:
        if path and os.path.exists(path):
            if os.path.isdir(path):
                # 目录：累加回测实际使用的 qfq_daily_{code}.pkl（忽略旧的 kline_{md5}.pkl）
                for fn in os.listdir(path):
                    if fn.startswith('qfq_daily_') and fn.endswith('.pkl'):
                        fp = os.path.join(path, fn)
                        if os.path.isfile(fp):
                            file_bytes += os.path.getsize(fp)
            elif os.path.isfile(path):
                file_bytes = os.path.getsize(path)
    except Exception:
        file_bytes = 0
    if file_bytes <= 0:
        # 缓存文件不存在/未知：用保守默认 1.2GB
        return int(1.2 * 1024 ** 3)
    # 按当前可用内存动态选膨胀倍率
    avail = get_available_physical_memory()
    if avail <= 0:
        avail = get_total_physical_memory() // 2
    mult = _adaptive_expand_mult(avail)
    est = int(file_bytes * mult) + _PER_PROC_BASE
    return max(est, _PER_PROC_FLOOR)


def auto_n_workers(cache_path=None, reserve_cores=_RESERVE_CORES,
                   os_headroom=_MEM_OS_HEADROOM, hard_cap=_HARD_CAP):
    """根据机型自动算安全并行进程数。

    规则：
      - 核数上限：逻辑核数 - reserve_cores（留 1 核给 OS/GUI 保持响应）
      - 内存上限：可用内存 - os_headroom 后，除以「每进程内存估算」（并先扣掉主进程那 1 份）
      - 取两者较小者，夹在 [1, min(逻辑核数, hard_cap)] 之间
      - 内存探测失败时**不**按内存限制（只受核数约束），避免误判把并行压成 1

    显式传入 n_workers 时不应调用本函数（调用方负责尊重显式值）。
    """
    cores = max(1, os.cpu_count() or 1)
    core_limit = max(1, cores - reserve_cores)

    total_mem = get_total_physical_memory()
    if total_mem <= 0:
        # 内存探测失败 → 仅按核数，不限制
        n = core_limit
    else:
        avail_mem = get_available_physical_memory()
        if avail_mem <= 0:
            avail_mem = total_mem // 2
        per_proc = estimate_cache_per_process_bytes(cache_path)
        # OS 预留按总内存自适应：16GB 机器只预留 1.5GB，8GB 留 1.2GB，避免 8/16GB 工作机被压成 1 进程
        headroom = _adaptive_os_headroom(total_mem)
        # 主进程也要载一份缓存，所以可用内存先扣掉 1 份 per_proc 再算 worker 数
        usable = avail_mem - headroom - per_proc
        if usable <= 0:
            mem_limit = 1
        else:
            mem_limit = max(1, int(usable // per_proc))
        n = min(core_limit, mem_limit)

    n = max(1, min(n, cores, hard_cap))
    return n


def describe_machine(cache_path=None):
    """返回人类可读的机型探测描述，用于回测/扫描开场日志。"""
    cores = max(1, os.cpu_count() or 1)
    total_mem = get_total_physical_memory()
    avail_mem = get_available_physical_memory()
    if avail_mem <= 0 and total_mem > 0:
        avail_mem = total_mem // 2
    per_proc = estimate_cache_per_process_bytes(cache_path)
    n = auto_n_workers(cache_path=cache_path)

    def gb(b):
        return f"{b / 1024 ** 3:.1f}GB"

    if total_mem <= 0:
        mem_str = "内存探测失败(不限制)"
        total_str = "未知"
        avail_str = "未知"
    else:
        total_str = gb(total_mem)
        avail_str = gb(avail_mem)

    headroom = _adaptive_os_headroom(total_mem) if total_mem > 0 else _MEM_OS_HEADROOM
    return (f"机型探测: {cores}核 | 物理内存 {total_str} | 可用 {avail_str} "
            f"| 每工作单元估算 {gb(per_proc)} | 自动并行 {n} 工作单元"
            f"{((' | 提示:可用内存偏少,关闭其他大内存进程可提升并行度' if n == 1 and avail_mem > 0 and avail_mem < headroom * 2 else ''))}")
