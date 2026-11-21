"""
技术指标计算（品种筛选功能的指标层）
====================================
全部基于 pandas 实现，口径对齐国内主流行情软件（通达信 / 文华财经 / 同花顺）：

  指标            参数            说明
  --------------- -------------- --------------------------------------------
  MA              5/10/20/60     简单移动平均，多头/空头排列判断的基础
  EMA             12/26          指数移动平均（adjust=False，递推式）
  MACD            (12,26,9)      DIF = EMA12-EMA26, DEA = EMA9(DIF),
                                 MACD 柱 = 2 x (DIF-DEA)；柱值翻倍是国内习惯
  BOLL            (20, 2.0)      中轨 = MA20，上下轨 = 中轨 ± 2 倍总体标准差
                                 (ddof=0，与通达信 STD 口径一致)
  RSI             14             Wilder 平滑
  KDJ             (9,3,3)        RSV 3 日平滑，K/D 初值 50
  ATR             14             Wilder 平滑，真实波幅 H-L / |H-C| / |L-C|
  量比            20             当日成交量 / 前 20 日成交量均量

关键点位口径（重要）：
  「长期高点 / 低点」一律取**不含当日**的前 N 根 K 线区间，
  即 `shift(1).rolling(N)`。若把当日也算进去，当日创新高时距离恒为 0，
  无法区分「正在逼近前高」和「刚刚突破」，因此必须剔除当日。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from market_data import Kline


# ============================================================
# 参数
# ============================================================

@dataclass
class IndicatorParams:
    """指标参数。默认值即国内最通用的参数组合。"""
    ma_periods: tuple = (5, 10, 20, 60)
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    boll_period: int = 20
    boll_std: float = 2.0
    rsi_period: int = 14
    kdj_period: int = 9
    kdj_smooth: int = 3
    atr_period: int = 14
    vol_period: int = 20
    lookback: int = 250          # 关键点位回看周期（交易日）
    ref_start: Optional[str] = None   # 参考区间模式：起始日期
    ref_end: Optional[str] = None     # 参考区间模式：结束日期


# 回看周期预设（交易日 ≈ 自然日 * 0.68）
LOOKBACK_PRESETS = [
    {'label': '近半年', 'bars': 120},
    {'label': '近1年', 'bars': 250},
    {'label': '近2年', 'bars': 500},
    {'label': '近3年', 'bars': 750},
    {'label': '全部历史', 'bars': 0},
]


# ============================================================
# 基础指标
# ============================================================

def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    # 国内软件的 EMA 为纯递推 MA(x, n) = (2*x + (n-1)*prev) / (n+1)，
    # 等价于 ewm(span=n, adjust=False)
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """返回 (DIF, DEA, MACD柱)。MACD 柱已乘 2，符合国内显示习惯。"""
    dif = ema(close, fast) - ema(close, slow)
    dea = dif.ewm(span=signal, adjust=False, min_periods=signal).mean()
    hist = (dif - dea) * 2
    return dif, dea, hist


def boll(close: pd.Series, period: int = 20, num_std: float = 2.0):
    """返回 (上轨, 中轨, 下轨, 带宽)。标准差用总体标准差 ddof=0。"""
    mid = close.rolling(period, min_periods=period).mean()
    std = close.rolling(period, min_periods=period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    width = (upper - lower) / mid
    return upper, mid, lower, width


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI。"""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    out = 100.0 - 100.0 / (1.0 + rs)
    # 平均跌幅为 0 时 RSI 理论上等于 100，避免除零产生 NaN
    out = out.where(avg_loss > 0, 100.0)
    out = out.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    return out


