# -*- coding: utf-8 -*-
"""筛选评分有效性回测：这套 100 分到底能不能预测后续涨跌？

为什么需要它
------------
筛选器给每个品种打 0~100 分，权重是 level 40 / trend 20 / macd 15 / boll 15 /
volume 10 —— 但这些数字**没有任何实证依据**。不验证就加重指标，只是在给噪声贴金。

这个脚本回答四个问题：

  1. 截面 IC —— 同一个交易日里，分高的品种后续真的涨得多吗？
  2. 分组收益 —— 把评分切成十档，收益是否单调递增？
  3. Top-N 超额 —— 每天取前 N 名，跑赢"全部品种等权"这个基准吗？
  4. 事件研究 —— 实际筛出来（在阈值内）的信号，按方向计盈亏，命中率多少？

再额外给出**各维度单因子 IC**，用来判断哪个权重该加重、哪个是负担。

如何避免未来函数
----------------
在第 T 个交易日做判断时，只允许看到 T 及之前的数据：

  * 把日线切成 ``k[:T+1]`` 再交给 ``evaluate()``，指标本身都是因果的
  * 关键点位口径会剔除切片最后一根（即 T 当日），不会"用今天的最高价判断今天"
  * 前视收益一律用**完整**序列里的 close[T]/close[T+h] 计算，不参与打分

用法::

    python backtest.py                       # 近 480 个交易日、默认参数
    python backtest.py --days 240
    python backtest.py --proximity 3 --lookback 120
    python backtest.py --ntop 15 --out detail.csv
"""
import argparse
import bisect
import os
import sys

import pandas as pd

from market_data import Kline, all_symbols, get_store
from screener import ScreenerConfig, evaluate

HORIZONS = (5, 10, 20)
MIN_BARS = 40          # 少于这么多根 K 线不参与（指标需要预热）
MIN_XS = 8             # 截面 IC 每天至少要有这么多个有效样本


# ============================================================
# 数据准备
# ============================================================

def _slice(kline: Kline, end_idx: int, bars: int) -> Kline:
    """截取 [end_idx-bars+1, end_idx] 这一段，构造新 Kline。

    只保留最近 `bars` 根：一是快很多，二是让滚动类指标（如布林带宽度分位）
    有稳定且一致的回看窗口，不会因为历史长短不同而口径漂移。
    """
    start = max(0, end_idx - bars + 1)
    return Kline(
        code=kline.code,
        dates=kline.dates[start:end_idx + 1],
        open=kline.open[start:end_idx + 1],
        high=kline.high[start:end_idx + 1],
        low=kline.low[start:end_idx + 1],
        close=kline.close[start:end_idx + 1],
        volume=kline.volume[start:end_idx + 1],
    )


def _idx_of(kline: Kline, date: str):
    """二分查找某日期在序列里的下标；没有该日期返回 None。"""
    i = bisect.bisect_left(kline.dates, date)
    if i < len(kline.dates) and kline.dates[i] == date:
        return i
    return None


def load_klines(codes, adjust_rolls=False, adjust_start='2022-06-01'):
    """加载日线。

    adjust_rolls=True 时改用**换月复权**序列：用真实持仓量还原每天的主力合约，
    再按换月比例做后复权。新浪原始的 ``CU0`` 主力连续在换月处有台阶，
    实测换月跳空平均是正常日波动的 2 倍（玻璃/焦煤单次可达 15%），
    不复权会把假跳空当成真实涨跌，严重污染动量类指标。
    """
    if adjust_rolls:
        from contracts import build_adjusted_kline
        out = {}
        total = len(codes)
        for i, code in enumerate(codes, 1):
            k = build_adjusted_kline(code, adjust_start)
            if k is not None and len(k) > 60:
                out[code] = k
            if i % 10 == 0 or i == total:
                print('  复权序列 %d/%d（成功 %d）' % (i, total, len(out)))
        return out

    store = get_store()
    klines = store.load(codes, realtime=False)
    cleaned = {}
    for code, k in klines.items():
        if not k.dates:
            continue
        if getattr(k, 'live', False) and len(k.dates) > 1:
            k = Kline(code=k.code, dates=k.dates[:-1], open=k.open[:-1],
                      high=k.high[:-1], low=k.low[:-1],
                      close=k.close[:-1], volume=k.volume[:-1])
        cleaned[code] = k
    return cleaned


# ============================================================
# 逐日滚动评分
# ============================================================

