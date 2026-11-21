# -*- coding: utf-8 -*-
"""期权到期作废结算 回归测试

背景（用户实际遇到的问题）：
    2026-11-20 开仓的沪深300期权（IO2611）当天到期作废。
    虚值到期**不会产生任何平仓成交行**，持仓只是从持仓汇总里消失。
    而 FIFO 只吃成交行，于是这笔仓位一直挂在"未平仓"上，已实现盈亏记 0 ——
    买方付出的权利金（全额亏损）在复盘里凭空消失。

修复思路：
    按合约代码推算到期日，把到期后仍未平仓的头寸强制按 0 价格结算：
      买方 盈亏 = -(开仓价 × 手数 × 乘数)
      卖方 盈亏 = +(开仓价 × 手数 × 乘数)

顺带修掉的既有 bug：
    查询窗口内没有任何期权成交时，原实现返回**全历史**匹配结果，
    导致查询一个与数据不相交的窗口（数据只有 3 月却查 2027 年）仍会显示
    全历史的期权盈亏。

用法：python tests/test_option_expiry.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from review import (ReviewEngine, is_option_contract, option_expiry_date,
                    parse_option_contract)
from test_review_engine import build_day, patched_parse, make_loader


# ============================================================
# 1. 合约解析与到期日推算
# ============================================================

def test_parse_contract():
    cases = [
        ('IO2611-C-4000', ('IO', 2026, 11, 'C', 4000.0)),
        ('MO2611-P-6000', ('MO', 2026, 11, 'P', 6000.0)),
        ('cu2606C80000', ('CU', 2026, 6, 'C', 80000.0)),
        ('sc2611C600', ('SC', 2026, 11, 'C', 600.0)),
        ('SR2701C6000', ('SR', 2027, 1, 'C', 6000.0)),
        ('IO2611C4000', ('IO', 2026, 11, 'C', 4000.0)),   # 无连字符也能解析
    ]
    for contract, want in cases:
        got = parse_option_contract(contract)
        assert got is not None, contract
        assert (got['symbol'], got['year'], got['month'], got['cp'], got['strike']) == want, \
            f'{contract} -> {got}'
    # 期货合约 / 非法输入
    for bad in ['ag2606', 'IF2606', '', None, 'IO2613-C-4000']:
        assert parse_option_contract(bad) is None, bad
        assert is_option_contract(bad) is False
    assert is_option_contract('IO2611-C-4000') is True
    print(f'  ✓ {len(cases)} 个期权合约解析正确；期货与非法输入识别为 None')


def test_cffex_expiry():
    """中金所：股指期权=第三个星期五，国债期权=第二个星期五。"""
    assert option_expiry_date('IO2611-C-4000').isoformat() == '2026-11-20'
    assert option_expiry_date('IO2612-C-4000').isoformat() == '2026-12-18'
    assert option_expiry_date('MO2611-C-6000').isoformat() == '2026-11-20'
    assert option_expiry_date('HO2611-C-3000').isoformat() == '2026-11-20'
    assert option_expiry_date('T2612-C-100').isoformat() == '2026-12-11'
    assert option_expiry_date('TS2612-C-100').isoformat() == '2026-12-11'
    # 2026-11-20 是星期五，且是 11 月第三个星期五 —— 正是用户的案例
    import datetime
    assert datetime.date(2026, 11, 20).weekday() == 4
    print('  ✓ 股指期权=第三个星期五（IO2611 → 2026-11-20）')


def test_commodity_expiry():
    """商品期权到期日：各交易所规则不同，不能一刀切。

    曾经的错误实现对所有商品都用「前一个月第 5 个交易日」，
    结果上期所/上期能源/大商所全都算早了（会把仍在交易的合约提前按 0 平掉，
    随后真实平仓又开出一笔反向仓 → 重复计账）。

    锚点来自券商公告（可外部核对）：
      原油 2609 系列期权 → 2026-08-13（标的交割月前第一月的倒数第 13 个交易日）
      原油 2610 系列期权 → 2026-09-11（同上，已隐含中秋假期）
      多晶硅/工业硅 2607 → 2026-06-05（前一个月第 5 个交易日）
    """
    import review

    has_cal = getattr(review, '_HAS_CHINESE_CALENDAR', False)

    if has_cal:
        exact = {
            'sc2609C600': '2026-08-13',    # INE 倒数第13（公告锚点）
            'sc2610C600': '2026-09-11',    # INE 倒数第13（公告锚点，跨中秋）
            'si2607C10000': '2026-06-05',  # GFEX 第5（公告锚点）
            'cu2609C80000': '2026-08-25',  # SHFE 倒数第5
            'm2609C3000': '2026-08-18',    # DCE 第12（2501 起）
            'SR2701C6000': '2026-12-11',   # CZCE 第15日之前倒数第3
        }
        bad = []
        for contract, want in exact.items():
            got = option_expiry_date(contract)
            if str(got) != want:
                bad.append(f'{contract}: 期望 {want}, 实际 {got}')
        assert not bad, '到期日与交易所规则不符:\n  ' + '\n  '.join(bad)
        print(f'  ✓ 6 个商品期权到期日与交易所规则逐日一致（含 INE 公告锚点）')
    else:
        # 无节假日日历时只做结构性校验
        for contract, (py, pm) in [('sc2609C600', (2026, 8)), ('cu2609C80000', (2026, 8)),
                                   ('m2609C3000', (2026, 8)), ('SI2607C10000', (2026, 6))]:
            d = option_expiry_date(contract)
            assert d is not None and (d.year, d.month) == (py, pm), f'{contract} -> {d}'
        print('  ✓ 商品期权到期日都落在「前一个月」内（未装节假日日历，仅做结构校验）')

    # 规则差异必须体现出来：同一个交割月，INE(倒数13) 早于 SHFE(倒数5)，
    # 而 DCE(第12) 晚于 GFEX(第5)
    ine = option_expiry_date('sc2609C600')
    shfe = option_expiry_date('cu2609C80000')
    dce = option_expiry_date('m2609C3000')
    gfex = option_expiry_date('si2609C10000')
    assert ine < shfe, f'INE 倒数第13 应早于 SHFE 倒数第5: {ine} vs {shfe}'
    assert gfex < dce, f'GFEX 第5 应早于 DCE 第12: {gfex} vs {dce}'
    print(f'  ✓ 规则差异正确体现：INE {ine} < SHFE {shfe}；GFEX {gfex} < DCE {dce}')

    # 回归：上期所绝不能再被算成「第 5 个交易日」
    import datetime
    aug = [d for d in _trading_days(2026, 8)]
    assert shfe != aug[4], 'SHFE 不应使用「第 5 个交易日」规则（旧 bug）'
    assert shfe == aug[-5], f'SHFE 应为倒数第 5 个交易日, 实际 {shfe}'
    print('  ✓ 上期所不再误用「第 5 个交易日」（旧 bug 回归）')


def _trading_days(year, month):
    """测试用：取某月交易日（复用 review 的内部实现）。"""
    import review
    return review._trading_days_in_month(year, month)


def test_unknown_rule_not_settled():
    """未收录到期日规则的品种（如集运欧线）不得猜测结算。"""
    import review
    assert option_expiry_date('ec2606C2000') is None
    assert review.option_expiry_known('ec2606C2000') is False
    assert review.option_expiry_known('cu2606C80000') is True

    d = patched_parse(build_day('2026-06-10', [], [
        ['ec2606C2000', 'ec', '看涨', 2000, '买方', 30, 2, -3000, None, 3]],
        {'平仓盈亏': 0, '当日手续费': 3, '客户权益': 1000000}))
    eng = ReviewEngine(make_loader({'2026-06-10': d}))
    res = eng.query_by_date_range('2026-06-01', '2026-06-30')
    assert res['expiry_settlements'] == [], res['expiry_settlements']
    assert eng.option_open_positions['ec2606C2000']['long'] == 2.0
    assert any('ec2606C2000' in x for x in res['expiry_unknown']), res['expiry_unknown']
    print('  ✓ 未收录规则的品种不结算，并给出人工核对提示')


# ============================================================
# 2. 到期作废结算
# ============================================================

def _buy_expire_day(contract='IO2611-C-4000', date='2026-11-20'):
    """买方开仓、当日到期作废（无平仓成交行）。"""
    return patched_parse(build_day(date, [], [
        [contract, 'IO', '看涨', 4000, '买方', 50, 2, -10000, None, 10]],
        {'平仓盈亏': 0, '当日手续费': 10, '客户权益': 1000000}))


def test_buyer_expires_worthless():
    d = _buy_expire_day()
    eng = ReviewEngine(make_loader({'2026-11-20': d}))
    res = eng.query_by_date_range('2026-11-20', '2026-11-20')

    # 买 2 手 @50 点 × 乘数 100 = 10000 元权利金，全部亏损
    assert abs(res['overview']['期权盈亏(元)'] - (-10000.0)) < 1e-6, res['overview']
    assert abs(res['overview']['期权权利金收支(元)'] - (-10000.0)) < 1e-6

    # 期末不再有未平仓
    assert eng.option_open_positions['IO2611-C-4000'] == {'long': 0.0, 'short': 0.0}

    # 到期结算被记录并可透出到前端
    ex = res['expiry_settlements']
    assert len(ex) == 1, ex
    assert ex[0]['contract'] == 'IO2611-C-4000'
    assert ex[0]['date'] == '2026-11-20'
    assert ex[0]['side'] == '买方'
    assert abs(ex[0]['pnl'] - (-10000.0)) < 1e-6
    print('  ✓ 买方到期作废：已实现盈亏 -10000 元（原实现为 0，亏损消失）')
    print(f'    结算记录: {ex[0]}')


def test_seller_expires_worthless():
    d = patched_parse(build_day('2026-11-20', [], [
        ['IO2611-P-3800', 'IO', '看跌', 3800, '卖方', 40, 3, 12000, None, 6]],
        {'平仓盈亏': 0, '当日手续费': 6, '客户权益': 1000000}))
    eng = ReviewEngine(make_loader({'2026-11-20': d}))
    res = eng.query_by_date_range('2026-11-20', '2026-11-20')
    # 卖方收 3 手 @40 点 × 100 = 12000 元权利金，作废后全数落袋
    assert abs(res['overview']['期权盈亏(元)'] - 12000.0) < 1e-6, res['overview']
    assert res['expiry_settlements'][0]['side'] == '卖方'
    print('  ✓ 卖方到期作废：已实现盈亏 +12000 元（权利金落袋）')


def test_not_yet_expired():
    """数据还没走到到期日 → 不应强制结算。"""
    d = patched_parse(build_day('2026-11-20', [], [
        ['IO2612-C-4000', 'IO', '看涨', 4000, '买方', 50, 2, -10000, None, 10]],
        {'平仓盈亏': 0, '当日手续费': 10, '客户权益': 1000000}))
    eng = ReviewEngine(make_loader({'2026-11-20': d}))
    res = eng.query_by_date_range('2026-11-20', '2026-11-20')
    assert abs(res['overview']['期权盈亏(元)']) < 1e-9, res['overview']
    assert res['expiry_settlements'] == []
    assert eng.option_open_positions['IO2612-C-4000'] == {'long': 2.0, 'short': 0.0}
    print('  ✓ 未到期的 IO2612 保持未平仓，不误结算')


def test_safety_net_trades_after_expiry():
    """安全网：若合约在推算的到期日之后仍有成交，说明算错了到期日，不许结算。"""
    d1 = patched_parse(build_day('2026-05-06', [], [
        ['cu2606C80000', 'cu', '看涨', 80000, '买方', 100, 1, -500, None, 1]],
        {'平仓盈亏': 0, '当日手续费': 1, '客户权益': 1000000}))
    d2 = patched_parse(build_day('2026-06-10', [], [
        ['cu2606C80000', 'cu', '看涨', 80000, '买方', 120, 1, -600, None, 1]],
        {'平仓盈亏': 0, '当日手续费': 1, '客户权益': 1000000}))
    eng = ReviewEngine(make_loader({'2026-05-06': d1, '2026-06-10': d2}))
    res = eng.query_by_date_range('2026-05-06', '2026-06-10')
    assert res['expiry_settlements'] == [], res['expiry_settlements']
    assert eng.option_open_positions['cu2606C80000']['long'] == 2.0
    print('  ✓ 到期日之后仍有成交 → 跳过结算（不会把活跃合约强平）')


def test_open_before_window():
    """窗口之前开仓、窗口内到期 → 亏损必须算进这个窗口。"""
    d_open = patched_parse(build_day('2026-10-15', [], [
        ['IO2611-C-4000', 'IO', '看涨', 4000, '买方', 50, 2, -10000, None, 10]],
        {'平仓盈亏': 0, '当日手续费': 10, '客户权益': 1000000}))
    # 11-25 是一个没有任何期权成交的交易日，它只用来表明"数据已经覆盖过到期日"
    d_later = patched_parse(build_day('2026-11-25', [], [],
                                      {'平仓盈亏': 0, '当日手续费': 0,
                                       '客户权益': 1000000}))
    eng = ReviewEngine(make_loader({'2026-10-15': d_open, '2026-11-25': d_later}))
    # 11 月没有任何期权成交，但 IO2611 在 11-20 到期
    res = eng.query_by_date_range('2026-11-01', '2026-11-30')
    assert abs(res['overview']['期权盈亏(元)'] - (-10000.0)) < 1e-6, res['overview']
    assert len(res['expiry_settlements']) == 1

    # 反例：数据没覆盖到到期日时不能擅自结算
    eng2 = ReviewEngine(make_loader({'2026-10-15': d_open}))
    res2 = eng2.query_by_date_range('2026-10-01', '2026-10-31')
    assert res2['expiry_settlements'] == [], '数据未覆盖到期日时不应结算'
    assert eng2.option_open_positions['IO2611-C-4000']['long'] == 2.0
    print('  ✓ 窗口外开仓、窗口内到期 → 亏损计入该窗口；数据未覆盖到期日则不结算')


def test_disjoint_window_is_zero():
    """既有 bug 回归：查询与数据完全不相交的窗口，期权盈亏必须为 0。"""
    d = patched_parse(build_day('2026-03-02', [], [
        ['MO2606-C-6000', 'MO', '看涨', 6000, '卖方', 50, 3, 15000, None, 6],
        ['MO2606-C-6000', 'MO', '看涨', 6000, '买方', 80, 3, -24000, None, 6]],
        {'平仓盈亏': 0, '当日手续费': 12, '客户权益': 1000000}))
    eng = ReviewEngine(make_loader({'2026-03-02': d}))

    in_window = eng.query_by_date_range('2026-03-02', '2026-03-05')
    assert abs(in_window['overview']['期权盈亏(元)'] - (-9000.0)) < 1e-6

    far = eng.query_by_date_range('2027-01-01', '2027-01-31')
    assert far['overview']['交易日数'] == 0
    assert abs(far['overview']['期权盈亏(元)']) < 1e-9, \
        f"空窗口不应计入全历史期权盈亏, 实际 {far['overview']['期权盈亏(元)']}"
    assert far['overview']['品种数'] == 0
    print('  ✓ 不相交窗口的期权盈亏为 0（原实现会返回全历史的 -9000）')


# ============================================================
# 3. 汇总与趋势是否包含到期结算
# ============================================================

def test_summary_and_trend_include_expiry():
    d = _buy_expire_day()
    eng = ReviewEngine(make_loader({'2026-11-20': d}))
    res = eng.query_by_date_range('2026-11-20', '2026-11-20')

    sym = res['symbol_summary'].set_index('品种')
    assert abs(sym.loc['IO', '期权盈亏(元)'] - (-10000.0)) < 1e-6, sym.loc['IO']
    assert abs(sym.loc['IO', '总盈亏(元)'] - (-10000.0)) < 1e-6
    # 胜率按已实现盈亏统计：一笔亏损
    assert sym.loc['IO', '胜率'] == '0.0%', sym.loc['IO', '胜率']
    assert sym.loc['IO', '盈亏比'] == '0.00', sym.loc['IO', '盈亏比']
    print('  ✓ 品种汇总计入到期结算（IO 期权盈亏 -10000、胜率 0.0%）')

    trend = res['daily_trend']
    row = trend[trend['date'].astype(str).str[:10] == '2026-11-20']
    assert len(row) == 1
    assert abs(float(row['opt_pnl'].iloc[0]) - (-10000.0)) < 1e-6, row['opt_pnl'].tolist()
    print('  ✓ 每日趋势在到期日计入 -10000')

    matched = eng.get_matched_options_df()
    expiry_rows = matched[matched['direction'] == '到期作废']
    assert len(expiry_rows) == 1, expiry_rows
    assert abs(float(expiry_rows['realized_pnl'].iloc[0]) - (-10000.0)) < 1e-6
    print('  ✓ 明细中出现「到期作废」行（前端可直接展示）')


# ============================================================
# 入口
# ============================================================

def main():
    print('[1] 合约解析与到期日')
    test_parse_contract()
    test_cffex_expiry()
    test_commodity_expiry()
    test_unknown_rule_not_settled()

    print('\n[2] 到期作废结算')
    test_buyer_expires_worthless()
    test_seller_expires_worthless()
    test_not_yet_expired()
    test_safety_net_trades_after_expiry()
    test_open_before_window()
    test_disjoint_window_is_zero()

    print('\n[3] 汇总与趋势')
    test_summary_and_trend_include_expiry()

    print('\n全部断言通过 ✔')


if __name__ == '__main__':
    main()
