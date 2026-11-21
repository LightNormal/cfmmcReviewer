# -*- coding: utf-8 -*-
"""品种筛选功能离线测试（不联网、不依赖真实行情）。

覆盖：
  1. 技术指标计算口径（MA / MACD / BOLL / RSI / ATR / 量比）
  2. 关键点位「不含当日」这一核心语义（含正向与反向断言）
  3. 实时快照合并的三种情形（覆盖 / 追加 / 忽略）
  4. 筛选打分与各维度过滤（阈值、方向、最低分、交易所、指定品种）
  5. REST API 契约（/api/screener、/detail、/watchlist 增删查）

用 FakeProvider + 合成 K 线，避免测试依赖第三方行情接口。
用法：python tests/test_screener.py
"""
import os
import sys
import time
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

import indicators
import market_data
import screener
from market_data import Kline, SymbolMeta, apply_realtime


# ============================================================
# 测试夹具
# ============================================================

def make_kline(code='TEST', closes=None, highs=None, lows=None, volumes=None,
               start='2025-01-01'):
    """用收盘价序列构造 Kline；未给定 high/low 时按 ±1% 生成。"""
    closes = [float(x) for x in closes]
    n = len(closes)
    highs = [float(x) for x in highs] if highs is not None else [c * 1.01 for c in closes]
    lows = [float(x) for x in lows] if lows is not None else [c * 0.99 for c in closes]
    volumes = [float(x) for x in volumes] if volumes is not None else [1000.0] * n
    start_dt = datetime.date.fromisoformat(start)
    dates = [(start_dt + datetime.timedelta(days=i)).isoformat() for i in range(n)]
    return Kline(code=code, dates=dates, open=list(closes), high=highs,
                 low=lows, close=closes, volume=volumes,
                 # 与真实 provider 一致：fetched_at 决定缓存新鲜度，
                 # 若给固定旧时间戳，KlineStore 会永远判定缓存过期而重复拉取
                 fetched_at=time.time())


class FakeProvider:
    """按品种返回预置 Kline，记录调用次数。"""
    name = 'fake'

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = 0

    def fetch(self, meta):
        self.calls += 1
        if meta.code not in self.mapping:
            raise RuntimeError('no data')
        return self.mapping[meta.code]


def trend_series(n=260, start=50.0, step=0.25):
    """稳步上涨序列。"""
    return [start + step * i for i in range(n)]


def make_store(mapping, tmpdir):
    return market_data.KlineStore(
        provider=FakeProvider(mapping), cache_dir=tmpdir, ttl=9999,
        realtime_provider=_NullRealtime())


class _NullRealtime:
    name = 'null'

    def fetch_many(self, codes):
        return {}


# ============================================================
# 1. 指标口径
# ============================================================