def collect_rows(klines, config, days, slice_bars, verbose=True):
    """对历史每个交易日跑一遍评分，并记录前视收益。"""
    max_h = max(HORIZONS)
    all_dates = sorted({d for k in klines.values() for d in k.dates})
    usable = all_dates[:-max_h] if len(all_dates) > max_h else []
    test_dates = usable[-days:] if (days and days < len(usable)) else usable

    codes = sorted(klines)
    rows = []
    total = len(test_dates)
    short = 0          # 切片不足 slice_bars 的次数（早期样本窗口不一致）
    for n, date in enumerate(test_dates, 1):
        for code in codes:
            k = klines[code]
            idx = _idx_of(k, date)
            if idx is None or idx + max_h >= len(k) or idx + 1 < MIN_BARS:
                continue
            if idx + 1 < slice_bars:
                short += 1
            try:
                res = evaluate(code, _slice(k, idx, slice_bars), config)
            except Exception:
                continue
            if res is None:
                continue

            close0 = k.close[idx]
            if not close0:
                continue
            row = {
                'date': date,
                'code': code,
                'score': res['score'],
                'direction': res['direction'],
                'setup': res['setup'],
                'dist_high': res['dist_to_high_pct'],
                'dist_low': res['dist_to_low_pct'],
            }
            for key in ('level', 'trend', 'macd', 'boll', 'volume'):
                row['s_' + key] = res['scores'].get(key)
            for h in HORIZONS:
                row['ret%d' % h] = k.close[idx + h] / close0 - 1.0
            rows.append(row)

        if verbose and (n % 40 == 0 or n == total):
            print('  已处理 %d/%d 个交易日，累计样本 %d' % (n, total, len(rows)))

    if short:
        pct = short / max(1, len(rows)) * 100
        print('  ⚠ 有 %d 个样本（%.1f%%）的K线切片不足 %d 根，'
              '其关键点位回看窗口会比其余样本短' % (short, pct, slice_bars))
        if pct > 20:
            print('    占比偏高，建议把数据起点往前推（--adjust-rolls 时默认已回溯到 2022-06）')
    return rows


# ============================================================
# 统计
# ============================================================

def _fmt(v, nd=4, pct=False):
    if v is None or (isinstance(v, float) and (v != v)):
        return '-'
    if pct:
        return '%.2f%%' % (v * 100)
    return ('%.' + str(nd) + 'f') % v


def cross_section_ic(df, col, ret_col):
    """逐日截面 Spearman IC。返回 (mean, std, ir, t, pos_ratio, n_days)。"""
    ics = []
    for _date, g in df.groupby('date'):
        s = g[[col, ret_col]].dropna()
        if len(s) < MIN_XS or s[col].nunique() < 3:
            continue
        ic = s[col].rank().corr(s[ret_col].rank())
        if ic == ic:           # 非 NaN
            ics.append(ic)
    if not ics:
        return None
    n = len(ics)
    mean = sum(ics) / n
    var = sum((x - mean) ** 2 for x in ics) / (n - 1) if n > 1 else 0.0
    std = var ** 0.5
    ir = (mean / std) if std > 0 else None
    t = (mean / (std / n ** 0.5)) if std > 0 and n > 1 else None
    pos = sum(1 for x in ics if x > 0) / n
    return {'mean': mean, 'std': std, 'ir': ir, 't': t, 'pos': pos, 'n': n}


def decile_table(df, col, ret_col, buckets=10):
    """把 col 分成十档，看各档 mean(ret)。"""
    s = df[[col, ret_col]].dropna()
    if len(s) < buckets * 20:
        return None
    try:
        s['bucket'] = pd.qcut(s[col], buckets, labels=False, duplicates='drop')
    except Exception:
        return None
    g = s.groupby('bucket')[ret_col].agg(['mean', 'median', 'count'])
    return g