def kdj(high: pd.Series, low: pd.Series, close: pd.Series,
        period: int = 9, smooth: int = 3):
    """返回 (K, D, J)。K/D 以 50 为初值做递推平滑。"""
    low_n = low.rolling(period, min_periods=period).min()
    high_n = high.rolling(period, min_periods=period).max()
    span = high_n - low_n
    rsv = (close - low_n) / span.replace(0, pd.NA) * 100
    rsv = pd.to_numeric(rsv, errors='coerce')
    alpha = 1.0 / smooth
    k = rsv.ewm(alpha=alpha, adjust=False, ignore_na=True).mean()
    d = k.ewm(alpha=alpha, adjust=False, ignore_na=True).mean()
    j = 3 * k - 2 * d
    return k, d, j


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder ATR。"""
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def volume_ratio(volume: pd.Series, period: int = 20) -> pd.Series:
    """量比 = 当日成交量 / 前 period 日成交量均量（不含当日）。"""
    avg = volume.shift(1).rolling(period, min_periods=period).mean()
    return volume / avg.replace(0, pd.NA)


# ============================================================
# 关键点位
# ============================================================

def key_levels(kline: Kline, params: IndicatorParams) -> dict:
    """计算长期高点 / 低点（不含当日）。

    返回 dict:
      high, low           关键点位价格
      high_date, low_date 该点位出现的日期
      window_bars         参与计算的 K 线根数
      mode                'lookback' | 'range'
      label               给前端展示的口径说明
    """
    if params.ref_start or params.ref_end:
        mode = 'range'
        idx = [
            i for i, d in enumerate(kline.dates)
            if (not params.ref_start or d >= params.ref_start)
            and (not params.ref_end or d <= params.ref_end)
        ]
        # 只有当区间一直延伸到「最后一根 K 线」时，才剔除它（那是未收盘的当日）。
        # 若用户指定的是历史区间（比如 2026-06-01 ~ 2026-06-30），
        # 区间末日本身就是完整交易日，剔除它会白白丢掉最后一个点。
        if len(idx) > 1 and idx[-1] == len(kline.dates) - 1:
            idx = idx[:-1]
        if not idx:
            return {'high': None, 'low': None, 'high_date': '', 'low_date': '',
                    'window_bars': 0, 'mode': mode, 'label': '参考区间无数据'}
        highs = [kline.high[i] for i in idx]
        lows = [kline.low[i] for i in idx]
        high = max(highs)
        low = min(lows)
        high_date = kline.dates[idx[highs.index(high)]]
        low_date = kline.dates[idx[lows.index(low)]]
        label = f'{kline.dates[idx[0]]} ~ {kline.dates[idx[-1]]}'
        return {'high': high, 'low': low, 'high_date': high_date,
                'low_date': low_date, 'window_bars': len(idx),
                'mode': mode, 'label': label}

    mode = 'lookback'
    n = params.lookback
    if n <= 0 or n > len(kline.dates) - 1:
        n = len(kline.dates) - 1
    if n <= 0:
        return {'high': None, 'low': None, 'high_date': '', 'low_date': '',
                'window_bars': 0, 'mode': mode, 'label': '历史数据不足'}

    # 取不含当日的前 n 根
    end = len(kline.dates) - 1
    start = end - n
    if start < 0:
        start = 0
    highs = kline.high[start:end]
    lows = kline.low[start:end]
    if not highs or not lows:
        return {'high': None, 'low': None, 'high_date': '', 'low_date': '',
                'window_bars': 0, 'mode': mode, 'label': '历史数据不足'}

    high = max(highs)
    low = min(lows)
    high_date = kline.dates[start + highs.index(high)]
    low_date = kline.dates[start + lows.index(low)]
    return {
        'high': high, 'low': low, 'high_date': high_date, 'low_date': low_date,
        'window_bars': len(highs), 'mode': mode,
        'label': f'前 {len(highs)} 个交易日',
    }


# ============================================================
# 综合分析
# ============================================================

def _clean(value, digits: int = 4):
    """NaN/Inf/numpy 标量 → JSON 安全的 float 或 None。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, digits)


def _series_to_list(series: pd.Series, digits: int = 4) -> list:
    return [_clean(v, digits) for v in series.tolist()]


