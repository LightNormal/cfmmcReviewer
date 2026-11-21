# -*- coding: utf-8 -*-
"""账户权益曲线 / 最大回撤 测试

背景：
    结算单「资金状况」区的 客户权益、保证金占用、当日结存 早就解析出来了，
    但此前只被用来算一个 `len()` —— 解析成本白付，用户也看不到账户是怎么
    走过来的（尤其看不到回撤）。

本测试锁定：
  1. get_equity_curve() 的列与派生字段（可用资金 = 权益 - 保证金）
  2. 无资金数据时返回空表（不画一条压在 0 上的直线）
  3. compute_equity_stats() 的首末权益 / 区间变动 / 最大回撤 / 峰谷日期
  4. /api/chart-data 透出 equity 与 equity_stats

用法：python tests/test_equity_curve.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

import review
from review import ReviewEngine, compute_equity_stats
from test_review_engine import build_day, patched_parse


class _Loader:
    def __init__(self, cache):
        self.cache = cache
        self.output_dir = 'x'


def _loader(equities, margins=None, deposits=None):
    """equities: {date: 客户权益}。"""
    cache = {}
    n = len(equities)
    margins = margins or [40000] * n
    for i, (d, eq) in enumerate(equities.items()):
        summary = {
            '平仓盈亏': 0, '当日手续费': 0, '客户权益': eq,
            '保证金占用': margins[i],
        }
        if deposits:
            summary['当日结存'] = deposits[i]
        p = patched_parse(build_day(d, [], [], summary))
        p['date'] = d
        cache[d] = p
    return _Loader(cache)


# ============================================================
# 1. 曲线本身
# ============================================================

def test_curve():
    loader = _loader({'2026-03-02': 100000, '2026-03-03': 120000,
                      '2026-03-04': 90000})
    eng = ReviewEngine(loader)
    eng.build_dfs('2026-03-02', '2026-03-04')
    curve = eng.get_equity_curve()

    for col in ('date', 'equity', 'deposit', 'margin', 'available',
                'pnl', 'commission', 'float_pnl'):
        assert col in curve.columns, f'缺少列 {col}'
    assert len(curve) == 3

    # 可用资金 = 权益 - 保证金
    assert list(curve['equity']) == [100000.0, 120000.0, 90000.0]
    assert list(curve['margin']) == [40000.0, 40000.0, 40000.0]
    assert list(curve['available']) == [60000.0, 80000.0, 50000.0]
    print('  ✓ 列完整，可用资金 = 权益 - 保证金')


def test_curve_empty_without_funds():
    """结算单没有资金状况区时，不能画一条压在 0 上的直线。"""
    cache = {}
    for d in ('2026-03-02', '2026-03-03'):
        p = patched_parse(build_day(d, [], [], {'平仓盈亏': 0, '当日手续费': 0}))
        p['date'] = d
        cache[d] = p
    eng = ReviewEngine(_Loader(cache))
    eng.build_dfs('2026-03-02', '2026-03-03')
    curve = eng.get_equity_curve()
    assert curve.empty, curve
    print('  ✓ 无资金数据 → 返回空表（不画无意义的直线）')

    # 窗口为空也一样
    eng.build_dfs('2027-01-01', '2027-01-31')
    assert eng.get_equity_curve().empty
    print('  ✓ 空窗口安全')


# ============================================================
# 2. 统计与回撤
# ============================================================

def test_stats():
    loader = _loader({'2026-03-02': 100000, '2026-03-03': 120000,
                      '2026-03-04': 90000, '2026-03-05': 95000,
                      '2026-03-06': 110000})
    eng = ReviewEngine(loader)
    eng.build_dfs('2026-03-02', '2026-03-06')
    st = compute_equity_stats(eng.get_equity_curve())

    assert st['first_equity'] == 100000.0
    assert st['last_equity'] == 110000.0
    assert st['change'] == 10000.0
    assert st['change_pct'] == 10.0
    # 120000 → 90000 是最大回撤
    assert st['max_drawdown'] == -30000.0, st['max_drawdown']
    assert st['max_drawdown_pct'] == -25.0, st['max_drawdown_pct']
    assert st['peak_date'] == '2026-03-03'
    assert st['trough_date'] == '2026-03-04'
    assert st['days'] == 5
    print('  ✓ 首末/变动/最大回撤(-30000, -25%)/峰谷日期 全部正确')


def test_stats_monotonic():
    """一路上涨 → 最大回撤为 0。"""
    loader = _loader({d: v for d, v in zip(
        ['2026-03-02', '2026-03-03', '2026-03-04'], [100000, 110000, 120000])})
    eng = ReviewEngine(loader)
    eng.build_dfs('2026-03-02', '2026-03-04')
    st = compute_equity_stats(eng.get_equity_curve())
    assert st['max_drawdown'] == 0.0, st['max_drawdown']
    assert st['max_drawdown_pct'] == 0.0, st['max_drawdown_pct']
    print('  ✓ 单调上涨时最大回撤为 0')

    # 一路下跌 → 回撤从首日开始算
    loader2 = _loader({d: v for d, v in zip(
        ['2026-03-02', '2026-03-03', '2026-03-04'], [120000, 100000, 90000])})
    eng2 = ReviewEngine(loader2)
    eng2.build_dfs('2026-03-02', '2026-03-04')
    st2 = compute_equity_stats(eng2.get_equity_curve())
    assert st2['max_drawdown'] == -30000.0, st2['max_drawdown']
    assert st2['peak_date'] == '2026-03-02'
    assert st2['trough_date'] == '2026-03-04'
    print('  ✓ 单调下跌时峰在首日、谷在末日')


def test_stats_edges():
    # 空表
    assert compute_equity_stats(pd.DataFrame()) == {}
    # 只有 date 列
    assert compute_equity_stats(pd.DataFrame({'date': []})) == {}
    # 权益全 0
    df = pd.DataFrame({'date': pd.to_datetime(['2026-03-02', '2026-03-03']),
                       'equity': [0.0, 0.0]})
    assert compute_equity_stats(df) == {}
    print('  ✓ 空表 / 缺列 / 权益全 0 都安全返回空 dict')


# ============================================================
# 3. REST API
# ============================================================

def test_api():
    import review_web

    cache = {}
    for d, eq in {'2026-03-02': 100000, '2026-03-03': 120000,
                  '2026-03-04': 90000}.items():
        p = patched_parse(build_day(d, [], [], {
            '平仓盈亏': 0, '当日手续费': 0, '客户权益': eq,
            '保证金占用': 40000, '当日结存': eq - 1000}))
        p['date'] = d
        cache[d] = p

    def fake_load_all(self, quarantine=True):
        self.cache = dict(cache)
        return self.cache

    orig = review.DataLoader.load_all
    review.DataLoader.load_all = fake_load_all
    review_web._loader = None
    try:
        c = review_web.app.test_client()
        d = c.get('/api/chart-data?start=2026-03-02&end=2026-03-04').get_json()
        assert 'equity' in d and len(d['equity']) == 3, d.keys()
        assert 'equity_stats' in d
        assert d['equity_stats']['max_drawdown'] == -30000.0
        assert d['equity'][0]['date'] == '2026-03-02'      # 已是 YYYY-MM-DD
        assert 'available' in d['equity'][0]
        print('  ✓ /api/chart-data 透出 equity（含 available）与 equity_stats')
    finally:
        review.DataLoader.load_all = orig


def main():
    print('[1] 权益曲线')
    test_curve()
    test_curve_empty_without_funds()

    print('\n[2] 统计与最大回撤')
    test_stats()
    test_stats_monotonic()
    test_stats_edges()

    print('\n[3] REST API')
    test_api()

    print('\n全部断言通过 ✔')


if __name__ == '__main__':
    main()
