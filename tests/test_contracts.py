# -*- coding: utf-8 -*-
"""换月复权的正确性测试（不联网，全部用假数据）

为什么必须测：
    复权算错了不会报错，只会**静默地**把所有回测结论带偏。这里锁定三件事：

      1. 主力合约是按「持仓量最大」选出来的，并且有切换缓冲，不会来回抖
      2. 换月比例的方向算对（后复权：最新价保持不变，历史价按比例缩放）
      3. 复权后换月处不再有假跳空；同时异常比例会被跳过，不会毁掉整条序列

用法：python tests/test_contracts.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import contracts
from contracts import adjust_factors, candidate_contracts


# ============================================================
# 1. 合约代码生成
# ============================================================

def test_candidate_contracts():
    got = candidate_contracts('CU', (2024, 11), (2025, 2))
    assert got == ['CU2411', 'CU2412', 'CU2501', 'CU2502'], got
    print('  ✓ 候选合约跨年展开正确')

    # 郑商所也用 4 位（实测 TA2601 有效、TA601 无效）
    assert candidate_contracts('TA', (2026, 1), (2026, 1)) == ['TA2601']
    print('  ✓ 郑商所品种同样使用 4 位 YYMM')


# ============================================================
# 2. 假数据构造
# ============================================================

def _bar(d, o, h, l, c, v, p):
    return {'d': d, 'o': str(o), 'h': str(h), 'l': str(l),
            'c': str(c), 'v': str(v), 'p': str(p)}


def _fake_contracts():
    """三个合约，持仓量交叉 → 主力从 CU2409 切到 CU2410。

    CU2401：价格从 100 每天 +1，持仓量从 1000 每天 -50
    CU2402：价格从 110 每天 +1，持仓量从 100 每天 +200
    在 i=4 时 CU2410 持仓 900 > CU2409 的 800×1.05=840 → 换月
    """
    out = {}
    days = ['2024-01-%02d' % (i + 1) for i in range(10)]
    a, b = [], []
    for i, d in enumerate(days):
        a.append(_bar(d, 100 + i, 101 + i, 99 + i, 100 + i, 1000, 1000 - i * 50))
        b.append(_bar(d, 110 + i, 111 + i, 109 + i, 110 + i, 1000, 100 + i * 200))
    out['CU2401'] = a
    out['CU2402'] = b
    return out, days


def _with_fake_network(fn):
    """把网络层换成假数据，并把缓存目录指到临时目录。"""
    old_dir = contracts.CACHE_DIR
    old_fetch = contracts.fetch_contract
    tmp = tempfile.mkdtemp()
    fake, days = _fake_contracts()

    def fake_fetch(contract, force=False):
        return fake.get(contract)

    contracts.CACHE_DIR = tmp
    contracts.fetch_contract = fake_fetch
    try:
        return fn(days)
    finally:
        contracts.CACHE_DIR = old_dir
        contracts.fetch_contract = old_fetch


# ============================================================
# 3. 主力还原
# ============================================================

def test_build_main_series():
    def run(days):
        return contracts.build_main_series('CU', '2024-01-01', '2024-01-31')

    series = _with_fake_network(run)
    assert series is not None, '没能还原主力序列'
    assert len(series['dates']) == 10, series['dates']

    # 前 4 天是 CU2409，之后切到 CU2410
    assert series['contract'][:4] == ['CU2401'] * 4, series['contract']
    assert series['contract'][4:] == ['CU2402'] * 6, series['contract']
    print('  ✓ 按持仓量选出主力，并在确认超过 5% 后才切换')

    rolls = series['rolls']
    assert len(rolls) == 1, rolls
    r = rolls[0]
    assert r['old'] == 'CU2401' and r['new'] == 'CU2402'
    assert r['date'] == '2024-01-05', r['date']
    assert r['index'] == 4, r['index']
    # CU2409 在 d4 收盘 104，CU2410 收盘 114 → 跳空 +9.615%
    assert abs(r['gap_pct'] - (114 - 104) / 104 * 100) < 1e-6, r['gap_pct']
    print('  ✓ 换月日、新旧合约、跳空幅度都正确')

    # 价格取的是对应主力合约的当日收盘
    assert series['close'][0] == 100.0
    assert series['close'][4] == 114.0     # 已换成 CU2410
    print('  ✓ 每日价格取自当日主力合约')


# ============================================================
# 4. 复权因子与连续性
# ============================================================

def test_adjust_factors():
    rolls = [{'index': 4, 'old_close': 104.0, 'new_close': 114.0}]
    f = adjust_factors(10, rolls)
    ratio = 114.0 / 104.0
    assert all(abs(x - ratio) < 1e-12 for x in f[:4]), f[:4]
    assert all(abs(x - 1.0) < 1e-12 for x in f[4:]), f[4:]
    print('  ✓ 后复权：换月之后因子为 1（最新价保持真实），之前按比例放大')

    # 异常比例（>2 倍）应被跳过，不影响序列
    bad = [{'index': 4, 'old_close': 1.0, 'new_close': 9.0}]
    f2 = adjust_factors(10, bad)
    assert all(x == 1.0 for x in f2), f2
    print('  ✓ 离谱的换月比例被跳过（不会毁掉整条序列）')


def test_adjustment_continuity():
    """复权后，换月处的日涨跌应恢复到正常量级。"""
    from market_data import Kline

    def run(days):
        return contracts.build_adjusted_kline('CU', '2024-01-01', '2024-01-31')

    k = _with_fake_network(run)
    assert k is not None and len(k.close) == 10

    # 未复权时 104 → 114 是 +9.6%，复权后应回到 ~+1%
    rets = [k.close[i] / k.close[i - 1] - 1 for i in range(1, len(k.close))]
    worst = max(abs(r) for r in rets)
    assert worst < 0.02, '复权后仍有 %.2f%% 的异常跳空：%s' % (worst * 100, rets)
    print('  ✓ 换月处假跳空已消除（最大日变动 %.2f%%，未复权时是 9.6%%）'
          % (worst * 100))

    # 最新价必须与原始序列一致（后复权的定义）
    assert abs(k.close[-1] - 110.0 + 9) < 1e-9 or abs(k.close[-1] - 119.0) < 1e-9, k.close[-1]
    print('  ✓ 最新价与未复权主力序列一致（%.1f）' % k.close[-1])

    # 成交量、持仓量不参与复权
    assert k.volume[0] == 1000.0
    print('  ✓ 成交量/持仓量不做复权（它们是量不是价）')


# ============================================================
# 5. 无数据时的行为
# ============================================================

def test_no_data():
    old_dir = contracts.CACHE_DIR
    old_fetch = contracts.fetch_contract
    contracts.CACHE_DIR = tempfile.mkdtemp()
    contracts.fetch_contract = lambda c, force=False: None
    try:
        assert contracts.build_main_series('XX', '2024-01-01', '2024-01-31') is None
        assert contracts.build_adjusted_kline('XX', '2024-01-01', '2024-01-31') is None
    finally:
        contracts.CACHE_DIR = old_dir
        contracts.fetch_contract = old_fetch
    print('  ✓ 没有任何合约数据时安全返回 None，不抛异常')


def main():
    print('[1] 合约代码生成')
    test_candidate_contracts()

    print('\n[2] 主力还原')
    test_build_main_series()

    print('\n[3] 复权因子')
    test_adjust_factors()
    test_adjustment_continuity()

    print('\n[4] 边界情况')
    test_no_data()

    print('\n全部断言通过 ✔')


if __name__ == '__main__':
    main()