def topn_excess_within(df, ret_col, ntop, proximity):
    """在**候选集内部**取评分前 N 名，与候选集均值比较。

    为什么这个口径才是对的：筛选器的真实用法是「先按 proximity 过滤出候选，
    再在候选里挑最好的」。用全市场做截面排序会混进大量"离关键点位很远、
    却因为均线/MACD 得分高"的品种，测出来的东西与产品用法不符。

    收益按信号方向计（靠近前高＝做多，靠近前低＝做空）。
    """
    d = df.copy()
    sign = d['direction'].map(lambda x: 1.0 if x == 'long' else -1.0)
    d['signed'] = d[ret_col] * sign
    near = (d['dist_high'] <= proximity) | (d['dist_low'] <= proximity)
    d = d[near]
    if d.empty:
        return None

    tops, bases = [], []
    for _date, g in d.groupby('date'):
        g = g.dropna(subset=['signed', 'score'])
        if len(g) < ntop:
            continue
        # 评分相同用代码兜底，保证结果可复现（不引入随机洗牌噪声）
        top = g.sort_values(['score', 'code'], ascending=[False, True]).head(ntop)
        tops.append(top['signed'].mean())
        bases.append(g['signed'].mean())
    if not tops:
        return None
    n = len(tops)
    diffs = [a - b for a, b in zip(tops, bases)]
    mean = sum(diffs) / n
    std = (sum((x - mean) ** 2 for x in diffs) / (n - 1)) ** 0.5 if n > 1 else 0.0
    t = (mean / (std / n ** 0.5)) if std > 0 and n > 1 else None
    return {
        'n': n, 'top_mean': sum(tops) / n, 'base_mean': sum(bases) / n,
        'mean_excess': mean, 't': t,
        'win_days': sum(1 for x in diffs if x > 0) / n,
    }


def topn_excess(df, ret_col, ntop):
    """逐日取评分前 ntop 名，与当日全体均值比较。"""
    diffs, tops, unis = [], [], []
    for _date, g in df.groupby('date'):
        s = g[['score', ret_col]].dropna()
        if len(s) < MIN_XS:
            continue
        uni = s[ret_col].mean()
        top = s.nlargest(ntop, 'score')[ret_col].mean()
        diffs.append(top - uni)
        tops.append(top)
        unis.append(uni)
    if not diffs:
        return None
    n = len(diffs)
    mean = sum(diffs) / n
    std = (sum((x - mean) ** 2 for x in diffs) / (n - 1)) ** 0.5 if n > 1 else 0.0
    t = (mean / (std / n ** 0.5)) if std > 0 and n > 1 else None
    return {
        'mean_excess': mean, 't': t, 'n': n,
        'top_mean': sum(tops) / n, 'uni_mean': sum(unis) / n,
        'win_days': sum(1 for x in diffs if x > 0) / n,
    }


def event_study(df, ret_col, proximity):
    """实际筛出来的信号：在阈值内 → 按方向计签收收益。"""
    d = df.dropna(subset=['ret' + str(HORIZONS[0])]).copy()
    # 与 require_level=True 的判定一致：离前高或前低在阈值内
    near = (d['dist_high'] <= proximity) | (d['dist_low'] <= proximity)
    ev = d[near].copy()
    if ev.empty:
        return None
    sign = ev['direction'].map(lambda x: 1.0 if x == 'long' else -1.0)
    ev['signed'] = ev[ret_col] * sign
    out = {
        'n': len(ev),
        'hit': float((ev['signed'] > 0).mean()),
        'mean': float(ev['signed'].mean()),
        'median': float(ev['signed'].median()),
        'by_dir': {},
        'by_setup': {},
    }
    for dirn, g in ev.groupby('direction'):
        out['by_dir'][dirn] = {
            'n': len(g), 'hit': float((g['signed'] > 0).mean()),
            'mean': float(g['signed'].mean()),
        }
    for st, g in ev.groupby('setup'):
        out['by_setup'][st] = {
            'n': len(g), 'hit': float((g['signed'] > 0).mean()),
            'mean': float(g['signed'].mean()),
        }
    return out


def _level_score_from_dist(dist, proximity, weight):
    """按 _score_level 的口径，从"距离"反推关键点位得分。

    之所以能这样算：total = level + trend + macd + boll + volume，
    而 level 只通过 `dist` 和 `proximity` 两个量决定。
    所以一次采集、任意 proximity 都能复算出评分，不必重跑指标——
    参数扫描因此从"几分钟一次"变成"几秒一轮"。
    """
    if dist is None:
        return 0.0
    if dist < 0:
        return float(weight)                 # 已突破，满分
    if proximity <= 0:
        return 0.0
    return max(0.0, weight * (1.0 - dist / proximity))


def rescore_with_proximity(df, proximity, weight):
    """用新的 proximity 重新计算综合评分（其余四维沿用原值）。"""
    sub = {k: (df['s_' + k].fillna(0.0)) for k in ('trend', 'macd', 'boll', 'volume')}
    other = sub['trend'] + sub['macd'] + sub['boll'] + sub['volume']
    dist = df['dist_high'].where(df['direction'] == 'long', df['dist_low'])
    level = dist.map(lambda d: _level_score_from_dist(d, proximity, weight))
    return (level + other).round(1)


