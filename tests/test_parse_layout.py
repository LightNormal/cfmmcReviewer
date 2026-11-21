# -*- coding: utf-8 -*-
"""结算单列布局鲁棒性测试

背景（用户实际报的问题）：
    用户贴出结算单里的一行：
        IO2611-P-3900  IO2611  看跌期权  买  3,900.00  7.00  1  +0.00  15.01
    即列顺序是「合约 标的 类型 买卖 行权价 权利金价格 手数 平仓盈亏 手续费」。

    而早期解析器写死的是「合约 标的 类型 **行权价 买卖** 权利金价格 手数 权利金 手续费」。
    于是它把 col4 的「3,900.00」当成了买卖方向，`direction.startswith('买')` 为假，
    整行被判为"方向无法识别"直接跳过 —— 这个期权的盈亏恒为 0。

    同时 `float('3,900.00')` 会因为千分位逗号抛异常并被吞成 0.0。

修复：
    1. 不再写死列号，改为按表头关键字建映射（识别不出才回退旧版固定列）
    2. `_to_float` 支持千分位、正负号、括号负数、全角字符、'--'
    3. 结算单没有「权利金总额」列时，用 单价×手数×乘数 自行补算

用法：python tests/test_parse_layout.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import review
from review import ReviewEngine, _to_float
from test_review_engine import (FakeSheet, FakeWorkbook, build_day, make_loader,
                                patched_parse)


def _parse_rows(rows):
    """直接喂一组原始行给 parse_trade_xls。"""
    wb = FakeWorkbook([FakeSheet(rows)])
    ox, oo = review.xlrd.open_workbook, review._looks_like_excel
    review.xlrd.open_workbook = lambda fp: wb
    review._looks_like_excel = lambda fp: True
    try:
        return review.parse_trade_xls('fake.xls')
    finally:
        review.xlrd.open_workbook, review._looks_like_excel = ox, oo


HEAD = [
    ['客户期货期权内部资金账户', '', '0072503313361', '', '', '交易日期', '', '2026-11-20'],
    ['上日结存', '', 1000000, '', '', '客户权益', '', 990000],
    [''],
    ['期货成交汇总'],
    ['合约', '买卖', '投保', '价格', '手数', '成交额', '', '开平', '手续费', '平仓盈亏'],
    ['合计'],
    [''],
    ['期权成交汇总'],
]

# 用户真实列顺序
USER_OPT_HEADER = ['合约', '标的', '类型', '买卖', '行权价', '权利金价格',
                   '手数', '平仓盈亏', '手续费']
USER_OPT_ROW = ['IO2611-P-3900', 'IO2611', '看跌期权', '买',
                '3,900.00', '7.00', '1', '+0.00', '15.01']


# ============================================================
# 1. 数值解析
# ============================================================

def test_to_float():
    cases = {
        '3,900.00': 3900.0,
        '-6,000.00': -6000.0,
        '+0.00': 0.0,
        '1,234,567.89': 1234567.89,
        '(1,234.5)': -1234.5,          # 括号负数
        '１，２３４．５': 1234.5,        # 全角
        '15.01元': 15.01,              # 带单位
        '--': 0.0,
        '-': 0.0,
        '—': 0.0,
        '': 0.0,
        None: 0.0,
        8000: 8000.0,
        '7': 7.0,
    }
    bad = []
    for raw, want in cases.items():
        got = _to_float(raw)
        if abs(got - want) > 1e-9:
            bad.append('%r: 期望 %s, 实际 %s' % (raw, want, got))
    assert not bad, '数值解析错误:\n  ' + '\n  '.join(bad)
    print(f'  ✓ {len(cases)} 种数值写法都能正确解析（含千分位/括号负数/全角）')


# ============================================================
# 2. 用户真实列顺序
# ============================================================

def test_user_layout():
    parsed = _parse_rows(HEAD + [USER_OPT_HEADER, USER_OPT_ROW, ['合计']])
    assert len(parsed['options_trades']) == 1, parsed['options_trades']
    t = parsed['options_trades'][0]

    assert t['direction'] == '买', f"方向应为「买」, 实际 {t['direction']!r}"
    assert t['strike'] == 3900.0, t['strike']
    assert t['premium_price'] == 7.0, t['premium_price']
    assert t['volume'] == 1.0, t['volume']
    assert t['multiplier'] == 100.0, t['multiplier']
    assert abs(t['commission'] - 15.01) < 1e-9, t['commission']
    # 结算单没有「权利金总额」列 → 用 单价×手数×乘数 补算，买方为支出(负)
    assert abs(t['premium_total'] + 700.0) < 1e-6, t['premium_total']
    assert t['symbol'] == 'IO'
    assert parsed['multiplier_warnings'] == []
    print('  ✓ 真实列顺序解析正确：方向=买、行权价=3900、权利金=-700（自动补算）')

    # 端到端：到期作废 → 已实现盈亏 -700
    parsed['date'] = '2026-11-20'
    eng = ReviewEngine(make_loader({'2026-11-20': parsed}))
    res = eng.query_by_date_range('2026-11-20', '2026-11-20')
    assert abs(res['overview']['期权盈亏(元)'] + 700.0) < 1e-6, res['overview']
    assert res['unparsed_option_rows'] == [], res['unparsed_option_rows']
    assert len(res['expiry_settlements']) == 1
    assert abs(res['expiry_settlements'][0]['pnl'] + 700.0) < 1e-6
    print('  ✓ 端到端：到期作废结算 -700 元，且不再出现「方向无法识别」')


# ============================================================
# 3. 旧版列顺序仍兼容
# ============================================================

def test_legacy_layout():
    legacy_header = ['合约', '标的', '类型', '行权价', '买卖',
                     '权利金价格', '手数', '权利金', None, '手续费']
    legacy_row = ['CU2606C80000', 'cu2606', '看涨', 80000, '买方',
                  100, 3, -1500, None, 0]
    parsed = _parse_rows(HEAD + [legacy_header, legacy_row, ['合计']])
    t = parsed['options_trades'][0]
    assert t['direction'] == '买方', t['direction']
    assert t['strike'] == 80000.0, t['strike']
    assert t['multiplier'] == 5.0, t['multiplier']          # 由 1500/(100*3) 反推
    assert abs(t['premium_total'] + 1500.0) < 1e-6, t['premium_total']
    print('  ✓ 旧版列顺序（买卖在后）依然正确解析')


def test_unrecognized_header_fallback():
    """表头认不出来时，回退旧版固定列并留下提示。"""
    rows = HEAD + [
        ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J'],      # 无意义表头
        ['CU2606C80000', 'cu2606', '看涨', 80000, '买方', 100, 3, -1500, None, 0],
        ['合计'],
    ]
    parsed = _parse_rows(rows)
    assert len(parsed['options_trades']) == 1
    assert parsed['options_trades'][0]['direction'] == '买方'
    assert parsed.get('layout_notes'), '回退时应留下 layout_notes 提示'
    print('  ✓ 表头不可识别时回退旧版固定列，并记录 layout_notes')


# ============================================================
# 4. 期货段同样按表头定位
# ============================================================

def test_futures_layout():
    """期货段换成「开平/手续费/平仓盈亏」顺序也能解析。"""
    rows = [
        ['客户期货期权内部资金账户', '', '0072503313361', '', '', '交易日期', '', '2026-11-20'],
        ['上日结存', '', 1000000, '', '', '客户权益', '', 990000],
        [''],
        ['期货成交汇总'],
        ['合约', '买卖', '投保', '价格', '手数', '成交额', '开平', '手续费', '平仓盈亏'],
        ['ag2606', '卖', '投机', '8,100.00', '2', '243,000.00', '平', '10.00', '+1,500.00'],
        ['合计'],
    ]
    parsed = _parse_rows(rows)
    t = parsed['futures_trades'][0]
    assert t['direction'] == '卖', t['direction']
    assert t['open_close'] == '平', t['open_close']
    assert t['price'] == 8100.0, t['price']          # 千分位
    assert t['volume'] == 2.0
    assert t['amount'] == 243000.0
    assert abs(t['realized_pnl'] - 1500.0) < 1e-9, t['realized_pnl']
    assert abs(t['commission'] - 10.0) < 1e-9
    print('  ✓ 期货段按表头定位，千分位与 +号 都正确')


def test_section_title_variants():
    """区块标题不能精确匹配：券商可能写「期权成交明细」「期权成交汇总表」。"""
    for title in ('期权成交明细', '期权成交汇总表', '期权成交'):
        rows = HEAD[:7] + [[title], USER_OPT_HEADER, USER_OPT_ROW, ['合计']]
        parsed = _parse_rows(rows)
        assert len(parsed['options_trades']) == 1, (title, parsed['options_trades'])
        assert parsed['options_trades'][0]['direction'] == '买'
    print('  ✓ 3 种区块标题写法都能识别')

    # 合计行的几种写法都应作为数据结束
    for total in ('合计', '小计', '总计', '合  计'):
        rows = HEAD[:7] + [['期权成交汇总'], USER_OPT_HEADER, USER_OPT_ROW,
                           [total], ['AU2606C500', 'au2606', '看涨', 500, '买', 1, 1, '0', '1']]
        parsed = _parse_rows(rows)
        assert len(parsed['options_trades']) == 1, (total, len(parsed['options_trades']))
    print('  ✓ 4 种合计行写法都能正确终止数据段')


def test_real_header_with_slash():
    """真实结算单表头写的是「买/卖」「开/平」（带斜杠）。

    早期关键字是「买卖」「开平」，匹配不上 → 这两列映射成空列 →
    所有成交行被判为"方向无法识别"，期权盈亏恒为 0、期货开平为空。
    这里锁定：表头规整后必须能匹配。
    """
    import review as _review

    rows = [
        ['客户期货期权内部资金账户', '', '0072503313361', '', '', '交易日期', '', '2026-06-22'],
        ['上日结存', '', 100000, '', '', '客户权益', '', 99000],
        [''],
        ['期货成交汇总'],
        ['合约', '买/卖', '投机（一般）/套保/套利', '成交价', '手数',
         '成交额', '', '开/平', '手续费', '平仓盈亏'],
        ['SN2607', '卖', '一般', 419400.0, 1.0, 419400.0, '', '平', 3.04, 130.0],
        ['合计'],
        [''],
        ['期权成交汇总'],
        ['品种合约', '标的合约', '期权类型', '执行价', '买/卖', '权利金单价',
         '成交量', '权利金', '是否备兑', '手续费'],
        ['IO2606-C-5000', 'IO2606', '看涨期权', 5000.0, '买', 4.0, 2.0, -800.0, '', 30.02],
        ['IO2606-C-5000', 'IO2606', '看涨期权', 5000.0, '卖', 10.0, 1.0, 1000.0, '', 15.01],
        ['合计'],
    ]
    parsed = _parse_rows(rows)

    ft = parsed['futures_trades'][0]
    assert ft['direction'] == '卖', f"期货方向应为「卖」, 实际 {ft['direction']!r}"
    assert ft['open_close'] == '平', f"开平应为「平」, 实际 {ft['open_close']!r}"
    assert ft['realized_pnl'] == 130.0
    print('  ✓ 表头「买/卖」「开/平」正确匹配（期货方向/开平不再为空）')

    ot = parsed['options_trades'][0]
    assert ot['direction'] == '买', f"期权方向应为「买」, 实际 {ot['direction']!r}"
    assert ot['strike'] == 5000.0
    assert ot['volume'] == 2.0
    assert abs(ot['premium_total'] + 800.0) < 1e-6
    assert parsed.get('layout_notes') is None or not parsed['layout_notes']
    print('  ✓ 期权方向=买、执行价=5000、权利金=-800，且无 layout_notes 告警')


def main():
    print('[1] 数值解析')
    test_to_float()

    print('\n[2] 用户真实列顺序')
    test_user_layout()

    print('\n[3] 旧版布局与回退')
    test_legacy_layout()
    test_unrecognized_header_fallback()

    print('\n[4] 期货段')
    test_futures_layout()

    print('\n[5] 区块标题变体')
    test_section_title_variants()

    print('\n[6] 真实表头（买/卖、开/平）')
    test_real_header_with_slash()

    print('\n全部断言通过 ✔')


if __name__ == '__main__':
    main()
