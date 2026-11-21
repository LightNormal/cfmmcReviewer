"""
品种筛选功能 · 后端 API
=======================
以 Flask Blueprint 形式挂载，不侵入原有复盘服务。

接口一览
--------
  GET    /api/screener/meta            筛选器元数据（品种池、交易所、参数预设、默认权重）
  GET    /api/screener                 执行筛选
  GET    /api/screener/detail/<code>   单品种详情（完整指标快照 + 图表序列）
  POST   /api/screener/refresh         强制刷新行情缓存

  GET    /api/watchlist                读取自选列表
  POST   /api/watchlist                加入自选        body: {code, note?}
  DELETE /api/watchlist/<code>         移出自选

设计说明
--------
* 行情走 `market_data.KlineStore`：进程内存 + 磁盘两级缓存，默认 TTL 10 分钟。
* 筛选结果按 (参数签名) 做短路缓存，重复查询同一组参数无需重算。
* 自选列表持久化到 `watchlist.json`（已 gitignore），写入用「临时文件 + 原子替换」，
  并用线程锁串行化，避免并发请求写坏文件。
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime

from flask import Blueprint, jsonify, request

import indicators
import market_data
import screener
from market_data import EXCHANGE_NAMES, all_symbols, get_store, get_symbol

screener_bp = Blueprint('screener', __name__)

WATCHLIST_FILE = os.environ.get('SCREENER_WATCHLIST', 'watchlist.json')
_RESULT_CACHE_TTL = int(os.environ.get('SCREENER_RESULT_TTL', '60'))

_watchlist_lock = threading.Lock()
_result_cache: dict = {}
_result_cache_lock = threading.Lock()


# ============================================================
# 工具
# ============================================================

def _json_safe(obj):
    """递归清洗 NaN/Infinity，保证前端 JSON.parse 不会炸。"""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, (int, str, bool)) or obj is None:
        return obj
    # numpy 标量 / pandas Timestamp 等
    try:
        f = float(obj)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return str(obj)


def _signature(config: screener.ScreenerConfig) -> str:
    """把请求参数压成缓存键。"""
    payload = {
        'p': config.proximity, 'l': config.lookback,
        'rs': config.ref_start, 're': config.ref_end,
        'ms': config.min_score, 'd': config.direction,
        'ex': config.exchanges, 'sy': config.symbols,
        'rl': config.require_level,
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


# ============================================================
# 自选列表持久化
# ============================================================

def _read_watchlist() -> list[dict]:
    if not os.path.exists(WATCHLIST_FILE):
        return []
    try:
        with open(WATCHLIST_FILE, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    items = data.get('items') if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    cleaned = []
    for it in items:
        if isinstance(it, str):
            cleaned.append({'code': it.upper(), 'added_at': '', 'note': ''})
        elif isinstance(it, dict) and it.get('code'):
            code = str(it['code']).upper()
            meta = get_symbol(code)
            if meta is None:
                continue
            cleaned.append({
                'code': code,
                'name': meta.name,
                'exchange': meta.exchange,
                'added_at': it.get('added_at', ''),
                'note': it.get('note', ''),
            })
    return cleaned


def _write_watchlist(items: list[dict]) -> None:
    tmp = WATCHLIST_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump({'updated_at': datetime.now().isoformat(timespec='seconds'),
                   'items': items}, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, WATCHLIST_FILE)


# ============================================================
# GET /api/screener/meta
# ============================================================

@screener_bp.route('/api/screener/meta', methods=['GET'])
def api_screener_meta():
    """筛选器的静态元数据，前端初始化时调用一次。"""
    symbols = []
    for s in all_symbols():
        symbols.append({
            'code': s.code,
            'name': s.name,
            'exchange': s.exchange,
            'exchange_name': EXCHANGE_NAMES.get(s.exchange, s.exchange),
        })

    exchanges = [{'code': code, 'name': name}
                 for code, name in EXCHANGE_NAMES.items()
                 if any(s['exchange'] == code for s in symbols)]

    return jsonify(_json_safe({
        'symbols': symbols,
        'exchanges': exchanges,
        'lookback_presets': indicators.LOOKBACK_PRESETS,
        'defaults': {
            'proximity': 5.0,
            'lookback': 250,
            'min_score': 0,
            'direction': 'all',
            'require_level': True,
        },
        'weights': screener.DEFAULT_WEIGHTS,
        'cache_ttl': market_data.CACHE_TTL,
        'data_note': '数据源：新浪财经期货主力连续合约日线 + 实时快照（免费公开接口）。'
                     '主力连续合约在换月时存在跳空，超长期高低点可能受换月缺口影响。',
    }))


# ============================================================
# GET /api/screener
# ============================================================

@screener_bp.route('/api/screener', methods=['GET'])
def api_screener_run():
    """执行筛选。

    Query:
      proximity     距关键点位阈值（%），默认 5
      lookback      关键点位回看交易日数，默认 250；0 = 全部历史
      ref_start     参考区间模式起始日（YYYY-MM-DD），可选
      ref_end       参考区间模式结束日（YYYY-MM-DD），可选
      min_score     最低评分，默认 0
      direction     all / long / short
      exchanges     逗号分隔，如 SHFE,DCE
      symbols       逗号分隔，只筛这些品种
      require_level 1/0，是否强制「在阈值内」，默认 1
      refresh       1 = 强制刷新行情缓存
    """
    try:
        config = screener.ScreenerConfig.from_query(request.args)
    except Exception as exc:
        return jsonify({'error': f'筛选参数错误: {exc}'}), 400

    force = str(request.args.get('refresh', '')).lower() in ('1', 'true', 'yes')
    sig = _signature(config) + f'|force={force}'

    if not force:
        with _result_cache_lock:
            cached = _result_cache.get(sig)
        if cached and (time.time() - cached['ts']) < _RESULT_CACHE_TTL:
            payload = dict(cached['payload'])
            payload['cached'] = True
            return jsonify(_json_safe(payload))

    store = get_store()
    if force:
        store.clear_memory()

    try:
        t0 = time.time()
        result = screener.screen(config, store=store)
        result['elapsed_ms'] = int((time.time() - t0) * 1000)
    except Exception as exc:
        return jsonify({'error': f'筛选失败: {exc}'}), 500

    result['cached'] = False
    result['generated_at'] = datetime.now().isoformat(timespec='seconds')

    with _result_cache_lock:
        # 简单容量控制，避免无限增长
        if len(_result_cache) > 32:
            _result_cache.clear()
        _result_cache[sig] = {'ts': time.time(), 'payload': result}

    return jsonify(_json_safe(result))


# ============================================================
# GET /api/screener/detail/<code>
# ============================================================

@screener_bp.route('/api/screener/detail/<code>', methods=['GET'])
def api_screener_detail(code: str):
    """单品种完整分析：指标快照 + K 线/指标序列（供 ECharts 画图）。"""
    meta = get_symbol(code)
    if meta is None:
        return jsonify({'error': f'未知品种: {code}'}), 404

    try:
        lookback = int(float(request.args.get('lookback', 250) or 250))
    except (TypeError, ValueError):
        lookback = 250
    ref_start = (request.args.get('ref_start') or '').strip() or None
    ref_end = (request.args.get('ref_end') or '').strip() or None

    store = get_store()
    if str(request.args.get('refresh', '')).lower() in ('1', 'true', 'yes'):
        store.clear_memory()

    kline = store.load_one(meta.code)
    if kline is None:
        return jsonify({'error': f'{meta.code} 行情获取失败（网络或数据源异常）'}), 502

    params = indicators.IndicatorParams(
        lookback=lookback, ref_start=ref_start, ref_end=ref_end)
    analysis = indicators.analyze(kline, params)

    # 复用筛选打分，方便前端展示「为什么被选中」
    config = screener.ScreenerConfig(
        proximity=5.0, lookback=lookback,
        ref_start=ref_start, ref_end=ref_end, require_level=False)
    try:
        evaluated = screener.evaluate(meta.code, kline, config)
    except Exception:
        evaluated = None

    return jsonify(_json_safe({
        'code': meta.code,
        'name': meta.name,
        'exchange': meta.exchange,
        'exchange_name': EXCHANGE_NAMES.get(meta.exchange, meta.exchange),
        'analysis': analysis,
        'screen_snapshot': evaluated,
    }))


# ============================================================
# POST /api/screener/refresh
# ============================================================

@screener_bp.route('/api/screener/refresh', methods=['POST'])
def api_screener_refresh():
    """强制刷新行情缓存（清空内存缓存 + 结果缓存）。"""
    get_store().clear_memory()
    with _result_cache_lock:
        _result_cache.clear()
    return jsonify({'success': True, 'message': '行情缓存已清空，下次查询将重新拉取'})


# ============================================================
# 自选列表
# ============================================================

@screener_bp.route('/api/watchlist', methods=['GET'])
def api_watchlist_get():
    with _watchlist_lock:
        items = _read_watchlist()
    return jsonify({'items': items, 'count': len(items)})


@screener_bp.route('/api/watchlist', methods=['POST'])
def api_watchlist_add():
    body = request.get_json(silent=True) or {}
    code = str(body.get('code') or '').strip().upper()
    if not code:
        return jsonify({'error': '缺少 code 参数'}), 400

    meta = get_symbol(code)
    if meta is None:
        return jsonify({'error': f'未知品种: {code}'}), 400

    with _watchlist_lock:
        items = _read_watchlist()
        existing = next((i for i in items if i['code'] == code), None)
        if existing:
            return jsonify({'success': True, 'already': True,
                            'message': f'{meta.name} 已在自选', 'items': items})
        items.append({
            'code': code,
            'name': meta.name,
            'exchange': meta.exchange,
            'added_at': datetime.now().isoformat(timespec='seconds'),
            'note': str(body.get('note') or '')[:200],
        })
        _write_watchlist(items)

    return jsonify({'success': True, 'message': f'{meta.name} 已加入自选', 'items': items})


@screener_bp.route('/api/watchlist/<code>', methods=['DELETE'])
def api_watchlist_remove(code: str):
    code = code.strip().upper()
    with _watchlist_lock:
        items = _read_watchlist()
        remaining = [i for i in items if i['code'] != code]
        if len(remaining) == len(items):
            return jsonify({'error': f'{code} 不在自选中'}), 404
        _write_watchlist(remaining)
    return jsonify({'success': True, 'message': f'{code} 已移出自选', 'items': remaining})
