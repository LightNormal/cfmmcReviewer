# -*- coding: utf-8 -*-
"""回测脚本的正确性测试（不联网）

重点验证两件事：

1. **没有未来函数** —— 这是回测唯一不能错的地方。
   构造两条序列：T 之前完全相同，T 之后一条暴涨、一条暴跌。
   如果第 T 日的评分因为"后面涨了"而改变，说明偷看了未来。
2. 统计函数本身算得对 —— IC、Top-N 超额、事件研究的符号方向。

用法：python tests/test_backtest.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import backtest as bt
from market_data import Kline
from screener import ScreenerConfig


# ============================================================
# 构造数据
# ============================================================

def make_kline(code='CU', n=420, base=50000.0, amp=0.012, overrides=None):
    """造一条确定性的伪随机游走日线（不依赖随机种子，结果可复现）。"""
    import datetime
    import math
    dates, opens, highs, lows, closes, vols = [], [], [], [], [], []
    price = base
    day0 = datetime.date(2024, 1, 1)
    for i in range(n):
        # 两个不同周期的正弦叠加：既有趋势又有波动
        drift = 0.0004 * math.sin(i / 37.0) + 0.0002 * math.cos(i / 11.0)
        noise = amp * math.sin(i * 1.7) * math.cos(i / 5.3)
        nxt = price * (1.0 + drift + noise)
        o, c = price, nxt
        h = max(o, c) * (1 + 0.003)
        l = min(o, c) * (1 - 0.003)
        dates.append((day0 + datetime.timedelta(days=i)).isoformat())
        opens.append(o); highs.append(h); lows.append(l); closes.append(c)
        vols.append(100000.0 + 5000 * i)
        price = nxt
    k = Kline(code=code, dates=dates, open=opens, high=highs, low=lows,
              close=closes, volume=vols)
    if overrides:
        overrides(k)
    return k


def make_config():
    return ScreenerConfig(proximity=5.0, lookback=250, min_score=0.0,
                          require_level=False, direction='all')


# ============================================================
# 1. 切片 / 日期定位
# ============================================================

def test_slice():
    k = make_kline(n=420)
    sub = bt._slice(k, 300, 250)
    assert len(sub) == 250, len(sub)
    assert sub.dates[-1] == k.dates[300]
    assert sub.dates[0] == k.dates[51]
    assert sub.close[-1] == k.close[300]
    assert sub.open[0] == k.open[51]
    print('  ✓ 切片长度、起止日期、价格对齐正确')

    # 数据不足时从头取
    sub2 = bt._slice(k, 10, 250)
    assert len(sub2) == 11 and sub2.dates[-1] == k.dates[10]
    print('  ✓ 历史不足时从第一根开始，不越界')


def test_idx_of():
    k = make_kline(n=100)
    assert bt._idx_of(k, k.dates[0]) == 0
    assert bt._idx_of(k, k.dates[57]) == 57
    assert bt._idx_of(k, k.dates[-1]) == 99
    assert bt._idx_of(k, '1999-01-01') is None
    assert bt._idx_of(k, '2999-12-31') is None
    print('  ✓ 日期二分定位正确，缺失日期返回 None')


# ============================================================
# 2. 未来函数（最关键）
# ============================================================

def test_no_lookahead():
    """T 日之后的行情怎么变，都不能影响 T 日的评分。

    分两层验证：
      A. 直接测评分机制：同一 T，切片之后的数据改成暴涨/暴跌 → 评分必须一致
      B. 再测 collect_rows 整体：只改最后一个测试日之后的数据 → 该日评分必须一致
    """
    from screener import evaluate

    cut = 300                     # 结算这天
    base = make_kline(n=420)
    cfg = make_config()

    def spike(k, start, factor):
        for i in range(start, len(k.close)):
            k.close[i] *= factor
            k.high[i] *= factor
            k.low[i] *= factor
            k.open[i] *= factor

    up = make_kline(n=420, overrides=lambda k: spike(k, cut + 1, 1.5))
    down = make_kline(n=420, overrides=lambda k: spike(k, cut + 1, 0.5))

    # 确认两条序列在 T 及之前完全相同
    assert up.close[:cut + 1] == down.close[:cut + 1]
    assert up.close[-1] != down.close[-1]

    # ---- A. 评分机制：切片末尾 = cut，结构上不应看到 cut+1 ----
    r_up = evaluate('CU', bt._slice(up, cut, 400), cfg)
    r_down = evaluate('CU', bt._slice(down, cut, 400), cfg)
    assert r_up and r_down
    assert r_up['score'] == r_down['score'], \
        '评分受未来数据影响（%s vs %s）→ 存在未来函数' % (r_up['score'], r_down['score'])
    for key in ('level', 'trend', 'macd', 'boll', 'volume'):
        assert r_up['scores'][key] == r_down['scores'][key], \
            '维度 %s 被未来数据影响' % key
    # 关键点位也不能变
    assert r_up['level_price'] == r_down['level_price']
    assert r_up['dist_to_high_pct'] == r_down['dist_to_high_pct']
    print('  ✓ 评分/各维度/关键点位只依赖 T 及之前的数据')

    # ---- B. collect_rows 整体：改最后测试日之后的数据 ----
    n = len(base.dates)
    max_h = max(bt.HORIZONS)
    last_test = n - max_h - 1          # collect_rows 最后一天会落在的下标
    up2 = make_kline(n=n, overrides=lambda k: spike(k, last_test + 1, 1.5))
    down2 = make_kline(n=n, overrides=lambda k: spike(k, last_test + 1, 0.5))
    rows_up = bt.collect_rows({'CU': up2}, cfg, days=1, slice_bars=400, verbose=False)
    rows_down = bt.collect_rows({'CU': down2}, cfg, days=1, slice_bars=400, verbose=False)
    assert rows_up and rows_down
    assert rows_up[-1]['date'] == base.dates[last_test], rows_up[-1]['date']
    assert rows_up[-1]['score'] == rows_down[-1]['score'], \
        '%s 评分不同（%s vs %s）→ 存在未来函数' % (
            rows_up[-1]['date'], rows_up[-1]['score'], rows_down[-1]['score'])
    # 而前视收益必须不同（否则测试没意义）
    assert rows_up[-1]['ret20'] != rows_down[-1]['ret20']
    print('  ✓ collect_rows 整体流程无未来函数，且前视收益确实随未来变化')


def test_forward_return_values():
    """前视收益必须等于 close[T+h]/close[T]-1。"""
    k = make_kline(n=420)
    rows = bt.collect_rows({'CU': k}, make_config(), days=5, slice_bars=400, verbose=False)
    assert rows
    for r in rows:
        idx = bt._idx_of(k, r['date'])
        for h in bt.HORIZONS:
            expect = k.close[idx + h] / k.close[idx] - 1.0
            got = r['ret%d' % h]
            assert abs(got - expect) < 1e-12, (r['date'], h, got, expect)
    print('  ✓ 前视收益与真实收盘价比值一致')


# ============================================================
# 3. 统计函数
# ============================================================

def test_cross_section_ic():
    rows = []
    for d in range(1, 21):
        date = '2026-01-%02d' % d
        for i in range(12):
            rows.append({'date': date, 'score': float(i),
                         'ret5': i * 0.001})          # 完全正相关
    df = pd.DataFrame(rows)
    st = bt.cross_section_ic(df, 'score', 'ret5')
    assert abs(st['mean'] - 1.0) < 1e-9, st
    assert st['n'] == 20
    print('  ✓ 正相关数据 IC = 1.0')

    df2 = df.copy()
    df2['ret5'] = -df2['ret5']                        # 完全负相关
    st2 = bt.cross_section_ic(df2, 'score', 'ret5')
    assert abs(st2['mean'] + 1.0) < 1e-9, st2
    print('  ✓ 负相关数据 IC = -1.0')

    # 样本不足时不报错
    tiny = pd.DataFrame([{'date': '2026-01-01', 'score': 1.0, 'ret5': 0.01}])
    assert bt.cross_section_ic(tiny, 'score', 'ret5') is None
    print('  ✓ 截面样本不足时返回 None（不崩）')


def test_topn_excess():
    rows = []
    for d in range(1, 21):
        date = '2026-01-%02d' % d
        for i in range(30):
            rows.append({'date': date, 'score': float(i),
                         'ret5': i * 0.001})
    df = pd.DataFrame(rows)
    r = bt.topn_excess(df, 'ret5', 5)
    assert r['mean_excess'] > 0, r          # 取分数最高的 5 只 → 收益更高
    assert r['win_days'] == 1.0
    print('  ✓ Top-N 超额方向正确（分数越高收益越高时超额为正）')

    df2 = df.copy()
    df2['ret5'] = -df2['ret5']
    r2 = bt.topn_excess(df2, 'ret5', 5)
    assert r2['mean_excess'] < 0
    print('  ✓ 反向数据时超额为负')


def test_event_study_signing():
    """靠近前高＝做多方向，收益取正；靠近前低＝做空方向，收益取负。"""
    rows = [
        # 靠近前高（long），后续跌 2% → 按做多方向记 -2%
        {'date': '2026-01-01', 'direction': 'long', 'setup': 'approaching',
         'dist_high': 1.0, 'dist_low': 30.0, 'ret5': -0.02, 'ret20': -0.02},
        # 靠近前低（short），后续跌 2% → 做空方向记 +2%
        {'date': '2026-01-01', 'direction': 'short', 'setup': 'approaching',
         'dist_high': 30.0, 'dist_low': 1.0, 'ret5': -0.02, 'ret20': -0.02},
        # 远离关键点位，不应进入事件样本
        {'date': '2026-01-01', 'direction': 'long', 'setup': 'watch',
         'dist_high': 30.0, 'dist_low': 30.0, 'ret5': 0.5, 'ret20': 0.5},
    ]
    df = pd.DataFrame(rows)
    ev = bt.event_study(df, 'ret5', proximity=5.0)
    assert ev['n'] == 2, ev['n']                      # 第三条被排除
    assert abs(ev['by_dir']['long']['mean'] - (-0.02)) < 1e-12
    assert abs(ev['by_dir']['short']['mean'] - 0.02) < 1e-12
    assert abs(ev['mean']) < 1e-12                    # -2% 与 +2% 抵消
    print('  ✓ 事件研究按方向计收益、并正确排除阈值外的样本')


def test_decile_table():
    rows = []
    for d in range(1, 31):
        date = '2026-01-%02d' % d
        for i in range(20):
            rows.append({'date': date, 'score': float(i), 'ret5': i * 0.001})
    df = pd.DataFrame(rows)
    g = bt.decile_table(df, 'score', 'ret5')
    assert g is not None and len(g) >= 8, None if g is None else len(g)
    means = list(g['mean'])
    assert means == sorted(means), means               # 单调递增
    print('  ✓ 分组收益单调（构造的线性关系被正确还原）')


def test_report_smoke():
    """报告函数在小样本下也能跑通，不抛异常。"""
    rows = []
    for d in range(1, 16):
        date = '2026-01-%02d' % d
        for i, code in enumerate(['CU', 'RB', 'M', 'AU', 'I',
                                  'TA', 'SR', 'FG', 'PP', 'ZC']):
            rows.append({
                'date': date, 'code': code, 'score': 50.0 + i * 3,
                'direction': 'long' if i % 2 == 0 else 'short',
                'setup': ['broken', 'approaching', 'watch'][i % 3],
                'dist_high': 1.0 + i, 'dist_low': 2.0 + i,
                's_level': 20.0, 's_trend': 10.0, 's_macd': 8.0,
                's_boll': 8.0, 's_volume': 5.0,
                'ret5': (i - 5) * 0.002, 'ret10': (i - 5) * 0.003,
                'ret20': (i - 5) * 0.004,
            })
    df = bt.report(rows, make_config(), 5.0, 5)
    assert df is not None and len(df) == len(rows)
    print('  ✓ 报告函数在小样本下正常运行')


def main():
    print('[1] 切片与日期定位')
    test_slice()
    test_idx_of()

    print('\n[2] 未来函数（最关键）')
    test_no_lookahead()
    test_forward_return_values()

    print('\n[3] 统计函数')
    test_cross_section_ic()
    test_topn_excess()
    test_event_study_signing()
    test_decile_table()

    print('\n[4] 报告冒烟')
    test_report_smoke()

    print('\n全部断言通过 ✔')


if __name__ == '__main__':
    main()
