"""
品种筛选引擎
============
核心需求：找出「价格逼近长期高点 / 低点（默认 5% 以内）」的品种，
再用均线、MACD、布林带、量能等技术指标给每个品种打分排序。

评分模型（满分 100）
--------------------
  维度            权重    看什么
  --------------- ------ ------------------------------------------------
  关键点位临近度    40    当前价距「前 N 日高点/低点」还差百分之几；越近越高，
                         已突破直接满分
  均线结构          20    收盘价与 MA5/10/20/60 的排列关系，是否顺着
                         突破方向排列（趋势确认）
  MACD             15    DIF/DEA 相对位置、是否在零轴上方、金叉/死叉、
                         MACD 柱是否扩张
  布林带           15    带宽是否处于历史低分位（收口=变盘前夜）、
                         价格在上轨/中轨的哪一侧、%B 位置
  量能             10    量比（当日量 / 前 20 日均量）

关键点位口径见 `indicators.key_levels`：取**不含当日**的前 N 根 K 线高低点，
这样「逼近前高」和「刚刚突破」才能区分开。

为什么把「关键点位」设成硬过滤（默认）
--------------------------------------
用户的核心诉求是「筛出即将突破的」，因此默认只返回
「距高点或距低点在 proximity% 以内」的品种；
把 require_level 设为 False 可切换成纯综合评分模式（全品种按分排序）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Iterable, Optional

from indicators import IndicatorParams, analyze
from market_data import (EXCHANGE_NAMES, KlineStore, all_symbols, get_store,
                         get_symbol)

# ============================================================
# 权重（可在配置里覆盖）
# ============================================================

# 评分权重（v2，经 480 交易日 × 3.4 万样本回测校准）
#
# 调整依据（见 BACKTEST.md）：
#   * 在「候选集内部」前 5 名里，现用评分 20 日按方向收益 +1.25%（候选集均值 +0.51%），
#     说明原有五维组合本身是有效的，不能简单砍掉
#   * 但叠加「方向对齐的 20 日动量」后进一步提升：
#     5 日 +0.106%（t=2.34，显著）、20 日 +0.197%（t=1.86，方向一致）
#   * 采用保守的 30% 动量占比：其余五维按 0.7 等比缩放，权重合计仍为 100
DEFAULT_WEIGHTS = {
    'level': 28.0,       # 40 × 0.7
    'trend': 14.0,       # 20 × 0.7
    'macd': 10.5,        # 15 × 0.7
    'boll': 10.5,        # 15 × 0.7
    'volume': 7.0,       # 10 × 0.7
    'momentum': 30.0,    # 新增：方向对齐的 20 日动量
}

# 动量归一化尺度：20 日涨跌幅达到 ±10% 即给满/零分
MOMENTUM_SCALE_PCT = 20.0

CONFIG_FIELDS = ('proximity', 'lookback', 'ref_start', 'ref_end', 'min_score',
                 'direction', 'exchanges', 'symbols', 'require_level')


# ============================================================
# 配置
# ============================================================

@dataclass
class ScreenerConfig:
    proximity: float = 5.0        # 距关键点位的阈值（百分比）
    lookback: int = 250           # 关键点位回看交易日数；0 = 全部历史
    ref_start: Optional[str] = None   # 参考区间模式（与 lookback 二选一）
    ref_end: Optional[str] = None
    min_score: float = 0.0
    direction: str = 'all'        # all | long | short
    exchanges: Optional[list[str]] = None
    symbols: Optional[list[str]] = None
    require_level: bool = True    # 是否强制要求「在阈值内」
    adjusted: bool = True         # 是否使用换月复权序列（强烈建议 True）
    weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    def indicator_params(self) -> IndicatorParams:
        return IndicatorParams(
            lookback=self.lookback,
            ref_start=self.ref_start,
            ref_end=self.ref_end,
        )

    @classmethod
    def from_query(cls, args) -> 'ScreenerConfig':
        """从 Flask request.args 构造，带完整容错（非法输入回退默认值）。"""

        def num(name, default, cast=float):
            raw = args.get(name)
            if raw is None or str(raw).strip() == '':
                return default
            try:
                return cast(float(str(raw).strip()))
            except (TypeError, ValueError):
                return default

        def flag(name, default):
            raw = args.get(name)
            if raw is None:
                return default
            return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')

        def csv_list(name):
            raw = args.get(name)
            if not raw:
                return None
            items = [x.strip() for x in str(raw).split(',') if x.strip()]
            return items or None

        direction = (args.get('direction') or 'all').strip().lower()
        if direction not in ('all', 'long', 'short'):
            direction = 'all'

        exchanges = csv_list('exchanges') or csv_list('exchange')
        if exchanges:
            exchanges = [e.strip().upper() for e in exchanges]
            exchanges = [e for e in exchanges if e in EXCHANGE_NAMES] or None

        return cls(
            proximity=max(0.1, min(50.0, num('proximity', 5.0))),
            lookback=max(0, num('lookback', 250, int)),
            ref_start=(args.get('ref_start') or '').strip() or None,
            ref_end=(args.get('ref_end') or '').strip() or None,
            min_score=num('min_score', 0.0),
            direction=direction,
            exchanges=exchanges,
            symbols=([s.strip().upper() for s in (csv_list('symbols') or [])] or None),
            require_level=flag('require_level', True),
            # 默认使用换月复权；传 adjusted=0 可退回原始主力连续（对比/排查用）
            adjusted=flag('adjusted', True),
        )


# ============================================================
# 打分
# ============================================================

def _score_level(dist: float, proximity: float, weight: float) -> tuple[float, bool]:
    """关键点位得分。dist 为「还差百分之几」，负值表示已突破。

    返回 (得分, 是否已突破)。
    """
    if dist is None:
        return 0.0, False
    if dist < 0:
        return float(weight), True          # 已突破，给满分
    if proximity <= 0:
        return 0.0, False
    ratio = dist / proximity                # 0=贴着点位, 1=刚好在阈值上
    return round(max(0.0, weight * (1.0 - ratio)), 1), False


def _score_trend(latest: dict, periods: tuple, direction: str, weight: float) -> tuple[float, list]:
    """均线结构得分：按「顺着突破方向」的排列程度给分。"""
    close = latest.get('close')
    vals = [close]
    for p in sorted(periods):
        v = latest.get(f'ma{p}')
        if v is None:
            return 0.0, ['均线数据不足']
        vals.append(v)

    total_pairs = len(vals) - 1
    reasons = []
    if direction == 'long':
        ok = sum(1 for i in range(total_pairs) if vals[i] > vals[i + 1])
    else:
        ok = sum(1 for i in range(total_pairs) if vals[i] < vals[i + 1])

    score = round(weight * ok / total_pairs, 1)

    alignment = latest.get('ma_alignment') or ''
    if alignment and alignment != '数据不足':
        reasons.append(alignment)

    slope = latest.get('ma20_slope_pct')
    if slope is not None:
        if direction == 'long' and slope > 0:
            reasons.append(f'MA20 上翘 {slope:+.2f}%')
        elif direction == 'short' and slope < 0:
            reasons.append(f'MA20 下行 {slope:+.2f}%')
        elif direction == 'long':
            reasons.append(f'MA20 下行 {slope:+.2f}%')
        else:
            reasons.append(f'MA20 上翘 {slope:+.2f}%')
    return score, reasons


def _score_macd(latest: dict, direction: str, weight: float) -> tuple[float, list]:
    """MACD 得分：位置 + 交叉 + 柱体扩张。"""
    dif, dea = latest.get('dif'), latest.get('dea')
    if dif is None or dea is None:
        return 0.0, ['MACD 数据不足']

    reasons = []
    score = 0.0
    long_side = direction == 'long'

    above = dif > dea if long_side else dif < dea
    if above:
        score += weight * 0.45
        reasons.append('DIF 在 DEA 上方（多头）' if long_side else 'DIF 在 DEA 下方（空头）')
    else:
        reasons.append('DIF 在 DEA 下方（多头排列未成）' if long_side
                       else 'DIF 在 DEA 上方（空头未成）')

    zero_ok = dif > 0 if long_side else dif < 0
    if zero_ok:
        score += weight * 0.20
        reasons.append('DIF 在零轴' + ('上方' if long_side else '下方'))

    rising = latest.get('macd_hist_rising')
    want_rising = long_side
    if rising is not None and bool(rising) == want_rising:
        score += weight * 0.15
        reasons.append('MACD 柱扩张' if want_rising else 'MACD 柱收敛')

    cross = (latest.get('macd_golden_cross') if long_side
             else latest.get('macd_dead_cross'))
    if cross:
        score += weight * 0.20
        reasons.append('近期 MACD 金叉' if long_side else '近期 MACD 死叉')

    return round(min(weight, score), 1), reasons


def _score_boll(latest: dict, direction: str, weight: float) -> tuple[float, list]:
    """布林带得分：收口（变盘前夜）+ 价格位置。"""
    reasons = []
    score = 0.0
    long_side = direction == 'long'

    if latest.get('boll_width_pct_rank') is None:
        return 0.0, ['布林带数据不足']

    if latest.get('boll_squeeze'):
        score += weight * 0.55
        reasons.append(f"布林带收口（带宽分位 {latest['boll_width_pct_rank']:.0f}%）")
    elif latest.get('boll_expanding'):
        score += weight * 0.25
        reasons.append('布林带张口（趋势已启动）')

    mid = latest.get('boll_mid')
    close = latest.get('close')
    if mid is not None and close is not None:
        if (close > mid) == long_side:
            score += weight * 0.25
            reasons.append('价格在布林中轨' + ('上方' if long_side else '下方'))

    pctb = latest.get('boll_pctb')
    if pctb is not None:
        if long_side and pctb >= 0.8:
            score += weight * 0.20
            reasons.append(f'%B={pctb:.2f} 贴近上轨')
        elif long_side and pctb <= 0.2:
            reasons.append(f'%B={pctb:.2f} 贴上轨未成')
        elif not long_side and pctb <= 0.2:
            score += weight * 0.20
            reasons.append(f'%B={pctb:.2f} 贴近下轨')
        elif not long_side and pctb >= 0.8:
            reasons.append(f'%B={pctb:.2f} 贴下轨未成')

    return round(min(weight, score), 1), reasons


def _score_momentum(latest: dict, direction: str, weight: float) -> tuple[float, list]:
    """区间动量打分（方向对齐）。

    为什么方向要对齐：
      * 「靠近前高」= 做多方向，我们希望过去确实在涨（正动量）→ 加分
      * 「靠近前低」= 做空方向，我们希望过去确实在跌（负动量）→ 加分
    所以先把动量乘上方向符号，再线性映射到 [0, weight]。

    回测依据：20 日动量对后续收益的截面 IC = +0.036（t=2.80，480 日 × 3.4 万样本）。
    """
    mom = latest.get('mom20')
    if mom is None:
        return 0.0, []
    aligned = mom * (1.0 if direction == 'long' else -1.0)
    ratio = (aligned + MOMENTUM_SCALE_PCT / 2.0) / MOMENTUM_SCALE_PCT
    ratio = max(0.0, min(1.0, ratio))
    reasons = []
    if ratio >= 0.75:
        reasons.append('20日动量同向且强（%+.1f%%）' % mom)
    elif ratio <= 0.25:
        reasons.append('20日动量反向（%+.1f%%）' % mom)
    return round(weight * ratio, 1), reasons


def _score_volume(latest: dict, weight: float) -> tuple[float, list]:
    """量能得分。放量确认突破，缩量通常是蓄势。"""
    vr = latest.get('vol_ratio')
    if vr is None:
        return 0.0, ['量比数据不足']

    if vr >= 2.0:
        score, note = 1.00, f'放量 {vr:.2f} 倍'
    elif vr >= 1.5:
        score, note = 0.85, f'温和放量 {vr:.2f} 倍'
    elif vr >= 1.0:
        score, note = 0.65, f'量能正常 {vr:.2f} 倍'
    elif vr >= 0.6:
        score, note = 0.45, f'缩量 {vr:.2f} 倍'
    else:
        score, note = 0.30, f'极度缩量 {vr:.2f} 倍'

    return round(weight * score, 1), [note]


# ============================================================
# 单品种分析 → 筛选结果
# ============================================================

def evaluate(code: str, kline, config: ScreenerConfig) -> Optional[dict]:
    """对一个品种跑完整评分，返回结果 dict；不满足硬条件时返回 None。"""
    meta = get_symbol(code)
    if meta is None or kline is None or len(kline) < 30:
        return None

    analysis = analyze(kline, config.indicator_params())
    latest = analysis.get('latest') or {}
    if not latest or latest.get('close') is None:
        return None

    proximity = config.proximity
    dist_high = latest.get('dist_to_high_pct')
    dist_low = latest.get('dist_to_low_pct')
    if dist_high is None or dist_low is None:
        return None

    # ---- 判定方向：哪边更近就用哪边 ----
    near_high = dist_high <= proximity
    near_low = dist_low <= proximity

    if not config.require_level:
        near_high = near_low = True

    if not (near_high or near_low):
        return None

    if near_high and near_low:
        direction = 'long' if abs(dist_high) <= abs(dist_low) else 'short'
        both_sides = True
    elif near_high:
        direction = 'long'
        both_sides = False
    else:
        direction = 'short'
        both_sides = False

    if config.direction == 'long' and direction != 'long':
        return None
    if config.direction == 'short' and direction != 'short':
        return None

    # ---- 各维度打分 ----
    w = config.weights
    level_score, broken = _score_level(
        dist_high if direction == 'long' else dist_low, proximity, w['level'])
    trend_score, trend_reasons = _score_trend(
        latest, config.indicator_params().ma_periods, direction, w['trend'])
    macd_score, macd_reasons = _score_macd(latest, direction, w['macd'])
    boll_score, boll_reasons = _score_boll(latest, direction, w['boll'])
    volume_score, volume_reasons = _score_volume(latest, w['volume'])
    momentum_score, momentum_reasons = _score_momentum(
        latest, direction, w.get('momentum', 0.0))

    total = round(level_score + trend_score + macd_score + boll_score
                  + volume_score + momentum_score, 1)
    if total < config.min_score:
        return None

    # ---- 信号标签 ----
    level_side = '前高' if direction == 'long' else '前低'
    if broken:
        setup = 'broken'
        signal = f'已突破{level_side}'
    elif (dist_high if direction == 'long' else dist_low) <= proximity * 0.5:
        setup = 'approaching'
        signal = f'逼近{level_side}'
    else:
        setup = 'watch'
        signal = f'接近{level_side}'

    level_dist = dist_high if direction == 'long' else dist_low
    level_price = latest.get('level_high' if direction == 'long' else 'level_low')
    level_date = latest.get('level_high_date' if direction == 'long' else 'level_low_date')

    reasons = [f'距{level_side} {level_dist:+.2f}%']
    if both_sides:
        reasons.append('处于长期区间上下沿之间')
    reasons += (momentum_reasons[:1] + trend_reasons[:2] + macd_reasons[:2]
                + boll_reasons[:2] + volume_reasons[:1])

    return {
        'code': code,
        'name': meta.name,
        'exchange': meta.exchange,
        'exchange_name': EXCHANGE_NAMES.get(meta.exchange, meta.exchange),
        'date': latest.get('date'),
        'live': bool(analysis.get('live')),
        'close': latest.get('close'),
        'change_pct': latest.get('change_pct'),
        'direction': direction,
        'both_sides': both_sides,
        'setup': setup,
        'signal': signal,
        'score': total,
        'scores': {
            'level': level_score, 'trend': trend_score, 'macd': macd_score,
            'boll': boll_score, 'volume': volume_score,
            'momentum': momentum_score,
        },
        'mom20': latest.get('mom20'),
        'mom5': latest.get('mom5'),
        'mom60': latest.get('mom60'),
        'proximity_threshold': proximity,
        'dist_to_high_pct': dist_high,
        'dist_to_low_pct': dist_low,
        'level_price': level_price,
        'level_date': level_date,
        'level_label': (analysis.get('levels') or {}).get('label', ''),
        'window_bars': (analysis.get('levels') or {}).get('window_bars', 0),
        'range_pos': latest.get('range_pos'),
        'range_span_pct': latest.get('range_span_pct'),
        'ma_alignment': latest.get('ma_alignment'),
        'rsi': latest.get('rsi'),
        'rsi_state': latest.get('rsi_state'),
        'macd': latest.get('macd'),
        'dif': latest.get('dif'),
        'dea': latest.get('dea'),
        'vol_ratio': latest.get('vol_ratio'),
        'atr_pct': latest.get('atr_pct'),
        'boll_pctb': latest.get('boll_pctb'),
        'boll_width_pct_rank': latest.get('boll_width_pct_rank'),
        'reasons': reasons,
    }


# ============================================================
# 批量筛选
# ============================================================

def screen(config: ScreenerConfig = None, store: KlineStore = None) -> dict:
    """对全部（或指定）品种执行筛选。

    返回::

        {
          'results': [...],          # 按评分倒序
          'meta': {...},             # 参数与数据口径
          'stats': {...},            # 统计
          'missing': [...],          # 行情拉取失败的品种
        }
    """
    config = config or ScreenerConfig()
    store = store or get_store()

    universe = all_symbols()
    if config.exchanges:
        allowed = set(config.exchanges)
        universe = [s for s in universe if s.exchange in allowed]
    if config.symbols:
        wanted = {s.strip().upper() for s in config.symbols if s.strip()}
        universe = [s for s in universe if s.code in wanted]

    codes = [s.code for s in universe]
    # 用换月复权序列：主力连续在换月处有台阶（实测平均是正常日波动的 2 倍，
    # 玻璃/焦煤单次可到 15%），不复权会让「前高/前低」本身失真。
    use_adjusted = getattr(config, 'adjusted', True)
    try:
        klines = store.load(codes, adjusted=use_adjusted)
    except TypeError:                 # 兼容不支持 adjusted 参数的外部 store
        klines = store.load(codes)

    results = []
    missing = []
    for code in codes:
        kline = klines.get(code)
        if kline is None:
            missing.append(code)
            continue
        try:
            item = evaluate(code, kline, config)
        except Exception as exc:  # 单品种异常不该让整轮筛选失败
            missing.append(code)
            continue
        if item is not None:
            results.append(item)

    results.sort(key=lambda r: (-r['score'], r['code']))

    live_count = sum(1 for r in results if r.get('live'))
    return {
        'results': results,
        'missing': missing,
        'meta': {
            'proximity': config.proximity,
            'lookback': config.lookback,
            'ref_start': config.ref_start,
            'ref_end': config.ref_end,
            'min_score': config.min_score,
            'direction': config.direction,
            'require_level': config.require_level,
            'exchanges': config.exchanges,
            'weights': config.weights,
            'level_mode': 'range' if (config.ref_start or config.ref_end) else 'lookback',
            'universe_size': len(codes),
            'data_source': ('sina_futures_main_continuous_adjusted'
                            if use_adjusted else 'sina_futures_main_continuous'),
            'adjusted': bool(use_adjusted),
            'data_note': ('新浪财经主力连续合约日线 + 实时快照；'
                          '已按真实持仓量还原主力合约并做换月复权，'
                          '避免换月假跳空污染前高/前低与动量'
                          if use_adjusted else
                          '新浪财经期指主力连续合约日线 + 实时快照；'
                          '未做换月复权，超长期高低点可能含换月缺口'),
            'live_coverage': live_count,
        },
        'stats': {
            'matched': len(results),
            'long': sum(1 for r in results if r['direction'] == 'long'),
            'short': sum(1 for r in results if r['direction'] == 'short'),
            'broken': sum(1 for r in results if r['setup'] == 'broken'),
            'approaching': sum(1 for r in results if r['setup'] == 'approaching'),
            'failed': len(missing),
        },
    }
