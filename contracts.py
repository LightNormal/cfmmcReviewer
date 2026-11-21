# -*- coding: utf-8 -*-
"""主力连续合约的换月复权

为什么必须做
------------
新浪的 ``CU0`` 这类「主力连续」是拼接出来的：主力合约换月那天，价格会从旧合约
的价直接跳到新合约的价。这个跳空**不是真实行情**，却会原样进入日收益序列。

实测 15 个品种（2024-10 ~ 2026-11，517 个交易日）::

    品种    |跳空|均值   |跳空|最大   正常|日收益|   跳空/正常
    FG        7.74%      15.56%        1.41%        5.51x
    JM        6.87%      13.85%        1.84%        3.74x
    M         3.54%       7.10%        0.70%        5.06x
    平均         —           —            —          2.06x

也就是说：**换月跳空的幅度平均是正常日波动的 2 倍**，对部分品种是 5 倍以上。
后果是

  * 假动量：一次 +7% 的换月跳空会被当成真实的 7% 上涨
  * 假突破：可能凭空突破"前高"，触发错误的突破信号
  * 指标失真：ATR / 布林带 / 均线全部被这些台阶污染

做法：不猜跳空，用真实持仓量还原
--------------------------------
不靠"检测异常大跳空"这种启发式（会误判真实暴涨暴跌），而是：

  1. 逐个拉取**单个合约**的日线（新浪支持 ``CU2601`` 这种代码，含持仓量 ``p``）
  2. 每个交易日取**持仓量最大**的合约为当日主力
  3. 记录换月日与两个合约的价差，按**比例**做后复权（ratio back-adjustment）
     换月日比例 ``r = P_新 / P_旧``，把所有更早的价格乘以 ``r`` 的累计值

这样合成序列在换月处连续，日收益不再包含人为台阶，并且**最新价格保持真实**
（后复权），可以直接与当前行情对照。

注意：成交量与持仓量不做复权（它们是量，不是价）。
"""
import glob
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache', 'contracts')

# 注意：URL 里含 `%20`，绝不能用 `%` 格式化拼接（会被当成格式符），
# 必须用字符串连接，否则请求直接失败且异常会被下面的 except 吞掉。
_SINA_URL_BASE = ('https://stock2.finance.sina.com.cn/futures/api/jsonp.php/'
                  'var%20_x=/InnerFuturesNewService.getDailyKLine?symbol=')
_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'),
    'Referer': 'https://finance.sina.com.cn',
}
_TIMEOUT = 20
_MAX_WORKERS = 10

_lock = threading.RLock()


# ============================================================
# 拉取单个合约（带磁盘缓存）
# ============================================================

def _cache_path(contract: str) -> str:
    return os.path.join(CACHE_DIR, contract + '.json')


def _read_cache(contract: str):
    """返回 (命中, 数据)。未命中返回 (False, None)；命中但为空返回 (True, None)。"""
    p = _cache_path(contract)
    if not os.path.exists(p):
        return False, None
    try:
        with open(p, encoding='utf-8') as f:
            return True, json.load(f)
    except Exception:
        return False, None


def _write_cache(contract: str, rows):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = _cache_path(contract) + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(rows, f, ensure_ascii=False)
        os.replace(tmp, _cache_path(contract))
    except Exception:
        pass


def fetch_contract(contract: str, force: bool = False):
    """拉单个合约的日线，返回 [{d,o,h,l,c,v,p}, ...]；无数据返回 None。"""
    if not force:
        hit, rows = _read_cache(contract)
        if hit:
            return rows

    rows = None
    try:
        r = requests.get(_SINA_URL_BASE + contract, headers=_HEADERS, timeout=_TIMEOUT)
        m = re.search(r'=\((\[.*\])\)', r.text, re.S)
        if m:
            arr = json.loads(m.group(1))
            rows = arr if arr else None
    except Exception:
        return None                       # 网络错误：不缓存，下次重试

    _write_cache(contract, rows)      # 空结果也缓存，避免反复请求不存在的合约
    return rows


def candidate_contracts(code: str, start: tuple, end: tuple) -> list:
    """生成候选合约代码。start/end 形如 (2024, 9)。

    A 股期货各交易所（含郑商所）在新浪统一用 4 位 YYMM，实测 ``TA2601`` 有效、
    ``TA601`` 无效，所以这里一律用 4 位。
    """
    out = []
    y, m = start
    while (y, m) <= end:
        out.append('%s%02d%02d' % (code, y % 100, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


# ============================================================
# 还原每日主力
# ============================================================

def _load_all_contracts(code, months, force=False, progress=False):
    """并发拉取候选合约，返回 {contract: rows}。"""
    data = {}
    todo = []
    for c in months:
        hit, rows = (False, None) if force else _read_cache(c)
        if hit:
            if rows:
                data[c] = rows
        else:
            todo.append(c)

    if todo:
        with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(todo))) as pool:
            for c, rows in zip(todo, pool.map(lambda x: fetch_contract(x, force), todo)):
                if rows:
                    data[c] = rows
        if progress:
            print('    拉取 %d 个新合约（缓存命中 %d）' % (len(todo), len(data) - len(todo)))
    return data