def sweep_proximity(df, values, weight, ntop):
    """扫描 proximity，看阈值松紧对预测力的影响。"""
    results = []
    for p in values:
        d = df.copy()
        d['score'] = rescore_with_proximity(d, p, weight)
        ic5 = cross_section_ic(d, 'score', 'ret5')
        ic20 = cross_section_ic(d, 'score', 'ret20')
        tn = topn_excess(d, 'ret5', ntop)
        ev = event_study(d, 'ret5', p)
        results.append({'proximity': p, 'ic5': ic5, 'ic20': ic20, 'topn': tn, 'event': ev})
    return results


def print_sweep(df, values, weight, ntop):
    print()
    print('-' * 74)
    print('  参数扫描：关键点位阈值 proximity（其余四维与权重不变）')
    print('-' * 74)

    # 自检：用反推公式重算当前阈值下的评分，应与原评分一致，否则说明公式抄错了
    check = rescore_with_proximity(df, df.attrs.get('proximity', 5.0), weight)
    diff = (check - df['score']).abs().max()
    print('  自检：反推公式与原评分的最大偏差 = %.6f（应为 0）' % diff)

    print()
    print('  %-8s %10s %10s %12s %12s %10s %10s'
          % ('阈值', 'IC(5日)', 'IC(20日)', 'Top20超额', '事件样本', '事件均值', '命中率'))
    for r in sweep_proximity(df, values, weight, ntop):
        ic5, ic20, tn, ev = r['ic5'], r['ic20'], r['topn'], r['event']
        print('  %-8s %10s %10s %12s %12s %10s %10s'
              % ('%.1f%%' % r['proximity'],
                 _fmt(ic5['mean']) if ic5 else '-',
                 _fmt(ic20['mean']) if ic20 else '-',
                 _fmt(tn['mean_excess'] * 100, 2) + '%' if tn else '-',
                 ev['n'] if ev else '-',
                 _fmt(ev['mean'] * 100, 2) + '%' if ev else '-',
                 _fmt(ev['hit'] * 100, 1) + '%' if ev else '-'))
    print()
    print('  解读：事件样本占全样本的比例越小，说明阈值越"精"；')
    print('        若某个阈值下 IC 明显转正且样本仍够多，那就是更合理的设定。')


# ============================================================
# 报告
# ============================================================