def test_indicators():
    # 90 根，保证 MA60 可计算（否则均线排列会返回「数据不足」）
    closes = [10.0 + 0.5 * i for i in range(90)]
    k = make_kline(closes=closes)
    r = indicators.analyze(k, indicators.IndicatorParams(lookback=10))
    latest = r['latest']

    # MA5 = 最近 5 个收盘均值
    assert abs(latest['ma5'] - np.mean(closes[-5:])) < 1e-6, latest['ma5']
    assert abs(latest['ma20'] - np.mean(closes[-20:])) < 1e-6

    # BOLL 中轨 == MA20
    assert abs(latest['boll_mid'] - latest['ma20']) < 1e-9

    # 注意：analyze() 的输出统一四舍五入到 4 位小数，因此比较容差取 1e-3
    # BOLL 上下轨 = 中轨 ± 2 倍总体标准差(ddof=0)
    std = np.std(closes[-20:], ddof=0)
    assert abs(latest['boll_upper'] - (latest['ma20'] + 2 * std)) < 1e-3, latest['boll_upper']
    assert abs(latest['boll_lower'] - (latest['ma20'] - 2 * std)) < 1e-3

    # 单调上涨：RSI 应为 100（无下跌）
    assert abs(latest['rsi'] - 100.0) < 1e-6, latest['rsi']

    # 均线排列：单调上涨必然是严格多头
    assert latest['ma_alignment'] == '多头排列', latest['ma_alignment']
    print('  ✓ MA / BOLL / RSI / 均线排列 口径正确')

    # 单调下跌 → 空头排列，RSI 为 0
    down = list(reversed(closes))
    r2 = indicators.analyze(make_kline(closes=down),
                            indicators.IndicatorParams(lookback=10))
    assert r2['latest']['ma_alignment'] == '空头排列', r2['latest']['ma_alignment']
    assert abs(r2['latest']['rsi'] - 0.0) < 1e-6, r2['latest']['rsi']
    print('  ✓ 下跌序列 → 空头排列 / RSI=0')

    # MACD：DIF = EMA12 - EMA26，DEA = EMA9(DIF)，柱 = 2*(DIF-DEA)
    s = pd.Series(closes, dtype=float)
    e12 = s.ewm(span=12, adjust=False, min_periods=12).mean()
    e26 = s.ewm(span=26, adjust=False, min_periods=26).mean()
    dif = e12 - e26
    dea = dif.ewm(span=9, adjust=False, min_periods=9).mean()
    assert abs(latest['dif'] - dif.iloc[-1]) < 1e-3, (latest['dif'], dif.iloc[-1])
    assert abs(latest['dea'] - dea.iloc[-1]) < 1e-3
    assert abs(latest['macd'] - 2 * (dif.iloc[-1] - dea.iloc[-1])) < 1e-2
    print('  ✓ MACD 三线定义与递推一致')

    # 量比：当日量 / 前 20 日均量（不含当日）
    vols = [100.0] * 25 + [300.0]
    r3 = indicators.analyze(make_kline(closes=[10.0] * 26, volumes=vols),
                            indicators.IndicatorParams())
    assert abs(r3['latest']['vol_ratio'] - 3.0) < 1e-6, r3['latest']['vol_ratio']
    print('  ✓ 量比 = 当日量 / 前 20 日均量 = 3.0')

    # ATR 为正且合理
    assert latest['atr'] is not None and latest['atr'] > 0
    print('  ✓ ATR 计算正常')


# ============================================================
# 2. 关键点位：必须不含当日
# ============================================================

