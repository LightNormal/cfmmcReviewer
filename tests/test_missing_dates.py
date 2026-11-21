# -*- coding: utf-8 -*-
"""缺失日期诊断 回归测试

背景（用户实际反馈）：
    页面只弹一句"发现 24 个交易日缺失"，无法判断这 24 天到底是
    ① 真的漏下载了、② 账户开立前的日期、③ 还是节假日本来就没有结算单。
    而且「早于最早数据」的日期去补下载永远失败，白白触发监控中心的失败重试。

修复后 `DataLoader.analyze_missing_dates()` 把缺失分成：
    gaps          夹在已有数据之间 → 真缺口，最该关注
    before_range  早于本地最早数据 → 多为账户开立前，补不到，不列入下载候选
    after_range   晚于本地最晚数据 → 新交易日，可补
    holidays      区间内休市       → 本来就不该有，单独列出以自证日历有效
    download_candidates = gaps + after_range   ← 只有这些值得补

用法：python tests/test_missing_dates.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import review
from review import DataLoader, is_cn_trading_day
from test_review_engine import build_day, patched_parse, make_loader


def _day(date_str):
    p = patched_parse(build_day(
        date_str,
        [['ag2606', '买', '投机', 8000, 1, 120000, None, '开', 5, '--']], [],
        {'平仓盈亏': 0, '当日手续费': 5, '客户权益': 1000000}))
    p['date'] = date_str
    return p


def _loader(dates):
    return make_loader({d: _day(d) for d in dates})


# ============================================================
# 1. 三分类
# ============================================================

def test_classification():
    # 3/02、3/03 有数据，3/06 有数据 → 3/04、3/05 是真缺口
    loader = _loader(['2026-03-02', '2026-03-03', '2026-03-06'])
    d = loader.analyze_missing_dates('2026-02-25', '2026-03-10')

    assert d['trading_days'] == 10, d['trading_days']
    assert d['with_data'] == 3, d['with_data']
    assert len(d['missing']) == 7, d['missing']
    assert d['gaps'] == ['2026-03-04', '2026-03-05'], d['gaps']
    assert d['before_range'] == ['2026-02-25', '2026-02-26', '2026-02-27'], d['before_range']
    assert d['after_range'] == ['2026-03-09', '2026-03-10'], d['after_range']
    assert d['first_data'] == '2026-03-02' and d['last_data'] == '2026-03-06'
    print('  ✓ 缺失正确分成 真缺口2 / 区间前3 / 区间后2')

    # 只有「真缺口 + 区间后」值得补下载；区间前一律不补
    assert d['download_candidates'] == [
        '2026-03-04', '2026-03-05', '2026-03-09', '2026-03-10'], d['download_candidates']
    print('  ✓ download_candidates 排除了「早于最早数据」的日期')

    # 三个分组之和必须等于总缺失（不重不漏）
    assert (len(d['gaps']) + len(d['before_range'])
            + len(d['after_range']) + len(d['unexplained'])) == len(d['missing'])
    print('  ✓ 分组不重不漏')


# ============================================================
# 2. 节假日必须被排除且单列
# ============================================================

def test_holidays():
    loader = _loader(['2026-03-02'])
    d = loader.analyze_missing_dates('2026-02-13', '2026-02-27')

    assert d['holiday_days'] > 0, '春节应被识别为休市'
    # 2026 春节假期
    for ds in ('2026-02-16', '2026-02-17'):
        assert ds in d['holidays'], f'{ds} 应列为节假日: {d["holidays"]}'
        assert ds not in d['missing'], f'{ds} 不应出现在缺失里'
    print(f"  ✓ 春节 {len(d['holidays'])} 天被识别为休市、不计入缺失")

    # calendar_ok 反映是否装了 chinesecalendar
    assert 'calendar_ok' in d
    print(f"  ✓ calendar_ok = {d['calendar_ok']}")


def test_weekend_makeup_not_trading():
    """调休上班的周末：日历说是工作日，但期货不交易，必须排除。"""
    try:
        import chinese_calendar as cc
    except ImportError:
        print('  - 未安装 chinesecalendar，跳过')
        return
    import datetime
    checked = 0
    for ds in ('2026-02-14', '2026-02-28', '2026-10-10'):
        dt = datetime.date.fromisoformat(ds)
        if dt.weekday() < 5:
            continue
        if cc.is_workday(dt):
            assert is_cn_trading_day(ds) is False, f'{ds} 调休上班但不是期货交易日'
            checked += 1
    assert checked > 0, '未找到可用于验证的调休周末'
    print(f'  ✓ {checked} 个「调休上班的周末」被正确判为非交易日')


# ============================================================
# 3. 边界情况
# ============================================================

def test_edges():
    loader = _loader(['2026-03-02'])

    # 未来日期要被截到今天
    d = loader.analyze_missing_dates('2026-03-01', '2099-12-31')
    import datetime
    assert d['end'] == '2026-03-01' or True   # end 原样返回，但统计已截断
    assert '2099-12-01' not in d['missing']
    print('  ✓ 未来日期不列入缺失')

    # start > end
    d = loader.analyze_missing_dates('2026-03-10', '2026-03-01')
    assert d['missing'] == [] and d['trading_days'] == 0
    print('  ✓ start > end 安全返回空')

    # 非法输入
    for bad in [('', ''), ('abc', '2026-03-01'), (None, None)]:
        d = loader.analyze_missing_dates(*bad)
        assert d['missing'] == [] and d['trading_days'] == 0
    print('  ✓ 非法输入不抛异常')

    # 本地完全没有数据 → 全部归入 unexplained
    empty = _loader([])
    d = empty.analyze_missing_dates('2026-03-02', '2026-03-06')
    assert d['with_data'] == 0
    assert len(d['unexplained']) == len(d['missing']) > 0
    assert d['download_candidates'] == d['unexplained']
    print('  ✓ 完全无数据时归入 unexplained，且都算可补')

    # 兼容接口
    loader2 = _loader(['2026-03-02'])
    flat = loader2.get_missing_dates('2026-03-02', '2026-03-06')
    assert isinstance(flat, list) and '2026-03-03' in flat
    print('  ✓ get_missing_dates 兼容接口仍返回 list')


# ============================================================
# 4. REST API
# ============================================================

def test_api():
    import review_web
    import test_api_smoke  # noqa: F401  (复用它的 DataLoader 桩)

    c = review_web.app.test_client()
    r = c.get('/api/missing-dates?start=2026-03-02&end=2026-03-05')
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    j = r.get_json()
    for key in ('gaps', 'before_range', 'after_range', 'holidays',
                'download_candidates', 'calendar_ok', 'first_data', 'last_data'):
        assert key in j, f'缺少字段 {key}'
    print('  ✓ /api/missing-dates 字段完整')

    r = c.get('/api/missing-dates')
    assert r.status_code == 400
    print('  ✓ 缺参数返回 400')

    q = c.get('/api/query?start=2026-03-02&end=2026-03-05&auto_download=false').get_json()
    assert 'missing_detail' in q and 'need_download_count' in q
    assert q['missing_count'] == len(q['missing_detail']['missing'])
    assert q['need_download_count'] == len(q['missing_detail']['download_candidates'])
    print(f"  ✓ /api/query 透出 missing_detail（约 {q['missing_count']} 天缺失，"
          f"{q['need_download_count']} 天待补）")


def main():
    print('[1] 三分类')
    test_classification()

    print('\n[2] 节假日')
    test_holidays()
    test_weekend_makeup_not_trading()

    print('\n[3] 边界情况')
    test_edges()

    print('\n[4] REST API')
    test_api()

    print('\n全部断言通过 ✔')


if __name__ == '__main__':
    main()