def analyze(kline: Kline, params: Optional[IndicatorParams] = None) -> dict:
    """计算全部指标。

    返回::

        {
          'code', 'name', 'last_date', 'bars',
          'latest': { 标量快照 },
          'series': { 供前端画图的等长数组 },
          'levels': { 关键点位 },
        }
    """
    params = params or IndicatorParams()
    from market_data import get_symbol

    meta = get_symbol(kline.code)
    result = {
        'code': kline.code,
        'name': meta.name if meta else kline.code,
        'exchange': meta.exchange if meta else '',
        'last_date': kline.last_date,
        'bars': len(kline),
        'stale': kline.stale,
        'live': getattr(kline, 'live', False),
        'fetched_at': kline.fetched_at,
    }

    if len(kline) < 2:
        result.update({'latest': {}, 'series': {}, 'levels': {},
                       'error': '历史数据不足，无法计算指标'})
        return result

    df = pd.DataFrame({
        'date': kline.dates,
        'open': kline.open,
        'high': kline.high,
        'low': kline.low,
        'close': kline.close,
        'volume': kline.volume,
    })

    close, high, low, vol = df['close'], df['high'], df['low'], df['volume']

    # ---- 均线 ----
    ma = {p: sma(close, p) for p in params.ma_periods}

    # ---- MACD ----
    dif, dea, hist = macd(close, params.macd_fast, params.macd_slow, params.macd_signal)

    # ---- BOLL ----
    bb_up, bb_mid, bb_low, bb_width = boll(close, params.boll_period, params.boll_std)
    bb_pctb = (close - bb_low) / (bb_up - bb_low).replace(0, pd.NA)

    # ---- RSI / KDJ / ATR / 量比 ----
    rsi_v = rsi(close, params.rsi_period)
    k_v, d_v, j_v = kdj(high, low, close, params.kdj_period, params.kdj_smooth)
    atr_v = atr(high, low, close, params.atr_period)
    vol_ma = vol.shift(1).rolling(params.vol_period, min_periods=params.vol_period).mean()
    vr = volume_ratio(vol, params.vol_period)

    # ---- 关键点位 ----
    levels = key_levels(kline, params)

    # ---- 最新快照 ----
    def last(series):
        return _clean(series.iloc[-1]) if len(series) else None

    last_close = float(close.iloc[-1])
    latest = {
        'date': kline.last_date,
        'close': _clean(last_close, 4),
        'open': _clean(df['open'].iloc[-1], 4),
        'high': _clean(high.iloc[-1], 4),
        'low': _clean(low.iloc[-1], 4),
        'volume': _clean(vol.iloc[-1], 2),
        'prev_close': _clean(close.iloc[-2], 4),
        'change_pct': _clean((last_close / float(close.iloc[-2]) - 1) * 100, 2)
        if float(close.iloc[-2]) else None,
    }

    # 区间动量：过去 N 个交易日的涨跌幅（%）。
    # 回测结论：20 日动量对后续 20 日收益的截面 IC = +0.036（t=2.80），
    # 是这段样本里少数显著为正的因子，因此单独提炼出来参与评分。
    for _n in (5, 20, 60):
        if len(close) > _n:
            _prev = float(close.iloc[-1 - _n])
            latest[f'mom{_n}'] = (_clean((last_close / _prev - 1) * 100, 2)
                                  if _prev else None)
        else:
            latest[f'mom{_n}'] = None
    for p in params.ma_periods:
        latest[f'ma{p}'] = last(ma[p])
    latest.update({
        'dif': last(dif), 'dea': last(dea), 'macd': last(hist),
        'boll_upper': last(bb_up), 'boll_mid': last(bb_mid),
        'boll_lower': last(bb_low), 'boll_width': last(bb_width),
        'boll_pctb': last(bb_pctb),
        'rsi': last(rsi_v),
        'k': last(k_v), 'd': last(d_v), 'j': last(j_v),
        'atr': last(atr_v),
        'atr_pct': _clean(last(atr_v) / last_close * 100, 2)
        if last(atr_v) is not None and last_close else None,
        'vol_ma': last(vol_ma),
        'vol_ratio': last(vr),
    })

    # 布林带宽度分位（判断「挤压」）：当前带宽在过去 N 根中的百分位
    window = min(120, len(df) - 1)
    width_valid = bb_width.dropna()
    if window >= 20 and len(width_valid) >= 20:
        recent = width_valid.iloc[-window:]
        cur = width_valid.iloc[-1]
        if cur is not None and not pd.isna(cur):
            latest['boll_width_pct_rank'] = round(
                float((recent <= cur).sum()) / len(recent) * 100, 1)
        else:
            latest['boll_width_pct_rank'] = None
    else:
        latest['boll_width_pct_rank'] = None

    # 均线排列
    latest['ma_alignment'] = _ma_alignment(latest, params.ma_periods, last_close)

    # ---- 派生信号（筛选打分用）----
    # 均线斜率：与 5 根之前比较，判断趋势是否还在延伸
    for p in params.ma_periods:
        slope = None
        if len(df) > 6:
            cur, prev = ma[p].iloc[-1], ma[p].iloc[-6]
            if prev is not None and not pd.isna(prev) and prev:
                slope = _clean((cur / prev - 1) * 100, 2)
        latest[f'ma{p}_slope_pct'] = slope

    # 收盘价相对均线偏离
    for p in params.ma_periods:
        v = latest.get(f'ma{p}')
        latest[f'close_vs_ma{p}_pct'] = (
            _clean((last_close / v - 1) * 100, 2) if v else None)

    # MACD 状态
    hist_delta = None
    if len(hist.dropna()) >= 2:
        clean_hist = hist.dropna()
        hist_delta = _clean(clean_hist.iloc[-1] - clean_hist.iloc[-2], 4)
    latest['macd_hist_delta'] = hist_delta
    latest['dif_above_zero'] = bool(_clean(dif.iloc[-1], 6) is not None and dif.iloc[-1] > 0)
    latest['dea_above_zero'] = bool(_clean(dea.iloc[-1], 6) is not None and dea.iloc[-1] > 0)
    latest['macd_golden_cross'] = _cross_recent(dif, dea, up=True, lookback=3)
    latest['macd_dead_cross'] = _cross_recent(dif, dea, up=False, lookback=3)
    latest['macd_hist_rising'] = bool(hist_delta is not None and hist_delta > 0)

    # RSI 变化 + 区间
    rsi_clean = rsi_v.dropna()
    latest['rsi_prev'] = _clean(rsi_clean.iloc[-2], 2) if len(rsi_clean) >= 2 else None
    latest['rsi_state'] = _rsi_state(latest.get('rsi'))

    # 布林带突破
    latest['boll_break_upper'] = bool(
        latest.get('boll_upper') and last_close > latest['boll_upper'])
    latest['boll_break_lower'] = bool(
        latest.get('boll_lower') and last_close < latest['boll_lower'])
    rank = latest.get('boll_width_pct_rank')
    latest['boll_squeeze'] = bool(rank is not None and rank <= 20)
    latest['boll_expanding'] = bool(rank is not None and rank >= 80)

    # ---- 距关键点位 ----
    if levels.get('high'):
        latest['level_high'] = _clean(levels['high'], 4)
        latest['level_high_date'] = levels['high_date']
        latest['dist_to_high_pct'] = _clean(
            (levels['high'] - last_close) / last_close * 100, 2) if last_close else None
        latest['above_high'] = last_close > levels['high']
    if levels.get('low'):
        latest['level_low'] = _clean(levels['low'], 4)
        latest['level_low_date'] = levels['low_date']
        latest['dist_to_low_pct'] = _clean(
            (last_close - levels['low']) / last_close * 100, 2) if last_close else None
        latest['below_low'] = last_close < levels['low']
    if levels.get('high') and levels.get('low') and levels['high'] > levels['low']:
        span = levels['high'] - levels['low']
        latest['range_pos'] = _clean((last_close - levels['low']) / span * 100, 1)
        latest['range_span_pct'] = _clean(span / last_close * 100, 2)
    else:
        latest['range_pos'] = None
        latest['range_span_pct'] = None

    # ---- 图表序列（默认只回传最近 max_bars 根，避免响应过大）----
    max_bars = 320
    tail = slice(-max_bars, None)
    series = {
        'dates': kline.dates[tail],
        'open': [_clean(v) for v in kline.open[tail]],
        'high': [_clean(v) for v in kline.high[tail]],
        'low': [_clean(v) for v in kline.low[tail]],
        'close': [_clean(v) for v in kline.close[tail]],
        'volume': [_clean(v, 2) for v in kline.volume[tail]],
        'ma': {str(p): _series_to_list(ma[p])[tail] for p in params.ma_periods},
        'boll_upper': _series_to_list(bb_up)[tail],
        'boll_mid': _series_to_list(bb_mid)[tail],
        'boll_lower': _series_to_list(bb_low)[tail],
        'dif': _series_to_list(dif)[tail],
        'dea': _series_to_list(dea)[tail],
        'macd': _series_to_list(hist)[tail],
        'level_high': _clean(levels.get('high'), 4),
        'level_low': _clean(levels.get('low'), 4),
    }

    result.update({'latest': latest, 'series': series, 'levels': levels})
    return result


