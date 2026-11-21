# -*- coding: utf-8 -*-
"""期权合约乘数 / 盈亏统计口径 回归测试

背景（用户报的问题）：
    原先 OPTION_MULTIPLIER 缺了上期能源（INE）和广期所（GFEX）整组品种，
    并且 get_option_multiplier() 对未收录品种**静默返回 1.0**。
    当结算单的「权利金」列缺失或为 0（反推乘数失败）时，就会按 1.0 计算盈亏：
      - 原油（真实乘数 1000）被缩小 1000 倍
      - 多晶硅（3）、黄金（1000）、铜（5）等同样受影响
    而且用户完全看不出来。

本测试锁定三件事：
    1. 常见期权品种必须在乘数表内，且数值正确
    2. 无法确定乘数时必须产生**告警**，不能静默按 1.0 计算
    3. 胜率/盈亏比在「纯期权亏损品种」上不再错误显示为 ∞

用法：python tests/test_option_multiplier.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import review
from review import (OPTION_MULTIPLIER, compute_pnl_stats, extract_symbol,
                    get_option_multiplier, is_known_multiplier,
                    resolve_multiplier, ReviewEngine)
from test_review_engine import build_day, patched_parse, make_loader


# ============================================================
# 1. 乘数表完整性
# ============================================================

# 真正有期权交易、结算单里可能出现的品种（按交易所分组）
OPTION_BEARING = {
    'CFFEX': ['IO', 'MO', 'HO'],
    'SHFE': ['CU', 'AL', 'ZN', 'PB', 'NI', 'SN', 'AU', 'AG', 'RB',
             'RU', 'BU', 'SP', 'AO', 'BR', 'SS'],
    'INE': ['SC', 'LU', 'NR', 'BC', 'EC'],
    'DCE': ['M', 'Y', 'P', 'C', 'CS', 'A', 'B', 'L', 'V', 'PP', 'EG',
            'EB', 'PG', 'I', 'J', 'JM', 'LH', 'JD', 'LG'],
    'CZCE': ['SR', 'CF', 'TA', 'MA', 'FG', 'RM', 'OI', 'AP', 'CJ', 'PK',
             'SA', 'SF', 'SM', 'UR', 'PX', 'SH', 'PF'],
    'GFEX': ['SI', 'LC', 'PS'],
}


def test_table_completeness():
    missing = []
    for exch, codes in OPTION_BEARING.items():
        for c in codes:
            if c not in OPTION_MULTIPLIER:
                missing.append(f'{exch}/{c}')
    assert not missing, f'以下有期权的品种缺少合约乘数: {missing}'
    print(f'  ✓ 乘数表覆盖 {sum(len(v) for v in OPTION_BEARING.values())} 个期权品种，无遗漏')


def test_table_values():
    """抽查各交易所代表品种的乘数（口径：1 点 = 标的期货交易单位）。"""
    expected = {
        # 用户点名的四个
        'CU': 5,      # 沪铜 5 吨/手
        'AU': 1000,   # 沪金 1000 克/手
        'SC': 1000,   # 原油 1000 桶/手
        'PS': 3,      # 多晶硅 3 吨/手
        # 原先整组缺失的 INE / GFEX
        'LU': 10, 'NR': 10, 'BC': 5, 'EC': 50,
        'SI': 5, 'LC': 1,
        # 原先填错的三个
        'JD': 10,     # 鸡蛋：5 吨/手、报价 元/500千克 → 1 点 = 10 元（原填 5）
        'LG': 90,     # 原木：90 立方米/手（原填 9）
        'TS': 20000,  # 2 年期国债面值 200 万 → 1 点 = 2 万元（原填 10000）
        # 其余抽查
        'IO': 100, 'MO': 100, 'T': 10000, 'TF': 10000, 'TL': 10000,
        'AG': 15, 'NI': 1, 'SN': 1, 'RB': 10, 'SS': 5, 'AO': 20,
        'M': 10, 'I': 100, 'J': 100, 'JM': 60, 'LH': 16, 'FB': 500,
        'SR': 10, 'CF': 5, 'SA': 20, 'SH': 30, 'PF': 5,
    }
    bad = []
    for code, want in expected.items():
        got = OPTION_MULTIPLIER.get(code)
        if got != want:
            bad.append(f'{code}: 期望 {want}, 实际 {got}')
    assert not bad, '乘数取值错误:\n  ' + '\n  '.join(bad)
    print(f'  ✓ 抽查 {len(expected)} 个品种乘数取值全部正确（含 4 个用户点名 + 3 个原值错误）')


def test_symbol_extraction():
    cases = {
        'cu2606C80000': 'CU',
        'AU2606C800': 'AU',
        'sc2606C600': 'SC',
        'ps2606C50000': 'PS',
        'SI2606C10000': 'SI',
        'lc2606C120000': 'LC',
        'IO2606-C-4000': 'IO',
        'TA2605C6000': 'TA',      # 不能把 TA 识别成 T
        'CF2605C15000': 'CF',
        'T2606C100': 'T',
    }
    for contract, want in cases.items():
        got = extract_symbol(contract)
        assert got == want, f'{contract} -> {got}, 期望 {want}'
    print(f'  ✓ {len(cases)} 个合约代码解析正确（含 TA/T、CU/C 这类前缀歧义）')


# ============================================================
# 2. resolve_multiplier 三级回退
# ============================================================

def test_resolve_multiplier():
    # (a) 反推成功且与内置表一致 → 采用整数的表值，不告警
    mult, source, warn = resolve_multiplier('au2606C800', 1000 * 100 * 2, 100, 2)
    assert mult == 1000 and source == 'inferred' and warn is None, (mult, source, warn)
    print('  ✓ 反推=1000 与内置一致 → multiplier=1000, 无告警')

    # (b) 反推与内置表冲突（差异 > 2%）→ 用反推值 + 告警
    mult, source, warn = resolve_multiplier('au2606C800', 1 * 100 * 2, 100, 2)
    assert source == 'inferred' and mult == 1.0
    assert warn and '不一致' in warn, warn
    print(f'  ✓ 反推与内置冲突 → 用反推值 + 告警: {warn[:40]}…')

    # (c) 反推失败 + 表内有 → 用表值
    for contract, want in [('sc2606C600', 1000), ('ps2606C50000', 3), ('au2606C800', 1000)]:
        mult, source, warn = resolve_multiplier(contract, 0, 100, 2)
        assert (mult, source, warn) == (float(want), 'table', None), (contract, mult, source, warn)
    print('  ✓ 反推失败(权利金=0) → 回退内置表（原油 1000 / 多晶硅 3 / 黄金 1000）')

    # (d) 反推失败 + 表内没有 → 1.0 但必须告警
    mult, source, warn = resolve_multiplier('zz2606C100', 0, 100, 2)
    assert mult == review.UNKNOWN_MULTIPLIER and source == 'unknown'
    assert warn and '未知' in warn and 'ZZ' in warn, warn
    print('  ✓ 完全未知品种 → 告警而非静默: ' + warn[:42] + '…')

    # (e) 未知品种但反推成功 → 用反推值，不应告警
    mult, source, warn = resolve_multiplier('zz2606C100', 7 * 100 * 2, 100, 2)
    assert mult == 7 and source == 'inferred' and warn is None, (mult, source, warn)
    print('  ✓ 未知品种但结算单能反推 → 用反推值 7，无告警')

    # (f) 参数异常不崩
    assert resolve_multiplier('cu2606C80000', None, None, None)[1] == 'table'
    assert resolve_multiplier('cu2606C80000', 0, 0, 0)[1] == 'table'
    print('  ✓ 入参缺失/为零时不抛异常')


# ============================================================
# 3. 端到端：原油期权反推失败时不能算成 1 倍
# ============================================================

def test_end_to_end_crude_oil():
    # 结算单「权利金」列全为 0（反推失败），此时必须靠内置表拿到 1000
    d1 = patched_parse(build_day(
        '2026-03-02', [],
        [['sc2606C600', 'sc', '看涨', 600, '买方', 100, 2, 0, None, 10]],
        {'平仓盈亏': 0, '当日手续费': 10, '客户权益': 1000000}))
    mult = d1['options_trades'][0]['multiplier']
    assert mult == 1000, f'原油乘数应为 1000，实际 {mult}（原实现会给 1.0）'
    assert d1['options_trades'][0]['multiplier_source'] == 'table'
    print('  ✓ 权利金缺失时原油取内置乘数 1000（不再是 1.0）')

    d2 = patched_parse(build_day(
        '2026-03-03', [],
        [['sc2606C600', 'sc', '看涨', 600, '卖方', 120, 2, 0, None, 10]],
        {'平仓盈亏': 0, '当日手续费': 10, '客户权益': 1000000}))

    eng = ReviewEngine(make_loader({'2026-03-02': d1, '2026-03-03': d2}))
    res = eng.query_by_date_range('2026-03-02', '2026-03-03')
    # (120-100) × 2 手 × 1000 = +40000
    got = res['overview']['期权盈亏(元)']
    assert abs(got - 40000.0) < 1e-6, f'原油期权盈亏应为 40000，实际 {got}'
    assert res['multiplier_warnings'] == [], res['multiplier_warnings']
    print('  ✓ 原油期权已实现盈亏 = +40000 元（100 点价差 × 2 手 × 1000）')

    # 同一笔如果按旧的 1.0 计算只有 40 元 —— 差了 1000 倍
    print('    （若按错误的乘数 1.0 计算，只会得到 +40 元，误差 1000 倍）')


def test_end_to_end_unknown():
    d1 = patched_parse(build_day(
        '2026-03-02', [],
        [['zz2606C100', 'zz', '看涨', 100, '买方', 50, 1, 0, None, 1]],
        {'平仓盈亏': 0, '当日手续费': 1, '客户权益': 1000}))
    assert d1['multiplier_warnings'], '未知品种必须产生告警'
    assert any('ZZ' in w for w in d1['multiplier_warnings'])
    print('  ✓ 未知品种解析阶段记录告警')

    eng = ReviewEngine(make_loader({'2026-03-02': d1}))
    res = eng.query_by_date_range('2026-03-02', '2026-03-02')
    assert res['multiplier_warnings'], '查询结果必须透出乘数告警'
    assert any('ZZ' in w for w in res['multiplier_warnings'])
    print('  ✓ 查询结果透出乘数告警（前端可据此提示用户）')


def test_missing_multiplier_column():
    """兼容旧缓存：options_trades 里没有 multiplier 字段时不能崩。"""
    parsed = {
        'date': '2026-03-02', 'account': '1',
        'futures_trades': [], 'summary': {},
        'options_trades': [{
            'opt_contract': 'sc2606C600', 'symbol': 'SC', 'underlying': 'sc',
            'opt_type': '看涨', 'strike': 600.0, 'direction': '买方',
            'premium_price': 100.0, 'volume': 2.0,
            'premium_total': -200000.0, 'pnl': -200000.0,
            'commission': 10.0, 'date': '2026-03-02', 'account': '1',
            # 故意不提供 multiplier
        }],
    }
    eng = ReviewEngine(make_loader({'2026-03-02': parsed}))
    matched = eng.get_matched_options_df()
    assert not matched.empty
    assert float(matched['multiplier'].iloc[0]) == 1000.0, matched['multiplier'].tolist()
    print('  ✓ 旧缓存缺 multiplier 字段时按内置表补齐为 1000')


# ============================================================
# 4. 胜率 / 盈亏比口径
# ============================================================

def test_pnl_stats():
    # 全亏：盈亏比必须是 0.00，不能是 ∞
    s = compute_pnl_stats([-9000, -1000])
    assert s['win_rate_text'] == '0.0%', s
    assert s['profit_factor_text'] == '0.00', s
    print('  ✓ 全亏记录 → 盈亏比 0.00（不再是 ∞）')

    # 全赢：无亏损单 → ∞
    s = compute_pnl_stats([100, 200])
    assert s['profit_factor_text'] == '∞', s
    assert s['win_rate_text'] == '100.0%'
    print('  ✓ 全赢记录 → 盈亏比 ∞')

    # 无已平仓记录（纯开仓）→ '-'
    s = compute_pnl_stats([0, 0, 0])
    assert s['win_rate_text'] == '-' and s['profit_factor_text'] == '-', s
    print('  ✓ 纯开仓（已实现=0）不计入 → -')

    # 混合
    s = compute_pnl_stats([300, -100, -100, 0])
    assert s['wins'] == 1 and s['losses'] == 2 and s['closed'] == 3
    assert s['win_rate_text'] == '33.3%', s
    assert s['profit_factor_text'] == '1.50', s   # 300 / 200
    print('  ✓ 混合记录 → 胜率 33.3%、盈亏比 1.50')

    # 空输入
    s = compute_pnl_stats([])
    assert s['closed'] == 0 and s['win_rate_text'] == '-'
    print('  ✓ 空输入安全')


def test_symbol_summary_consistency():
    """纯期权亏损品种：品种汇总的胜率/盈亏比必须与详情口径一致，不能显示 ∞。"""
    d1 = patched_parse(build_day(
        '2026-03-02', [],
        [['MO2606-C-6000', 'MO', '看涨', 6000, '卖方', 50, 3, 15000, None, 6],
         ['MO2606-C-6000', 'MO', '看涨', 6000, '买方', 80, 3, -24000, None, 6]],
        {'平仓盈亏': 0, '当日手续费': 12, '客户权益': 1000000}))

    eng = ReviewEngine(make_loader({'2026-03-02': d1}))
    res = eng.query_by_date_range('2026-03-02', '2026-03-02')
    row = res['symbol_summary'].set_index('品种').loc['MO']

    assert abs(row['期权盈亏(元)'] - (-9000.0)) < 1e-6, row['期权盈亏(元)']
    assert row['盈亏比'] != '∞', f"纯亏损品种不应显示盈亏比 ∞，实际 {row['盈亏比']}"
    assert row['盈亏比'] == '0.00', row['盈亏比']
    assert row['胜率'] == '0.0%', row['胜率']
    print('  ✓ 纯期权亏损品种：胜率 0.0% / 盈亏比 0.00（旧实现是 - / ∞）')


# ============================================================
# 入口
# ============================================================

def main():
    print('[1] 乘数表完整性')
    test_table_completeness()
    test_table_values()
    test_symbol_extraction()

    print('\n[2] resolve_multiplier 三级回退')
    test_resolve_multiplier()

    print('\n[3] 端到端：原油 / 未知品种 / 旧缓存')
    test_end_to_end_crude_oil()
    test_end_to_end_unknown()
    test_missing_multiplier_column()

    print('\n[4] 胜率与盈亏比口径')
    test_pnl_stats()
    test_symbol_summary_consistency()

    print('\n全部断言通过 ✔')


if __name__ == '__main__':
    main()
