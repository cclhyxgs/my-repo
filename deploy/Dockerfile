# ============================================================
# M-Bull H5 —— api 服务镜像（骨架期）
# ============================================================
# 约束落点：
#   §2.13-1 依赖版本锁定 → 只从 requirements-server.txt 安装，不在镜像里解析自由版本
#   §2.13-2 时区固定     → TZ=Asia/Shanghai（容器级）
#   C1#2 规格 S          → api 单副本；R-09 未解决前禁止调大 --workers
# ============================================================
FROM python:3.13-slim

ENV TZ=Asia/Shanghai \
    QUANT_SYSTEM_DIR=/data/m-bull \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MBULL_ENV=production \
    MBULL_ENABLE_INTERNAL_PROBE=0

# §2.13-2：容器时区（先装 tzdata 再建立软链，顺序不可颠倒）
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-server.txt /app/requirements-server.txt
RUN pip install --no-cache-dir -r /app/requirements-server.txt

# engine 为零改动既有代码，整目录复制；config/ 作为首启种子
COPY engine/ /app/engine/
COPY server/ /app/server/
COPY config/ /app/config/

# engine/config.py 导入期会 makedirs(CONFIG_DIR/CACHE_DIR)，须保证挂卷点位存在且可写
RUN mkdir -p /data/m-bull/config /data/m-bull/cache
VOLUME ["/data/m-bull"]

EXPOSE 8000

# R-09：quant_config._cache / _diag_tasks / _scan_tasks / self._market_type 等
# 进程级全局状态全部外置之前，api 只允许单副本 —— 显式 --workers 1，禁止调大。
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