def _ma_alignment(latest: dict, periods: tuple, close: float) -> str:
    """判断均线排列：多头 / 空头 / 纠缠。"""
    vals = []
    for p in sorted(periods):
        v = latest.get(f'ma{p}')
        if v is None:
            return '数据不足'
        vals.append((p, v))
    if close is None:
        return '数据不足'
    values = [close] + [v for _, v in vals]
    if all(values[i] > values[i + 1] for i in range(len(values) - 1)):
        return '多头排列'
    if all(values[i] < values[i + 1] for i in range(len(values) - 1)):
        return '空头排列'
    # 半程判断：短期均线同向
    short = [close] + [v for p, v in vals if p <= 20]
    if all(short[i] > short[i + 1] for i in range(len(short) - 1)):
        return '短期偏多'
    if all(short[i] < short[i + 1] for i in range(len(short) - 1)):
        return '短期偏空'
    return '均线纠缠'


def _cross_recent(a: pd.Series, b: pd.Series, up: bool, lookback: int = 3) -> bool:
    """判断 a 在最近 lookback 根内是否上穿(up=True)/下穿 b。"""
    diff = (a - b).dropna()
    if len(diff) < 2:
        return False
    recent = diff.iloc[-(lookback + 1):]
    for i in range(1, len(recent)):
        prev, cur = recent.iloc[i - 1], recent.iloc[i]
        if pd.isna(prev) or pd.isna(cur):
            continue
        if up and prev <= 0 < cur:
            return True
        if not up and prev >= 0 > cur:
            return True
    return False


def _rsi_state(value) -> str:
    """把 RSI 映射成国内习惯的超买超卖档位。"""
    if value is None:
        return '未知'
    if value >= 80:
        return '严重超买'
    if value >= 70:
        return '超买'
    if value <= 20:
        return '严重超卖'
    if value <= 30:
        return '超卖'
    if value >= 50:
        return '偏强'
    return '偏弱'
