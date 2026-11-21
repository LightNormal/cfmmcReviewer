# -*- coding: utf-8 -*-
"""期权「实值行权」结算测试

背景（真实数据验证）：
    2026-06-22 的结算单里有两块：
      「期权成交汇总」  —— 当天的期权买卖成交
      「期权行权明细」  —— 实值到期被行权的记录（**不在成交汇总里**）

    实测这一行：
        中国金融期货交易所 | IO2606-C-5000 | IO2606 | 看涨期权 | 5000.0 |
        2026-06-22 | 买 | 1.0 | 2884.0 | 4.0 | 0.0 | 0.0

    只读「期权成交汇总」的话，这笔被行权的 1 手永远平不掉，
    最后会被当作「到期作废」按 0 处理 —— 变成全额权利金亏损，方向完全反了。

    反推行权结算价：5000 + 2884/100 = 5028.84（与沪深300 指数最后 2 小时
    均价 5027~5037 的估算一致，验证了作废/行权判定的方法）。

用法：python tests/test_exercise.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import review
from review import ReviewEngine
from test_review_engine import FakeSheet, FakeWorkbook, make_loader


BASE = [
    ['客户期货期权内部资金账户', '', '0072503313361', '', '', '交易日期', '', '2026-06-22'],
    ['上日结存', '', 105455.83, '', '', '客户权益', '', 94166.56],
    [''],
    ['期货成交汇总'],
    ['合约', '买/卖', '投机（一般）/套保/套利', '成交价', '手数', '成交额', '',
     '开/平', '手续费', '平仓盈亏'],
    ['合计'],
    [''],
    ['期权成交汇总'],
    ['品种合约', '标的合约', '期权类型', '执行价', '买/卖', '权利金单价',
     '成交量', '权利金', '是否备兑', '手续费'],
]

OPT_TAIL = [
    ['合计'],
    [''],
    ['期权持仓汇总'],
    ['品种合约', '标的合约', '期权类型', '执行价', '买持仓', '买均价',
     '卖持仓', '卖均价', '昨结算价', '今结算价', '交易保证金', '交易编码'],
    ['合计'],
    [''],
    ['期权行权明细'],
    ['交易所', '品种合约', '标的合约', '期权类型', '执行价', '行权日期',
     '买/卖', '成交量', '行权盈亏', '手续费', '行权锁定保证金', '冻结资金'],
]


def _parse(opt_rows, exercise_rows):
    rows = list(BASE) + list(opt_rows)
    if exercise_rows is not None:
        rows += list(OPT_TAIL) + list(exercise_rows) + [['合计']]
    else:
        rows += [['合计']]
    wb = FakeWorkbook([FakeSheet(rows)])
    ox, oo = review.xlrd.open_workbook, review._looks_like_excel
    review.xlrd.open_workbook = lambda fp: wb
    review._looks_like_excel = lambda fp: True
    try:
        p = review.parse_trade_xls('fake.xls')
    finally:
        review.xlrd.open_workbook, review._looks_like_excel = ox, oo
    p['date'] = '2026-06-22'
    return p


BUY_2 = ['IO2606-C-5000', 'IO2606', '看涨期权', 5000.0, '买', 4.0, 2.0, -800.0, '', 30.02]
SELL_1 = ['IO2606-C-5000', 'IO2606', '看涨期权', 5000.0, '卖', 10.0, 1.0, 1000.0, '', 15.01]
EX_1 = ['中国金融期货交易所', 'IO2606-C-5000', 'IO2606', '看涨期权', 5000.0,
        '2026-06-22', '买', 1.0, 2884.0, 4.0, 0.0, 0.0]


# ============================================================
# 1. 行权明细解析
# ============================================================

def test_parse_exercise():
    p = _parse([BUY_2, SELL_1], [EX_1])
    ex = p['option_exercise']
    assert len(ex) == 1, ex
    e = ex[0]
    assert e['opt_contract'] == 'IO2606-C-5000'
    assert e['direction'] == '买'
    assert e['volume'] == 1.0
    assert e['exercise_pnl'] == 2884.0
    assert e['commission'] == 4.0
    assert e['strike'] == 5000.0
    assert e['date'] == '2026-06-22'
    print('  ✓ 期权行权明细解析正确（合约/方向/手数/行权盈亏/手续费）')

    # 行权结算价 = 5000 + 2884/(1*100)
    intrinsic = abs(e['exercise_pnl']) / (e['volume'] * 100)
    assert abs(intrinsic - 28.84) < 1e-9, intrinsic
    print(f'  ✓ 反推行权内在价值 = {intrinsic} 点（结算价 5028.84）')

    # 没有行权区块时为空列表，不报错
    p2 = _parse([BUY_2], None)
    assert p2['option_exercise'] == []
    print('  ✓ 无行权区块时安全返回空列表')


# ============================================================
# 2. FIFO 端到端
# ============================================================

def test_buyer_exercised():
    p = _parse([BUY_2, SELL_1], [EX_1])
    eng = ReviewEngine(make_loader({'2026-06-22': p}))
    res = eng.query_by_date_range('2026-06-22', '2026-06-22')

    matched = eng.get_matched_options_df()
    got = {str(r['direction']): r for _, r in matched.iterrows()}

    # 卖 1 手 @10 平掉第一手 @4：+600
    assert abs(float(got['卖']['realized_pnl']) - 600.0) < 1e-6, got['卖'].to_dict()
    # 剩下的 1 手被行权：内在价值 28.84 - 成本 4 = 24.84 点 → +2484
    assert '行权' in got, matched[['direction', 'realized_pnl']].to_dict('records')
    assert abs(float(got['行权']['realized_pnl']) - 2484.0) < 1e-6, got['行权'].to_dict()
    assert got['行权']['premium_price'] == 28.84

    # 总计 3084
    assert abs(res['overview']['期权盈亏(元)'] - 3084.0) < 1e-6, res['overview']

    # 行权后该合约无未平仓，且**不应**再产生「到期作废」行
    assert eng.option_open_positions['IO2606-C-5000']['long'] == 0
    assert res['expiry_settlements'] == [], res['expiry_settlements']
    print('  ✓ 买 2 卖 1 行权 1：平仓 +600、行权 +2484、合计 +3084，无重复的作废结算')

    # 手续费含行权的 4.0
    assert abs(res['overview']['期权手续费(元)'] - (30.02 + 15.01 + 4.0)) < 1e-6
    print('  ✓ 行权手续费已计入')


def test_seller_assigned():
    """卖方被指派：方向应翻转为「买」来平掉空头。"""
    opt_rows = [
        ['IO2606-C-5000', 'IO2606', '看涨期权', 5000.0, '卖', 10.0, 1.0, 1000.0, '', 15.01],
    ]
    ex = [['中国金融期货交易所', 'IO2606-C-5000', 'IO2606', '看涨期权', 5000.0,
           '2026-06-22', '卖', 1.0, -2884.0, 4.0, 0.0, 0.0]]
    p = _parse(opt_rows, ex)
    eng = ReviewEngine(make_loader({'2026-06-22': p}))
    eng.query_by_date_range('2026-06-22', '2026-06-22')

    matched = eng.get_matched_options_df()
    row = matched[matched['direction'] == '行权'].iloc[0]
    # 卖 1 手 @10 收 1000；被指派赔内在价值 28.84 → (10 - 28.84) × 100 = -1884
    assert abs(float(row['realized_pnl']) - (-1884.0)) < 1e-6, row.to_dict()
    print('  ✓ 卖方被指派：按内在价值平空 → -1884 元')


def test_worthless_still_zero():
    """虚值到期仍走「按 0 作废」，不能被行权逻辑影响。"""
    opt_rows = [['IO2606-C-6000', 'IO2606', '看涨期权', 6000.0, '买', 4.0, 1.0, -400.0, '', 15.01]]
    p = _parse(opt_rows, None)
    eng = ReviewEngine(make_loader({'2026-06-22': p}))
    res = eng.query_by_date_range('2026-06-22', '2026-06-22')
    assert abs(res['overview']['期权盈亏(元)'] + 400.0) < 1e-6, res['overview']
    assert len(res['expiry_settlements']) == 1
    assert abs(res['expiry_settlements'][0]['pnl'] + 400.0) < 1e-6
    print('  ✓ 虚值到期仍按 0 作废（-400），未受行权逻辑影响')


def main():
    print('[1] 行权明细解析')
    test_parse_exercise()

    print('\n[2] FIFO 端到端')
    test_buyer_exercised()
    test_seller_assigned()
    test_worthless_still_zero()

    print('\n全部断言通过 ✔')


if __name__ == '__main__':
    main()
