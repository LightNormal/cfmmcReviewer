# -*- coding: utf-8 -*-
"""离线单元测试：模拟结算单 worksheet，验证解析与 FIFO 期权盈亏引擎。

不依赖 xlwt / 网络。用 FakeSheet 模拟 xlrd sheet 的
nrows/ncols/cell_value 接口，直接喂给 parse_trade_xls 与 ReviewEngine。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from review import parse_trade_xls, DataLoader, ReviewEngine


class FakeSheet:
    def __init__(self, rows):
        self.rows = rows
        self.nrows = len(rows)
        self.ncols = max((len(r) for r in rows), default=0)

    def cell_value(self, r, c):
        if r < self.nrows and c < len(self.rows[r]):
            v = self.rows[r][c]
            return '' if v is None else v
        return ''


class FakeWorkbook:
    def __init__(self, sheets):
        self.sheets = sheets
        self.nsheets = len(sheets)

    def sheet_by_index(self, i):
        return self.sheets[i]


def build_day(date, futures_rows, options_rows, summary_left):
    """构造一天的结算单表格（按真实布局）

    futures_rows/options_rows: list of tuple，元素为列 0..9 的值（None 跳过）
    summary_left: dict label -> value（写在 c0/c2）
    """
    rows = []
    # 头部资金状况区: 左半 c0/c2, 右半 c5/c7（真实报表布局）
    right_keys = ('客户权益', '保证金占用', '风险度')
    left = {k: v for k, v in summary_left.items() if k not in right_keys}
    right = {k: v for k, v in summary_left.items() if k in right_keys}
    rows.append(['客户期货期权内部资金账户', None, '0072503313361', None, None, '交易日期', None, date])
    left_items = list(left.items())
    right_items = list(right.items())
    for i in range(max(len(left_items), len(right_items), 1)):
        row = [None] * 8
        if i < len(left_items):
            row[0], row[2] = left_items[i]
        if i < len(right_items):
            row[5], row[7] = right_items[i]
        rows.append(row)
    rows.append([None] * 8)
    # 期货成交汇总
    rows.append(['期货成交汇总'])
    rows.append(['合约', '买卖', '投保', '价格', '手数', '成交额', None, '开平', '手续费', '平仓盈亏'])
    for fr in futures_rows:
        rows.append(list(fr))
    rows.append(['合计'])
    rows.append([None])
    # 期权成交汇总
    rows.append(['期权成交汇总'])
    rows.append(['合约', '标的', '类型', '行权价', '买卖', '权利金价格', '手数', '权利金', None, '手续费'])
    for orow in options_rows:
        rows.append(list(orow))
    rows.append(['合计'])
    return FakeWorkbook([FakeSheet(rows)])


def patched_parse(wb):
    """绕过 xlrd.open_workbook，直接喂 FakeWorkbook 进解析主体"""
    import review

    orig_open = review.xlrd.open_workbook
    orig_check = review._looks_like_excel
    review.xlrd.open_workbook = lambda fp: wb
    review._looks_like_excel = lambda fp: True
    try:
        return parse_trade_xls('fake.xls')
    finally:
        review.xlrd.open_workbook = orig_open
        review._looks_like_excel = orig_check


def make_loader(days_map):
    loader = DataLoader.__new__(DataLoader)
    loader.output_dir = 'fake'
    loader.cache = {}
    for d, parsed in days_map.items():
        parsed['date'] = d
        loader.cache[d] = parsed
    return loader


def main():
    # Day1 (窗口外): 买方开仓 IO2606-C-4000 2手@200点 → 权利金 -40000元 (乘数100)
    #                卖方开仓 IO2606-P-3800 1手@100点 → 权利金 +10000元
    d1 = patched_parse(build_day(
        '2026-03-02',
        futures_rows=[],
        options_rows=[
            ['IO2606-C-4000', 'IO', '看涨', 4000, '买方', 200, 2, -40000, None, 10],
            ['IO2606-P-3800', 'IO', '看跌', 3800, '卖方', 100, 1, 10000, None, 5],
        ],
        summary_left={'平仓盈亏': 0, '当日手续费': 15, '客户权益': 1000000}))
    assert d1['options_trades'][0]['pnl'] == -40000, d1['options_trades'][0]
    assert d1['options_trades'][1]['pnl'] == 10000
    # 乘数应从 权利金总额/(单价×手数) 反推为 100
    assert abs(d1['options_trades'][0]['multiplier'] - 100.0) < 1e-9, d1['options_trades'][0]['multiplier']
    assert abs(d1['options_trades'][1]['multiplier'] - 100.0) < 1e-9
    print('✓ 解析: 现金流符号正确 + 乘数反推=100(元/点)')

    # Day2: 期货 ag 开+平; 卖方平 C 2手@260点(+52000); 买方平 P 1手@60点(-6000)
    d2 = patched_parse(build_day(
        '2026-03-03',
        futures_rows=[
            ['ag2606', '买', '投机', 8000, 1, 120000, None, '开', 5, '--'],
            ['ag2606', '卖', '投机', 8100, 1, 121500, None, '平', 5, 1500],
        ],
        options_rows=[
            ['IO2606-C-4000', 'IO', '看涨', 4000, '卖方', 260, 2, 52000, None, 10],
            ['IO2606-P-3800', 'IO', '看跌', 3800, '买方', 60, 1, -6000, None, 5],
        ],
        summary_left={'平仓盈亏': 1500, '当日手续费': 20, '客户权益': 1002000}))
    assert d2['futures_trades'][0]['realized_pnl'] == 0, '开仓 -- 应解析为0'
    assert d2['futures_trades'][1]['realized_pnl'] == 1500
    # 首行期货数据(开仓)必须被解析到 —— 验证 futures_start+1 off-by-one 修复
    assert len(d2['futures_trades']) == 2, f"期货首行丢失! 仅解析到 {len(d2['futures_trades'])} 笔"
    print('✓ 解析: 期货首行数据不再丢失, "--"平仓盈亏归0')

    # Day3: MO 卖方开3手@50点(+15000), 买方平3手@80点(-24000) → 亏 (50-80)*3*100=-9000
    d3 = patched_parse(build_day(
        '2026-03-04',
        futures_rows=[],
        options_rows=[
            ['MO2606-C-6000', 'MO', '看涨', 6000, '卖方', 50, 3, 15000, None, 6],
            ['MO2606-C-6000', 'MO', '看涨', 6000, '买方', 80, 3, -24000, None, 6],
        ],
        summary_left={'平仓盈亏': 0, '当日手续费': 12, '客户权益': 1001000}))

    # Day4: 无成交日（正常空结算单）
    d4 = patched_parse(build_day('2026-03-05', [], [], {'平仓盈亏': 0, '当日手续费': 0, '客户权益': 1001000}))
    assert d4['futures_trades'] == [] and d4['options_trades'] == []
    assert d4['date'] == '2026-03-05' and d4['summary']['客户权益'] == 1001000
    print('✓ 无成交日解析正常，不再报错')

    loader = make_loader({'2026-03-02': d1, '2026-03-03': d2,
                          '2026-03-04': d3, '2026-03-05': d4})
    engine = ReviewEngine(loader)

    # ---- 全范围 FIFO ----
    res = engine.query_by_date_range('2026-03-02', '2026-03-05')
    ov = res['overview']
    # C: (260-200)*2*100 = +12000, P: (100-60)*1*100 = +4000, MO: (50-80)*3*100 = -9000 → +7000
    assert abs(ov['期权盈亏(元)'] - 7000.0) < 1e-6, ov
    # 现金流: (-40000+10000)+(52000-6000)+(15000-24000)=+7000
    assert abs(ov['期权权利金收支(元)'] - 7000.0) < 1e-6, ov
    assert abs(ov['期货盈亏(元)'] - 1500.0) < 1e-6
    assert abs(ov['期权手续费(元)'] - 42.0) < 1e-6
    print(f"✓ FIFO 已实现盈亏(含乘数100): 期权+7000, 期货+1500")

    # ---- 跨窗口：从 Day2 起查，Day1 的开仓窗口外，平仓盈亏仍须正确 ----
    res2 = engine.query_by_date_range('2026-03-03', '2026-03-05')
    assert abs(res2['overview']['期权盈亏(元)'] - 7000.0) < 1e-6, \
        f"跨窗口应+7000, 实际 {res2['overview']['期权盈亏(元)']}"
    # 窗口内期权笔数只含 Day2/Day3
    assert res2['overview']['期权笔数'] == 4
    print('✓ 跨窗口平仓盈亏正确（窗口外开仓也能匹配）')

    # ---- 品种汇总 ----
    sym = res['symbol_summary'].set_index('品种')
    assert abs(sym.loc['IO', '期权盈亏(元)'] - 16000.0) < 1e-6
    assert abs(sym.loc['IO', '总盈亏(元)'] - 16000.0) < 1e-6
    assert abs(sym.loc['AG', '总盈亏(元)'] - 1500.0) < 1e-6
    assert abs(sym.loc['MO', '期权盈亏(元)'] - (-9000.0)) < 1e-6
    print('✓ 品种汇总: IO=+16000 AG=+1500 MO=-9000')

    # ---- 每日趋势按平仓日记账 ----
    trend = res['daily_trend']
    t = {str(r['date'])[:10]: r for _, r in trend.iterrows()}
    assert abs(t['2026-03-02']['opt_pnl'] - 0.0) < 1e-6, '纯开仓日已实现应为0'
    assert abs(t['2026-03-03']['opt_pnl'] - 16000.0) < 1e-6
    assert abs(t['2026-03-04']['opt_pnl'] - (-9000.0)) < 1e-6
    assert abs(t['2026-03-03']['futures_pnl'] - 1500.0) < 1e-6
    print('✓ 每日趋势: 已实现盈亏记在平仓日, 开仓日为0')

    # ---- 逐笔符号（全范围窗口，明细含 Day1 开仓行）----
    engine.query_by_date_range('2026-03-02', '2026-03-05')
    matched = engine.get_matched_options_df()
    m = {(str(r['date'])[:10], r['opt_contract'], r['direction']): r['realized_pnl']
         for _, r in matched.iterrows()}
    assert abs(m[('2026-03-03', 'IO2606-C-4000', '卖方')] - 12000.0) < 1e-6, m
    assert abs(m[('2026-03-03', 'IO2606-P-3800', '买方')] - 4000.0) < 1e-6, m
    assert abs(m[('2026-03-02', 'IO2606-C-4000', '买方')] - 0.0) < 1e-6
    assert abs(m[('2026-03-04', 'MO2606-C-6000', '卖方')] - 0.0) < 1e-6
    assert abs(m[('2026-03-04', 'MO2606-C-6000', '买方')] - (-9000.0)) < 1e-6
    print('✓ 逐笔已实现盈亏符号与记账位置正确')

    # ---- 未平仓 ----
    op = engine.option_open_positions
    assert op['IO2606-C-4000'] == {'long': 0, 'short': 0}
    print('✓ 未平仓统计:', op)

    # ---- 部分平仓(拆单匹配): CU期权乘数5元/点。买3手@100点(-1500元), 卖平2手@120点(+1200元)
    #      已实现 = (120-100)*2*5 = +200, 剩余1手持仓 ----
    e1 = patched_parse(build_day('2026-04-01', [], [
        ['cu2606C80000', 'cu', '看涨', 80000, '买方', 100, 3, -1500, None, 0]], {}))
    e2 = patched_parse(build_day('2026-04-02', [], [
        ['cu2606C80000', 'cu', '看涨', 80000, '卖方', 120, 2, 1200, None, 0]], {}))
    assert abs(e1['options_trades'][0]['multiplier'] - 5.0) < 1e-9, \
        e1['options_trades'][0]['multiplier']
    eng2 = ReviewEngine(make_loader({'2026-04-01': e1, '2026-04-02': e2}))
    r2 = eng2.query_by_date_range('2026-04-01', '2026-04-02')
    assert abs(r2['overview']['期权盈亏(元)'] - 200.0) < 1e-6, r2['overview']
    assert eng2.option_open_positions['cu2606C80000'] == {'long': 1, 'short': 0}
    print('✓ CU拆单部分平仓: 乘数5反推正确, 平2手(+200), 剩余1手持仓')

    # ---- 空窗口 ----
    r3 = engine.query_by_date_range('2026-05-01', '2026-05-31')
    assert r3['overview']['交易日数'] == 0
    print('✓ 空窗口不崩溃')

    print('\n全部断言通过 ✔')


if __name__ == '__main__':
    main()