def test_key_levels():
    n = 120
    closes = [50.0] * (n - 1) + [60.0]           # 最后一根突然拉高
    highs = [51.0] * (n - 1) + [61.0]            # 当日最高 61 —— 全场最高
    lows = [49.0] * (n - 1) + [48.0]             # 当日最低 48 —— 全场最低
    k = make_kline(closes=closes, highs=highs, lows=lows)

    levels = indicators.key_levels(k, indicators.IndicatorParams(lookback=100))
    assert levels['high'] == 51.0, f"关键高点必须剔除当日, 实际 {levels['high']}"
    assert levels['low'] == 49.0, f"关键低点必须剔除当日, 实际 {levels['low']}"
    print('  ✓ 关键高低点剔除当日（当日 61/48 未污染 51/49）')

    # 距离计算：close=60 距前高 51 → -15%（已突破）
    latest = indicators.analyze(k, indicators.IndicatorParams(lookback=100))['latest']
    assert latest['dist_to_high_pct'] < 0, latest['dist_to_high_pct']
    assert abs(latest['dist_to_high_pct'] - (51.0 - 60.0) / 60.0 * 100) < 1e-6
    assert latest['above_high'] is True

    # 回看窗口实际生效：lookback=10 与 100 结果不同
    lv10 = indicators.key_levels(k, indicators.IndicatorParams(lookback=10))
    assert lv10['window_bars'] == 10, lv10['window_bars']
    assert lv10['label'] == '前 10 个交易日'

    # 参考区间模式：历史区间要**完整**使用（含区间末日）
    # 早期实现无条件剔除区间最后一根，导致指定「2026年6月」时丢掉 6-30，
    # 高点/低点可能算偏。现在只在区间延伸到最后一根（未收盘的当日）时才剔除。
    k2 = make_kline(closes=[10.0 + i for i in range(40)])
    lv_range = indicators.key_levels(k2, indicators.IndicatorParams(
        ref_start=k2.dates[5], ref_end=k2.dates[15]))
    assert lv_range['mode'] == 'range', lv_range['mode']
    # 区间内 lows = closes*0.99, 最小在最早一天
    assert lv_range['low_date'] == k2.dates[5], lv_range['low_date']
    # 历史区间（未触及末端）→ 含 dates[15]
    assert lv_range['high_date'] == k2.dates[15], lv_range['high_date']
    assert lv_range['window_bars'] == 11, lv_range['window_bars']
    print('  ✓ 历史参考区间完整包含末日（不再误剔除）')

    # 区间延伸到最后一根 → 剔除未收盘的当日（与回看口径一致）
    lv_tail = indicators.key_levels(k2, indicators.IndicatorParams(
        ref_start=k2.dates[30], ref_end=k2.dates[39]))
    assert lv_tail['high_date'] == k2.dates[38], lv_tail['high_date']
    assert lv_tail['window_bars'] == 9, lv_tail['window_bars']
    print('  ✓ 区间延伸到最后一根时，剔除未收盘的当日')

    # 回看交易日数可以是任意值（不限于预设）
    for bars in (37, 137, 251):
        lv = indicators.key_levels(k2, indicators.IndicatorParams(lookback=bars))
        expect = min(bars, len(k2) - 1)
        assert lv['window_bars'] == expect, (bars, lv['window_bars'], expect)
    assert indicators.key_levels(k2, indicators.IndicatorParams(lookback=0))['window_bars'] == 39
    print('  ✓ 回看交易日数支持任意值（0 = 全部历史）')

    # 数据不足时返回 None 而不是崩
    tiny = make_kline(closes=[10.0])
    lv_tiny = indicators.key_levels(tiny, indicators.IndicatorParams(lookback=250))
    assert lv_tiny['high'] is None
    print('  ✓ 数据不足时安全降级')


# ============================================================
# 3. 实时快照合并
# ============================================================

def test_realtime_merge():
    k = make_kline(closes=[10.0, 11.0, 12.0])
    last_date = k.dates[-1]

    # (a) 同一天 → 覆盖最后一根
    apply_realtime(k, {'date': last_date, 'open': 12.0, 'high': 13.5,
                       'low': 11.8, 'last': 13.0, 'volume': 999.0})
    assert k.close[-1] == 13.0 and k.high[-1] == 13.5 and k.volume[-1] == 999.0
    assert len(k.dates) == 3 and k.live is True
    print('  ✓ 快照日期相同 → 覆盖最后一根')

    # (b) 更晚的日期 → 追加一根
    later = (datetime.date.fromisoformat(last_date) + datetime.timedelta(days=1)).isoformat()
    apply_realtime(k, {'date': later, 'open': 13.0, 'high': 14.0,
                       'low': 12.5, 'last': 13.8, 'volume': 555.0})
    assert len(k.dates) == 4 and k.dates[-1] == later
    assert k.close[-1] == 13.8
    print('  ✓ 快照日期更晚 → 追加一根')

    # (c) 更旧的日期 → 忽略
    older = (datetime.date.fromisoformat(last_date) - datetime.timedelta(days=5)).isoformat()
    before = list(k.close)
    apply_realtime(k, {'date': older, 'open': 1.0, 'high': 1.0,
                       'low': 1.0, 'last': 1.0, 'volume': 1.0})
    assert k.close == before
    print('  ✓ 快照日期更旧 → 忽略，不污染历史')

    # (d) 空快照不炸
    apply_realtime(k, {})
    print('  ✓ 空快照安全')


