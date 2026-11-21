# -*- coding: utf-8 -*-
"""结算单原文诊断（--dump-sheet / /api/debug/sheet）测试

为什么需要这个工具：
    用户报告"IO2611-P-3900 这个期权的盈亏没有计算"。
    但结算单里期权**行权/履约**是否单独成块、列位置如何，在没有真实样本时
    无法可靠推断 —— 尤其是股指期权是现金交割，行权既不是普通平仓成交，
    也可能不出现在「期权成交汇总」里。

    所以先提供只读的诊断：把某天的结算单原文摊开，并自动识别区块标题、
    高亮包含目标合约的行。定位逻辑不猜列位置，因此不会引入错误数据。

用法：python tests/test_sheet_dump.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import review
from review import DataLoader, ReviewEngine
from test_review_engine import FakeSheet, FakeWorkbook, make_loader, patched_parse, build_day


SHEET_ROWS = [
    ['客户期货期权内部资金账户', '', '0072503313361', '', '', '交易日期', '', '2026-11-20'],
    ['上日结存', '', 1000000, '', '', '客户权益', '', 990000],
    [''],
    ['期货成交汇总'],
    ['合约', '买卖', '投保', '价格', '手数', '成交额', '', '开平', '手续费', '平仓盈亏'],
    ['ag2606', '买', '投机', 8000, 1, 120000, '', '开', 5, '--'],
    ['合计'],
    [''],
    ['期权成交汇总'],
    ['合约', '标的', '类型', '行权价', '买卖', '权利金价格', '手数', '权利金', '', '手续费'],
    ['IO2611-P-3900', 'IO', '看跌', 3900, '买方', 30, 2, -6000, '', 10],
    ['合计'],
    [''],
    # 关键：行权/履约区块（真实布局未知，这里模拟一种可能形态）
    ['期权行权汇总'],
    ['合约', '行权价', '标的结算价', '行权数量', '行权盈亏', '', '', '', '', '手续费'],
    ['IO2611-P-3900', 3900, 3880, 2, 4000, '', '', '', '', 0],
    ['合计'],
]


def _patch(loader, wb):
    """让 dump_sheet 读到我们伪造的工作表。"""
    loader.scan_files = lambda: [os.path.join('fake', 'out', 'dw_0072503313361', '日报',
                                              '逐笔', '0072503313361_2026-11-20.xls')]
    orig_excel = review._looks_like_excel
    orig_open = review.xlrd.open_workbook
    review._looks_like_excel = lambda fp: True
    review.xlrd.open_workbook = lambda fp: wb
    return orig_excel, orig_open


def _unpatch(orig_excel, orig_open):
    review._looks_like_excel = orig_excel
    review.xlrd.open_workbook = orig_open


def test_dump_sheet():
    loader = DataLoader.__new__(DataLoader)
    loader.output_dir = 'fake'
    loader.cache = {}
    loader._lock = __import__('threading').RLock()

    wb = FakeWorkbook([FakeSheet(SHEET_ROWS)])
    ox, oo = _patch(loader, wb)
    try:
        # 1) 识别区块标题
        d = loader.dump_sheet('2026-11-20')
        titles = [s['title'] for s in d['sections']]
        assert '期货成交汇总' in titles, titles
        assert '期权成交汇总' in titles, titles
        assert '期权行权汇总' in titles, titles
        print(f'  ✓ 识别到 {len(titles)} 个区块标题，含「期权行权汇总」')

        # 2) 不传 contract 时返回全部行
        assert len(d['rows']) == len(SHEET_ROWS)
        assert d['matches'] == []
        print(f'  ✓ 返回全部 {len(d["rows"])} 行原文')

        # 3) 传 contract 时只回匹配行，且能同时命中成交与行权两处
        d2 = loader.dump_sheet('2026-11-20', 'IO2611-P-3900')
        rows = [m['row'] for m in d2['matches']]
        assert len(rows) == 2, rows          # 成交汇总 1 行 + 行权汇总 1 行
        assert d2['rows'] == []              # 指定合约时不回全部行，避免刷屏
        joined = ' '.join(d2['matches'][1]['cells'])
        assert '行权盈亏' not in joined      # 该行是数据行不是表头
        assert '4000' in joined
        print(f'  ✓ 指定合约后命中 2 行（成交 + 行权），且不刷屏')

        # 4) 合约不存在
        d3 = loader.dump_sheet('2026-11-20', 'AU2612-C-500')
        assert d3['matches'] == []
        print('  ✓ 合约不存在时 matches 为空（不会误报）')

        # 5) 找不到该日期的文件
        d4 = loader.dump_sheet('2026-01-05')
        assert d4.get('error'), d4
        print(f'  ✓ 找不到文件时返回错误: {d4["error"][:30]}…')

        # 6) 行数截断
        d5 = loader.dump_sheet('2026-11-20', max_rows=5)
        assert len(d5['rows']) == 5 and d5['truncated'] is True
        print('  ✓ max_rows 截断生效')
    finally:
        _unpatch(ox, oo)


def test_unparsed_direction_flagged():
    """方向无法识别（行权/履约）时，必须显式暴露，而不是静默记 0。"""
    parsed = {
        'date': '2026-11-20', 'account': '1',
        'futures_trades': [], 'summary': {},
        'options_trades': [
            {'opt_contract': 'IO2611-P-3900', 'symbol': 'IO', 'underlying': 'IO',
             'opt_type': '看跌', 'strike': 3900.0, 'direction': '买方',
             'premium_price': 30.0, 'volume': 2.0, 'multiplier': 100.0,
             'multiplier_source': 'inferred', 'premium_total': -6000.0, 'pnl': -6000.0,
             'commission': 10.0, 'date': '2026-11-20', 'account': '1'},
            # 行权行：方向不是买/卖
            {'opt_contract': 'IO2611-P-3900', 'symbol': 'IO', 'underlying': 'IO',
             'opt_type': '看跌', 'strike': 3900.0, 'direction': '行权',
             'premium_price': 0.0, 'volume': 2.0, 'multiplier': 100.0,
             'multiplier_source': 'table', 'premium_total': 0.0, 'pnl': 0.0,
             'commission': 0.0, 'date': '2026-11-20', 'account': '1'},
        ],
    }
    eng = ReviewEngine(make_loader({'2026-11-20': parsed}))
    res = eng.query_by_date_range('2026-11-20', '2026-11-20')

    rows = res['unparsed_option_rows']
    assert len(rows) == 1, rows
    assert rows[0]['contract'] == 'IO2611-P-3900'
    assert rows[0]['direction'] == '行权'
    assert rows[0]['volume'] == 2.0
    print('  ✓ 「行权」方向被显式记录并上抛（不再静默丢弃）')

    # 买家买入后到期作废 → 该合约会被到期结算为 -6000
    ex = res['expiry_settlements']
    assert len(ex) == 1 and abs(ex[0]['pnl'] + 6000.0) < 1e-6, ex
    print('  ✓ 同时到期结算为 -6000（说明当前是「按作废处理」）')


def main():
    print('[1] 结算单原文诊断')
    test_dump_sheet()

    print('\n[2] 无法识别的行权方向')
    test_unparsed_direction_flagged()

    print('\n全部断言通过 ✔')


if __name__ == '__main__':
    main()
