#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据源注册表：读 config/data_sources.json 构建「类别 -> provider 列表」映射。

职责：
- 类别：kline / realtime / stock_list / market_breadth
- 每个类别对应有序 provider 名列表（前者优先，失败回退后者）
- provider 实现名映射到 builtin 模块中的类；可插源（tushare 等）后续加
- 凭据从 config/credentials.json 按 ``credential_key`` 注入（内置源无需）
- config 缺失/损坏时回退到 DEFAULT_CATEGORIES，保证与原行为一致、工具不崩

注：第一刀仅支持内置三源；遇到未知 impl（如 tushare）仅告警跳过，
不阻断其它内置源注册。
"""
import json
import logging
import os

from engine.config import CONFIG_DIR
from engine.data_sources.base import DataSource
from engine.data_sources import builtin as _builtin
from engine.data_sources import tdx as _tdx

logger = logging.getLogger(__name__)

# 类别 -> 默认 provider 顺序（config 缺失时兜底）
# kline：tdx（通达信 pytdx，TCP 7709，无 WAF 配额）优先，tencent 回退。
# realtime：基线顺序；registry.load 会按「一键主源联动」把 A股 kline 主源插到最前。
DEFAULT_CATEGORIES = {
    "kline": ["tdx", "tencent"],
    "realtime": ["sina", "tencent", "tdx"],
    "stock_list": ["sina"],
    "market_breadth": ["eastmoney"],
}

# 一键主源联动的能力矩阵（2026-09-19）：
# 用户在数据源设置里切主源时，各类别跟随主源（该源支持的类别），
# 不支持的类别保持各自默认链。判定依据全部来自实测：
#   tdx       —— kline(日K/分钟/周K自算) ✓ futures(ExHq 主连) ✓ index ✓ realtime(快照) ✓
#   tencent   —— kline ✓ futures ✗（无腾讯期货源） index ✓ realtime ✓
#   sina      —— kline(未复权) ✓ futures ✓ index ✗（IndexFetcher 无新浪源） realtime ✓
#   eastmoney —— kline ✗ futures ✓ index ✓ realtime ✗
_SOURCE_PRIMARY_REALTIME = {"tdx", "tencent", "sina"}
# provider 键 -> 内置实现类
_BUILTIN = {
    "tdx": _tdx.TdxSource,
    "tencent": _builtin.TencentSource,
    "sina": _builtin.SinaSource,
    "eastmoney": _builtin.EastmoneySource,
}
# config 的 impl 字段可用实现类名（如 "TencentSource"）映射到 provider 键，
# 这样 impl 既可是 provider 键（"tencent"）也可是类名（"TencentSource"）。
_IMPL_TO_KEY = {
    "TdxSource": "tdx",
    "TencentSource": "tencent",
    "SinaSource": "sina",
    "EastmoneySource": "eastmoney",
}


def _load_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        # 首次运行未配置数据源/凭据是常态，不需刷 WARNING（沿用内置免费源）
        logger.debug("数据源配置 %s 不存在，使用内置默认", path)
        return None
    except Exception as e:
        logger.warning("数据源配置读取失败 %s: %s，使用内置默认", path, e)
        return None


class DataSourceRegistry:
    def __init__(self, categories, providers):
        # categories: dict[cat] -> list[provider_name]
        self._categories = categories
        # providers: dict[name] -> DataSource instance
        self._providers = providers

    @classmethod
    def load(cls, config_dir=None):
        config_dir = config_dir or CONFIG_DIR
        ds_path = os.path.join(config_dir, 'data_sources.json')
        cred_path = os.path.join(config_dir, 'credentials.json')
        cfg = _load_json(ds_path) or {}
        categories = cfg.get('categories') or dict(DEFAULT_CATEGORIES)
        provider_defs = cfg.get('providers') or {}
        creds = _load_json(cred_path) or {}

        instances = {}
        for pname, pdef in provider_defs.items():
            inst = cls._build_provider(pname, pdef, creds)
            if inst is not None:
                instances[pname] = inst

        # 兜底：确保默认 provider 都存在（config 缺 provider 定义时）
        for pname, impl in _BUILTIN.items():
            if pname not in instances:
                try:
                    instances[pname] = impl()
                except Exception as e:
                    logger.warning("内置 provider %s 实例化失败: %s", pname, e)

        # 归一化类别映射：只保留存在的 provider
        norm = {}
        for cat, plist in categories.items():
            kept = [p for p in plist if p in instances]
            if kept:
                norm[cat] = kept

        # 一键主源联动（realtime）：把 A股 kline 主源插到实时候选最前。
        # 主源支持实时（tdx/tencent/sina）才插；eastmoney 等不支持的不插。
        # 指数联动在 IndexFetcher 内部读 kline_source，期货联动在
        # web_api.save_data_sources 同步写 futures_kline_source —— 三处合力实现
        # 「切一次主源，所有类别跟随」。
        try:
            # 一键主源联动：`kline_source`（标量主源，UI 一键切换写入）驱动两处动态化——
            #   ① categories.kline 首位 = 主源（失败转移顺序跟随主源）
            #   ② realtime 首位 = 主源（能力矩阵：tdx/tencent/sina 支持实时）
            # 注意：kline_source 与 categories.kline 是两套体系（标量开关 vs provider 列表），
            # 联动必须读标量、改列表——只看列表首位会把 DEFAULT 的 'tdx' 误当主源。
            _ks = (cfg or {}).get('kline_source')
            if _ks in _SOURCE_PRIMARY_REALTIME:
                kl = [p for p in (norm.get('kline') or []) if p != _ks]
                norm['kline'] = [_ks] + kl
                rt = [p for p in (norm.get('realtime') or []) if p != _ks]
                norm['realtime'] = [_ks] + rt
        except Exception as e:
            logger.debug("主源联动失败（忽略）: %s", e)

        registry = cls(norm, instances)
        logger.info(
            "已加载数据源配置：%s",
            {c: [instances[p].name for p in ps] for c, ps in norm.items()},
        )
        return registry

    @classmethod
    def _build_provider(cls, pname, pdef, creds):
        impl_name = pdef.get('impl')
        # 第一刀仅支持内置实现；未知 impl（tushare 等可插源）后续接入。
        # impl 字段既可是 provider 键（"tencent"），也可是实现类名（"TencentSource"）。
        pkey = _IMPL_TO_KEY.get(impl_name, impl_name)
        impl_cls = _BUILTIN.get(pkey)
        if impl_cls is None:
            logger.warning("未知数据源实现 %r（%s），跳过（第一刀仅支持内置源）", impl_name, pname)
            return None
        try:
            inst = impl_cls()
            inst.name = pdef.get('display_name', inst.name)
        except Exception as e:
            logger.warning("provider %s 实例化失败: %s", pname, e)
            return None
        cred_key = pdef.get('credential_key')
        if cred_key:
            inst.set_credential(creds.get(cred_key))
        return inst

    def resolve(self, category):
        """返回该类别的有序 provider 实例列表（空列表表示无配置）。"""
        return [self._providers[p] for p in self._categories.get(category, [])]