# ============================================================
# 4. 筛选打分与过滤
# ============================================================

def _build_universe():
    """构造 4 个品种：逼近前高 / 逼近前低 / 远离两端 / 刚突破前高。"""
    n = 300
    # 稳步上涨后小幅回调 → 距前高约 3%
    up = [50 + 0.2 * i for i in range(n - 1)] + [50 + 0.2 * (n - 2) - 2.0]
    # 稳步下跌后小幅反弹 → 距前低约 2.5%
    down = [100 - 0.15 * i for i in range(n - 1)] + [100 - 0.15 * (n - 2) + 1.0]
    # 先涨后跌、收在长期区间中部 → 离前高和前低都远（必须被阈值过滤掉）
    # 注意不能只用单边下跌序列：单边下跌永远贴着自己的前低，反而会被选中
    far = [50 + 1.0 * i for i in range(150)] + [200 - 0.55 * i for i in range(1, 151)]
    # 突破前高
    brk = [50 + 0.15 * i for i in range(n - 1)] + [50 + 0.15 * (n - 2) + 5.0]

    return {
        'TESTUP': make_kline('TESTUP', closes=up),
        'TESTDOWN': make_kline('TESTDOWN', closes=down),
        'TESTFAR': make_kline('TESTFAR', closes=far),
        'TESTBRK': make_kline('TESTBRK', closes=brk),
    }


def _register_test_symbols():
    """把虚构品种注册进 SYMBOLS，便于全流程测试。"""
    for code, name, exch in [('TESTUP', '测试涨', 'SHFE'),
                             ('TESTDOWN', '测试跌', 'DCE'),
                             ('TESTFAR', '测试远', 'CZCE'),
                             ('TESTBRK', '测试破', 'SHFE')]:
        market_data.SYMBOLS[code] = SymbolMeta(code, name, exch)