def build_main_series(code, start='2024-10-01', end=None, force=False, progress=False):
    """用持仓量还原历史上每天的主力合约，并合成连续序列。

    返回 dict::

        {
          'dates': [...], 'open': [...], 'high': [...], 'low': [...],
          'close': [...], 'volume': [...], 'oi': [...],
          'contract': [...],               # 每天的主力合约代码
          'rolls': [ {date, old, new, old_close, new_close, gap_pct, index}, ... ],
        }

    ``close`` 是**未复权**的主力原始价格（换月处有台阶）。
    """
    if not end:
        import datetime
        end = datetime.date.today().isoformat()

    m0 = (int(start[:4]), int(start[5:7]) - 2)          # 往前多要 2 个月，保证第一个
    if m0[1] < 1:                                        # 交易日就有合约在交易
        m0 = (m0[0] - 1, 12 + m0[1])
    m1 = (int(end[:4]), int(end[5:7]) + 3)               # 往后多要 3 个月
    if m1[1] > 12:
        m1 = (m1[0] + 1, m1[1] - 12)

    months = candidate_contracts(code, m0, m1)
    contracts = _load_all_contracts(code, months, force=force, progress=progress)
    if not contracts:
        return None

    # 一次性建索引：contract -> {date: row}，避免逐日线性扫描
    bars = {}
    for name, rows in contracts.items():
        bars[name] = {row['d']: row for row in rows if 'd' in row}

    # date -> {contract: (close, oi)}，只保留窗口内
    by_date = {}
    for name, idx in bars.items():
        for d, row in idx.items():
            if start and d < start:
                continue
            if end and d > end:
                continue
            try:
                by_date.setdefault(d, {})[name] = (float(row['c']),
                                                   float(row.get('p') or 0))
            except Exception:
                continue

    dates = sorted(by_date)
    if not dates:
        return None

    # 逐日选主力：持仓量最大；换月需要新合约明显超过旧合约（5% 缓冲，避免来回切换）
    cur = None
    picks, rolls = [], []
    for d in dates:
        live = {c: v for c, v in by_date[d].items() if v[1] > 0}
        if not live:
            live = dict(by_date[d])          # 持仓量全为 0（如已交割），退回全部
        if not live:
            continue
        best = max(live, key=lambda c: live[c][1])
        if cur is None:
            cur = best
        elif best != cur:
            cur_oi = live.get(cur, (0.0, 0.0))[1]
            if cur not in live or live[best][1] > cur_oi * 1.05:
                old_close = by_date[d].get(cur, (None,))[0]
                new_close = live[best][0]
                if old_close:
                    rolls.append({
                        'date': d, 'old': cur, 'new': best,
                        'old_close': old_close, 'new_close': new_close,
                        'gap_pct': (new_close - old_close) / old_close * 100,
                        'index': len(picks),
                    })
                    cur = best
        picks.append((d, cur))

    # 取每个主力合约当日完整的 OHLCV
    out = {k: [] for k in ('dates', 'open', 'high', 'low', 'close',
                           'volume', 'oi', 'contract')}
    for d, c in picks:
        row = bars.get(c, {}).get(d)
        if row is None:
            continue
        try:
            o, h, l, cl = (float(row['o']), float(row['h']),
                           float(row['l']), float(row['c']))
        except Exception:
            continue
        # 价格必须为正：偶尔有个别合约返回 0 价（数据毛刺），
        # 留着会让收益率除零，把整条序列和 IC 计算污染掉。
        if not (o > 0 and h > 0 and l > 0 and cl > 0):
            continue
        out['dates'].append(d)
        out['open'].append(o)
        out['high'].append(h)
        out['low'].append(l)
        out['close'].append(cl)
        out['volume'].append(float(row.get('v') or 0))
        out['oi'].append(float(row.get('p') or 0))
        out['contract'].append(c)

    # 因子下标基于 picks；若上面跳过了个别行，日期→下标需要重新对齐
    idx_map = {d: i for i, d in enumerate(out['dates'])}
    for r in rolls:
        r['index'] = idx_map.get(r['date'], -1)
    out['rolls'] = [r for r in rolls if 0 <= r['index'] < len(out['dates'])]
    return out if out['dates'] else None


# ============================================================
# 后复权
# ============================================================

def adjust_factors(n, rolls):
    """算出每根 K 线应乘的复权因子。

    对第 j 根 K 线，因子 = Π(所有发生在 j 之后的换月比例)。
    最新一根因子恒为 1（后复权：最新价格保持真实）。
    """
    f = [1.0] * n
    for r in reversed(rolls):
        i = r['index']
        if i <= 0 or i >= n:
            continue
        ratio = r['new_close'] / r['old_close'] if r['old_close'] else 1.0
        if not (ratio > 0) or not (0.5 < ratio < 2.0):
            # 比例离谱（多半是数据错或临近交割的极端贴水），跳过以免毁掉整条序列
            r['applied'] = False
            continue
        r['applied'] = True
        for j in range(i):
            f[j] *= ratio
    return f


def build_adjusted_kline(code, start='2024-10-01', end=None, force=False, progress=False):
    """返回换月复权后的 Kline（可直接喂给指标与筛选器）。"""
    from market_data import Kline

    series = build_main_series(code, start, end, force=force, progress=progress)
    if not series:
        return None

    f = adjust_factors(len(series['dates']), series['rolls'])
    k = Kline(
        code=code,
        dates=list(series['dates']),
        open=[p * x for p, x in zip(series['open'], f)],
        high=[p * x for p, x in zip(series['high'], f)],
        low=[p * x for p, x in zip(series['low'], f)],
        close=[p * x for p, x in zip(series['close'], f)],
        volume=list(series['volume']),
        source='sina-adjusted',
    )
    k.rolls = series['rolls']          # 附加信息，方便排查
    return k


def roll_report(code, start='2024-10-01', end=None):
    """换月明细（用于人工核对）。"""
    series = build_main_series(code, start, end)
    if not series:
        return None
    f = adjust_factors(len(series['dates']), series['rolls'])
    return {
        'code': code,
        'days': len(series['dates']),
        'rolls': series['rolls'],
        'factors': f,
    }


def clear_cache():
    for p in glob.glob(os.path.join(CACHE_DIR, '*.json')):
        try:
            os.remove(p)
        except OSError:
            pass