def report(rows, config, proximity, ntop, dataset_note=''):
    df = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    if df.empty:
        print('\n没有采集到任何样本，无法回测。')
        return None

    dates = sorted(df['date'].unique())
    print()
    print('=' * 74)
    print('  回测结果%s' % (('  ' + dataset_note) if dataset_note else ''))
    print('=' * 74)
    print('  样本区间 : %s ~ %s（%d 个交易日）' % (dates[0], dates[-1], len(dates)))
    print('  品种数量 : %d' % df['code'].nunique())
    print('  样本总数 : %d 个 (交易日 × 品种)' % len(df))
    print('  参数     : proximity=%.1f%%  lookback=%d  min_score=%.0f'
          % (config.proximity, config.lookback, config.min_score))
    print('  前视期   : %s 个交易日' % ', '.join(str(h) for h in HORIZONS))

    # ---- 1. 截面 IC ----
    print()
    print('-' * 74)
    print('  1. 截面 IC（综合评分 vs 未来收益，Spearman）')
    print('-' * 74)
    print('  %-8s %10s %10s %8s %8s %10s %8s'
          % ('前视期', 'mean IC', 'std', 'IR', 't值', 'IC>0占比', '样本日'))
    for h in HORIZONS:
        st = cross_section_ic(df, 'score', 'ret%d' % h)
        if st is None:
            print('  %-8s %10s' % ('%d日' % h, '样本不足'))
            continue
        print('  %-8s %10.4f %10.4f %8s %8s %9.1f%% %8d'
              % ('%d日' % h, st['mean'], st['std'], _fmt(st['ir'], 3),
                 _fmt(st['t'], 2), st['pos'] * 100, st['n']))

    # ---- 2. 分组 ----
    print()
    print('-' * 74)
    print('  2. 评分分组（十档，看收益是否随分数单调递增）')
    print('-' * 74)
    main_h = HORIZONS[0]
    g = decile_table(df, 'score', 'ret%d' % main_h)
    if g is None:
        print('  样本不足')
    else:
        print('  %-6s %12s %12s %8s' % ('档位', '平均收益', '中位数', '样本'))
        for b, row in g.iterrows():
            print('  %-6s %11.2f%% %11.2f%% %8d'
                  % ('D%d' % (int(b) + 1), row['mean'] * 100,
                     row['median'] * 100, row['count']))

    # ---- 3. Top-N ----
    print()
    print('-' * 74)
    print('  3. 每日取评分前 %d 名 vs 全体等权基准' % ntop)
    print('-' * 74)
    print('  %-8s %12s %12s %12s %8s %8s'
          % ('前视期', 'Top-N 均值', '基准均值', '超额', 't值', '胜率'))
    for h in HORIZONS:
        r = topn_excess(df, 'ret%d' % h, ntop)
        if r is None:
            print('  %-8s %12s' % ('%d日' % h, '样本不足'))
            continue
        print('  %-8s %11.2f%% %11.2f%% %11.2f%% %8s %7.1f%%'
              % ('%d日' % h, r['top_mean'] * 100, r['uni_mean'] * 100,
                 r['mean_excess'] * 100, _fmt(r['t'], 2), r['win_days'] * 100))

    # ---- 3b. 候选集内部排序（真实用法）----
    print()
    print('-' * 74)
    print('  3b. 候选集内部取前 %d 名 vs 候选集均值（这才是筛选器的真实用法）' % ntop)
    print('-' * 74)
    print('  %-8s %12s %12s %12s %8s %8s'
          % ('前视期', 'Top-N 均值', '候选集均值', '超额', 't值', '胜率'))
    for h in HORIZONS:
        r = topn_excess_within(df, 'ret%d' % h, ntop, proximity)
        if r is None:
            print('  %-8s %12s' % ('%d日' % h, '样本不足'))
            continue
        print('  %-8s %11.2f%% %11.2f%% %11.2f%% %8s %7.1f%%'
              % ('%d日' % h, r['top_mean'] * 100, r['base_mean'] * 100,
                 r['mean_excess'] * 100, _fmt(r['t'], 2), r['win_days'] * 100))
    print('  注：此处按信号方向计收益（靠近前高＝做多）。同分用代码兜底，未做随机破平；')
    print('      若两个方案的差距很小（<0.1pp），需要用共用破平的配对检验才能判定优劣。')

    # ---- 4. 事件研究 ----
    print()
    print('-' * 74)
    print('  4. 事件研究：实际筛出的信号（距前高/前低 ≤ %.1f%%）' % proximity)
    print('-' * 74)
    for h in (HORIZONS[0], HORIZONS[-1]):
        ev = event_study(df, 'ret%d' % h, proximity)
        if ev is None:
            print('  %d日：样本不足' % h)
            continue
        print('  【%d日后】样本 %d 个，按方向计收益 → 均值 %.2f%%，中位数 %.2f%%，命中率 %.1f%%'
              % (h, ev['n'], ev['mean'] * 100, ev['median'] * 100, ev['hit'] * 100))
        for k in sorted(ev['by_dir']):
            d = ev['by_dir'][k]
            label = '靠近前高(做多方向)' if k == 'long' else '靠近前低(做空方向)'
            print('      %-18s n=%-5d 均值 %+.2f%%  命中率 %.1f%%'
                  % (label, d['n'], d['mean'] * 100, d['hit'] * 100))
        order = {'broken': '已突破', 'approaching': '逼近', 'watch': '接近'}
        for k in ('broken', 'approaching', 'watch'):
            if k in ev['by_setup']:
                d = ev['by_setup'][k]
                print('      形态 %-12s n=%-5d 均值 %+.2f%%  命中率 %.1f%%'
                      % (order.get(k, k), d['n'], d['mean'] * 100, d['hit'] * 100))

    # ---- 5. 各维度单因子 ----
    print()
    print('-' * 74)
    print('  5. 各维度单因子 IC（%d 日前视）—— 判断哪一维真有用' % main_h)
    print('-' * 74)
    dims = [('s_level', 'level (关键点位)', config.weights.get('level')),
            ('s_trend', 'trend (均线)', config.weights.get('trend')),
            ('s_macd', 'macd', config.weights.get('macd')),
            ('s_boll', 'boll (布林)', config.weights.get('boll')),
            ('s_volume', 'volume (量能)', config.weights.get('volume')),
            ('score', '综合评分', None)]
    print('  %-22s %8s %10s %8s %8s' % ('维度', '权重', 'mean IC', 'IR', 't值'))
    for col, label, w in dims:
        st = cross_section_ic(df, col, 'ret%d' % main_h)
        if st is None:
            print('  %-22s %8s %10s' % (label, w if w is not None else '-', '样本不足'))
            continue
        print('  %-22s %8s %10.4f %8s %8s'
              % (label, w if w is not None else '-', st['mean'],
                 _fmt(st['ir'], 2), _fmt(st['t'], 2)))

    print()
    print('-' * 74)
    print('  读表提示')
    print('-' * 74)
    print('  * |mean IC| < 0.02 基本等于没有预测力；0.02~0.05 偏弱；> 0.05 已算可用')
    print('  * |t值| > 2 才谈得上统计显著；持有期重叠时实际显著性会低于名义值')
    print('  * 十档收益若不是单调递增，说明该分数更像"噪声排名"而非强弱排序')
    print('  * 未计交易成本与滑点；期货主力连续换月跳空未做复权')
    return df