def test_screening(tmpdir):
    _register_test_symbols()
    mapping = _build_universe()
    store = make_store(mapping, tmpdir)

    base = dict(proximity=5.0, lookback=250, require_level=True,
                # 测试用 FakeProvider 只提供「主力连续」序列，没有单合约数据，
                # 因此关掉换月复权（否则会去真实网络拉单个合约）。
                # 复权逻辑本身由 tests/test_contracts.py 单独覆盖。
                adjusted=False)
    codes = list(mapping.keys())

    def cfg(**kw):
        """在 base 基础上覆盖参数，避免重复关键字。"""
        merged = dict(base)
        merged.update(kw)
        return screener.ScreenerConfig(symbols=codes, **merged)

    res = screener.screen(cfg(), store=store)
    found = {r['code'] for r in res['results']}

    assert 'TESTFAR' not in found, '离前高很远的品种不应入选'
    assert 'TESTUP' in found, f"逼近前高的品种应入选, 实际 {found}"
    assert 'TESTDOWN' in found, f"逼近前低的品种应入选, 实际 {found}"
    assert 'TESTBRK' in found, '已突破的品种应入选'

    up = next(r for r in res['results'] if r['code'] == 'TESTUP')
    assert up['direction'] == 'long', up['direction']
    assert 0 <= up['score'] <= 100
    assert abs(sum(up['scores'].values()) - up['score']) < 0.3, (
        '总分应等于各维度之和', up['scores'], up['score'])
    print(f"  ✓ 阈值过滤：命中 {sorted(found)}（TESTFAR 被排除）")
    print(f"  ✓ 评分守恒：各维度之和 = 总分 = {up['score']}")

    # 结果按评分倒序
    scores = [r['score'] for r in res['results']]
    assert scores == sorted(scores, reverse=True), scores
    print('  ✓ 结果按评分倒序')

    # 方向过滤
    long_only = screener.screen(cfg(direction='long'), store=store)
    assert all(r['direction'] == 'long' for r in long_only['results'])
    assert 'TESTDOWN' not in {r['code'] for r in long_only['results']}
    print('  ✓ direction=long 只返回多头')

    # 阈值收紧到 0.5% → 命中的更少
    tight = screener.screen(cfg(proximity=0.5), store=store)
    loose_count = len(res['results'])
    assert len(tight['results']) <= loose_count
    print(f"  ✓ 阈值 5% → {loose_count} 个, 0.5% → {len(tight['results'])} 个（单调）")

    # 最低评分过滤
    hi = screener.screen(cfg(min_score=60), store=store)
    assert all(r['score'] >= 60 for r in hi['results'])
    print(f"  ✓ min_score=60 过滤生效（{len(hi['results'])} 个）")

    # 交易所过滤
    by_exch = screener.screen(cfg(exchanges=['DCE']), store=store)
    assert all(r['exchange'] == 'DCE' for r in by_exch['results'])
    print('  ✓ 交易所过滤生效')

    # require_level=False → 全品种都参与评分
    allscored = screener.screen(cfg(require_level=False), store=store)
    assert 'TESTFAR' in {r['code'] for r in allscored['results']}
    print('  ✓ require_level=0 时全品种参与综合评分')

    # 未知品种不应崩
    unknown = screener.screen(screener.ScreenerConfig(
        symbols=['NOPE'], proximity=5.0, lookback=250), store=store)
    assert unknown['results'] == []
    print('  ✓ 未知品种安全返回空')

    # 参数解析容错
    class FakeArgs(dict):
        def get(self, k, d=None):
            return dict.get(self, k, d)

    cfg = screener.ScreenerConfig.from_query(FakeArgs(
        proximity='abc', lookback='', direction='weird',
        exchanges='SHFE,BAD,XYZ', symbols='cu,rb'))
    assert cfg.proximity == 5.0, cfg.proximity
    assert cfg.lookback == 250, cfg.lookback
    assert cfg.direction == 'all', cfg.direction
    assert cfg.exchanges == ['SHFE'], cfg.exchanges
    assert cfg.symbols == ['CU', 'RB'], cfg.symbols
    print('  ✓ 非法查询参数回退默认值')

    # 缓存：第二次不应重复请求 provider
    calls_before = store.provider.calls
    store.load(codes)
    assert store.provider.calls == calls_before, 'TTL 内不应重新拉取'
    print('  ✓ 磁盘/内存缓存生效，未重复请求数据源')


# ============================================================
# 5. REST API
# ============================================================

def test_api(tmpdir):
    import screener_api
    import review_web

    mapping = _build_universe()
    store = make_store(mapping, tmpdir)
    screener_api.get_store = lambda: store          # 替换为假数据源
    screener_api.WATCHLIST_FILE = os.path.join(tmpdir, 'watchlist.json')
    screener_api._result_cache.clear()

    client = review_web.app.test_client()

    # meta
    r = client.get('/api/screener/meta')
    assert r.status_code == 200
    meta = r.get_json()
    assert meta['symbols'] and meta['exchanges']
    assert 'lookback_presets' in meta and 'weights' in meta
    print(f"  ✓ /api/screener/meta 返回 {len(meta['symbols'])} 个品种")

    # 筛选
    r = client.get('/api/screener?proximity=5&lookback=250&adjusted=0'
                   '&symbols=TESTUP,TESTDOWN,TESTFAR,TESTBRK')
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    body = r.get_json()
    assert 'results' in body and 'stats' in body and 'meta' in body
    assert body['stats']['matched'] == len(body['results'])
    assert 'TESTFAR' not in {x['code'] for x in body['results']}
    # 严格 JSON：不得出现 NaN/Infinity 字面量
    import json as _json
    text = r.get_data(as_text=True)
    _json.loads(text)
    assert 'NaN' not in text and 'Infinity' not in text
    print(f"  ✓ /api/screener 命中 {body['stats']['matched']} 个，JSON 严格合法")

    # 缓存命中
    r2 = client.get('/api/screener?proximity=5&lookback=250&adjusted=0'
                    '&symbols=TESTUP,TESTDOWN,TESTFAR,TESTBRK')
    assert r2.get_json().get('cached') is True
    print('  ✓ 相同参数二次请求命中结果缓存')

    # 回看交易日数支持任意值（不是只有预设的几个）
    r = client.get('/api/screener?lookback=137&require_level=0'
                   '&symbols=TESTUP,TESTDOWN,TESTFAR,TESTBRK&adjusted=0')
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    body = r.get_json()
    assert body['meta']['lookback'] == 137, body['meta']['lookback']
    print('  ✓ lookback=137（任意值）被接受')

    # 参考区间模式
    r = client.get('/api/screener?ref_start=2025-03-01&ref_end=2025-09-30'
                   '&require_level=0&symbols=TESTUP,TESTDOWN,TESTFAR,TESTBRK&adjusted=0')
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    body = r.get_json()
    assert body['meta']['level_mode'] == 'range', body['meta']['level_mode']
    assert body['meta']['ref_start'] == '2025-03-01'
    print('  ✓ 参考区间模式（按日期段取高低点）生效')

    # 详情
    r = client.get('/api/screener/detail/TESTUP?lookback=250')
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    d = r.get_json()
    assert d['code'] == 'TESTUP' and d['analysis']['latest']
    s = d['analysis']['series']
    assert len(s['dates']) == len(s['close']) == len(s['macd'])
    assert len(s['dates']) <= 320
    assert s['level_high'] is not None and s['level_low'] is not None
    assert d['screen_snapshot'] is not None
    print(f"  ✓ /api/screener/detail 返回 {len(s['dates'])} 根 K 线且序列等长")

    r = client.get('/api/screener/detail/NOSUCH')
    assert r.status_code == 404
    print('  ✓ 未知品种详情返回 404')

    # 自选：增
    r = client.post('/api/watchlist', json={'code': 'TESTUP'})
    assert r.status_code == 200 and r.get_json()['success']
    # 重复添加幂等
    r = client.post('/api/watchlist', json={'code': 'TESTUP'})
    assert r.get_json().get('already') is True
    # 不存在品种
    r = client.post('/api/watchlist', json={'code': 'NOPE'})
    assert r.status_code == 400
    # 查
    items = client.get('/api/watchlist').get_json()['items']
    assert [i['code'] for i in items] == ['TESTUP']
    assert items[0]['name'] == '测试涨'
    print('  ✓ 自选增/查/幂等/非法品种校验正确')

    # 落盘可复读
    assert os.path.exists(screener_api.WATCHLIST_FILE)
    items2 = screener_api._read_watchlist()
    assert [i['code'] for i in items2] == ['TESTUP']
    print('  ✓ 自选列表持久化可复读')

    # 删
    r = client.delete('/api/watchlist/TESTUP')
    assert r.status_code == 200
    assert client.get('/api/watchlist').get_json()['items'] == []
    r = client.delete('/api/watchlist/TESTUP')
    assert r.status_code == 404
    print('  ✓ 自选删除与重复删除（404）正确')

    # 刷新
    r = client.post('/api/screener/refresh')
    assert r.status_code == 200
    print('  ✓ /api/screener/refresh 正常')


# ============================================================
# 入口
# ============================================================

def main():
    import tempfile

    print('[1] 技术指标口径')
    test_indicators()

    print('\n[2] 关键点位（不含当日）')
    test_key_levels()

    print('\n[3] 实时快照合并')
    test_realtime_merge()

    with tempfile.TemporaryDirectory() as tmp:
        print('\n[4] 筛选打分与过滤')
        test_screening(tmp)

        print('\n[5] REST API')
        test_api(tmp)

    print('\n全部断言通过 ✔')


if __name__ == '__main__':
    main()