def main():
    ap = argparse.ArgumentParser(description='筛选评分有效性回测')
    ap.add_argument('--days', type=int, default=480, help='回测最近多少个交易日（默认 480）')
    ap.add_argument('--proximity', type=float, default=5.0, help='距关键点位阈值%%（默认 5）')
    ap.add_argument('--lookback', type=int, default=250, help='关键点位回看交易日（默认 250）')
    ap.add_argument('--ntop', type=int, default=20, help='Top-N 组合只数（默认 20）')
    ap.add_argument('--slice-bars', type=int, default=0,
                    help='每次评分使用的K线根数（默认 max(lookback+100, 400)）')
    ap.add_argument('--symbols', default='', help='只测这些品种，逗号分隔')
    ap.add_argument('--out', default='', help='把逐条明细导出为 CSV')
    ap.add_argument('--from-csv', default='',
                    help='跳过采集，直接分析已导出的明细 CSV（配合 --sweep 秒级出结果）')
    ap.add_argument('--sweep', default='',
                    help='扫描 proximity，如 1,2,3,5,8')
    ap.add_argument('--adjust-rolls', action='store_true',
                    help='使用换月复权序列（用真实持仓量还原主力，去除换月假跳空）')
    args = ap.parse_args()

    slice_bars = args.slice_bars or max(args.lookback + 100, 400)

    config = ScreenerConfig(
        proximity=args.proximity,
        lookback=args.lookback,
        min_score=0.0,
        require_level=False,      # 全员打分，才能做截面排序比较
        direction='all',
        symbols=None,
    )

    # ---- 直接分析已有明细 ----
    if args.from_csv:
        src = pd.read_csv(args.from_csv)
        print('从 CSV 载入 %d 行：%s' % (len(src), os.path.abspath(args.from_csv)))
        src.attrs['proximity'] = args.proximity
        report(src, config, args.proximity, args.ntop)
        if args.sweep:
            values = [float(x) for x in args.sweep.split(',') if x.strip()]
            print_sweep(src, values, config.weights['level'], args.ntop)
        return 0

    if args.symbols:
        codes = [c.strip().upper() for c in args.symbols.split(',') if c.strip()]
    else:
        codes = [m.code for m in all_symbols()]

    print('加载 %d 个品种的日线…' % len(codes))
    klines = load_klines(codes, adjust_rolls=args.adjust_rolls)
    print('  成功加载 %d 个品种（%s）'
          % (len(klines), '换月复权' if args.adjust_rolls else '未复权，主力连续原始序列'))
    if not klines:
        print('没有可用数据，退出')
        return 1

    # 覆盖率提醒：主力连续换月较早的品种可能历史很短
    short = [(c, len(k)) for c, k in klines.items() if len(k) < args.lookback + 60]
    if short:
        print('  注意：%d 个品种历史不足 %d 根，关键点位回看会被截断'
              % (len(short), args.lookback + 60))

    print('开始滚动回测（每个交易日只喂当日及之前的数据）…')
    rows = collect_rows(klines, config, args.days, slice_bars)
    if not rows:
        print('没有采集到样本')
        return 1

    df = report(rows, config, args.proximity, args.ntop)

    if df is not None and args.sweep:
        values = [float(x) for x in args.sweep.split(',') if x.strip()]
        df.attrs['proximity'] = args.proximity
        print_sweep(df, values, config.weights['level'], args.ntop)

    if args.out and df is not None:
        df.to_csv(args.out, index=False, encoding='utf-8-sig')
        print('\n明细已导出: %s（%d 行）' % (os.path.abspath(args.out), len(df)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
