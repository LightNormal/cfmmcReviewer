"""
期货交易复盘系统
================
功能：
  1. 解析监控中心下载的逐笔/逐日 xls 结算单
  2. 按日期范围查询盈亏，按盈利金额排序
  3. 品种汇总（Ag2604 + Ag2608 + Ag2604C15000 → Ag）
  4. 统计：胜率、盈亏比、手续费、品种排名
  5. 可视化：盈亏柱状图、品种饼图、趋势图
  6. 自动补数：缺失日期自动从监控中心下载

用法：
  python review.py                    # 交互式 CLI
  python review.py --start 20260401 --end 20260510   # 命令行模式
"""

import os
import re
import sys
import json
import glob
import argparse
from datetime import datetime, timedelta
from collections import defaultdict

import xlrd
import pandas as pd
import matplotlib
matplotlib.use('Agg')  # 非GUI后端，兼容 Web 服务和 CLI
import matplotlib.pyplot as plt
matplotlib.rcParams['font.sans-serif'] = ['Arial Unicode MS', 'SimHei', 'PingFang SC']
matplotlib.rcParams['axes.unicode_minus'] = False


# ============================================================
# 配置
# ============================================================
DEFAULT_OUTPUT_DIR = './output'
CONFIG_FILE = 'config.json'


# ============================================================
# 中国证券市场交易日判断
# ============================================================
# 优先使用 chinesecalendar 库（专业维护中国法定节假日及调休安排）
# pip install chinesecalendar   → 每年更新，含调休上班日
# 若未安装或超出数据年份范围，回退到仅排除周末
try:
    import chinese_calendar  # type: ignore
    _HAS_CHINESE_CALENDAR = True
except ImportError:
    _HAS_CHINESE_CALENDAR = False


def is_cn_trading_day(date_str: str) -> bool:
    """
    判断某日是否为中国证券市场的交易日（非周末且非法定节假日）。

    优先使用 chinesecalendar 库（自动处理历年节假日及调休上班日）；
    若未安装或超出其数据年份范围，回退到仅排除周末。
    """
    if not date_str:
        return False
    try:
        dt = datetime.strptime(date_str[:10], '%Y-%m-%d')
    except (ValueError, TypeError):
        return False

    # 排除周末
    if dt.weekday() >= 5:
        return False

    # 优先使用 chinesecalendar（专业中国假期库，含调休上班日）
    if _HAS_CHINESE_CALENDAR:
        try:
            return chinese_calendar.is_workday(dt)
        except NotImplementedError:
            # 超出 chinesecalendar 数据年份范围，回退
            pass

    # 回退：无额外库或超出范围时仅排除周末
    # 建议安装: pip install chinesecalendar
    return True


# 兼容性提示（首次调用时打印一次）
_TRADING_DAY_WARNED = False


# ============================================================
# 工具函数
# ============================================================
def _to_float(val) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _parse_pnl(val) -> float:
    s = str(val).strip()
    if s in ('--', '', '-'):
        return 0.0
    return _to_float(s)


def extract_symbol(contract: str) -> str:
    """从合约名提取品种代码。AG2608→AG, Ag2604C15000→Ag, SC2605P600→SC"""
    s = contract.strip().upper()
    s = s.split('-')[0]
    m = re.match(r'^([A-Z]+)', s)
    return m.group(1) if m else s


# ============================================================
# 数据解析：从 xls 中提取成交记录
# ============================================================
def parse_trade_xls(filepath: str) -> dict:
    wb = xlrd.open_workbook(filepath)
    ws = wb.sheet_by_index(0)

    result = {
        'date': '', 'account': '',
        'futures_trades': [], 'options_trades': [], 'summary': {},
    }

    # ---- 逐日汇总解析（资金状况区域）----
    # 实际 xls 布局为两列并排：
    #   左列: c0=标签, c2=值   |   右列: c5=标签, c7=值
    #   特殊: "交易日期"在c5, 值在c7; "可用资金"等只有右半边
    # 兼容两种报表格式:
    #   逐日盯市: "当日盈亏"
    #   逐笔对冲: "平仓盈亏", "浮动盈亏"
    _sum_labels_left = ['上日结存', '当日存取合计', '当日盈亏', '平仓盈亏',
                        '浮动盈亏', '当日总权利金',
                        '当日手续费', '当日结存']
    _sum_labels_right = ['客户权益', '实有货币资金', '非货币充抵金额',
                         '货币充抵金额', '冻结资金', '保证金占用',
                         '可用资金', '风险度', '追加保证金']
    for r in range(min(20, ws.nrows)):
        row_vals = [str(ws.cell_value(r, c)).strip() for c in range(ws.ncols)]
        # 账号
        if '客户期货期权内部资金账户' in row_vals[0]:
            result['account'] = str(row_vals[2] if len(row_vals) > 2 else '').zfill(12)
        # 交易日期（在右半边 c5）
        if '交易日期' in row_vals[5] if len(row_vals) > 5 else False:
            raw = str(row_vals[7] if len(row_vals) > 7 else '')
            try:
                result['date'] = datetime.strptime(raw[:10], '%Y-%m-%d').strftime('%Y-%m-%d')
            except Exception:
                pass
        # 左半边: 标签在 c0, 值在 c2
        key0 = str(ws.cell_value(r, 0)).strip()
        if key0 in _sum_labels_left and ws.ncols > 2:
            val = ws.cell_value(r, 2)
            if str(val).strip() != '':
                result['summary'][key0] = val
        # 右半边: 标签在 c5, 值在 c7
        key5 = str(ws.cell_value(r, 5)).strip() if ws.ncols > 5 else ''
        if key5 in _sum_labels_right and ws.ncols > 7:
            val = ws.cell_value(r, 7)
            if str(val).strip() != '':
                result['summary'][key5] = val

    # 如果没从表格中提取到日期，尝试从文件名提取
    if not result['date']:
        import re as _re
        # 文件名格式: {account}_{YYYY-MM-DD}.xls 或 {account}_{YYYYMMDD}.xls
        basename = os.path.basename(filepath)
        # 先找标准日期格式 YYYY-MM-DD 或 YYYYMMDD（在 _ 之后的部分）
        m = _re.search(r'_(\d{4}[-]?\d{2}[-]?\d{2})\.', basename)
        if m:
            d = m.group(1)
            if len(d) == 8:
                result['date'] = f'{d[:4]}-{d[4:6]}-{d[6:]}'
            else:
                result['date'] = d.replace('-', '')[:8]
                result['date'] = f'{result["date"][:4]}-{result["date"][4:6]}-{result["date"][6:]}'

    # ---- 期货成交汇总 ----
    futures_start = None
    for r in range(ws.nrows):
        if str(ws.cell_value(r, 0)).strip() == '期货成交汇总':
            futures_start = r + 2
            break
    if futures_start:
        for r in range(futures_start + 1, ws.nrows):
            contract = str(ws.cell_value(r, 0)).strip()
            if not contract or contract == '合计':
                break
            pnl = _parse_pnl(ws.cell_value(r, 9))
            result['futures_trades'].append({
                'contract': contract,
                'symbol': extract_symbol(contract),
                'direction': str(ws.cell_value(r, 1)).strip(),
                'hedge_type': str(ws.cell_value(r, 2)).strip(),
                'price': _to_float(ws.cell_value(r, 3)),
                'volume': _to_float(ws.cell_value(r, 4)),
                'amount': _to_float(ws.cell_value(r, 5)),
                'open_close': str(ws.cell_value(r, 7)).strip(),
                'commission': _to_float(ws.cell_value(r, 8)),
                'realized_pnl': pnl,
                'date': result['date'],
                'account': result['account'],
            })

    # ---- 期权成交汇总 ----
    options_start = None
    for r in range(ws.nrows):
        if str(ws.cell_value(r, 0)).strip() == '期权成交汇总':
            options_start = r + 2
            break
    if options_start:
        for r in range(options_start, ws.nrows):
            opt_contract = str(ws.cell_value(r, 0)).strip()
            if not opt_contract or opt_contract == '合计':
                break
            premium_total = _to_float(ws.cell_value(r, min(7, ws.ncols - 1)))
            result['options_trades'].append({
                'opt_contract': opt_contract,
                'symbol': extract_symbol(opt_contract),
                'underlying': str(ws.cell_value(r, min(1, ws.ncols - 1))).strip(),
                'opt_type': str(ws.cell_value(r, min(2, ws.ncols - 1))).strip(),
                'strike': _to_float(ws.cell_value(r, min(3, ws.ncols - 1))),
                'direction': str(ws.cell_value(r, min(4, ws.ncols - 1))).strip(),
                'premium_price': _to_float(ws.cell_value(r, min(5, ws.ncols - 1))),
                'volume': _to_float(ws.cell_value(r, min(6, ws.ncols - 1))),
                'premium_total': premium_total,
                'commission': _to_float(ws.cell_value(r, min(10, ws.ncols - 1))),
                # premium_total 已含符号: 买方(负=支出), 卖方(正=收入)
                'pnl': premium_total,
                'date': result['date'],
                'account': result['account'],
            })

    return result


# ============================================================
# 数据加载：扫描目录下所有 xls 文件
# ============================================================
class DataLoader:
    """加载和管理本地已下载的结算单数据"""

    def __init__(self, output_dir: str = DEFAULT_OUTPUT_DIR):
        self.output_dir = output_dir
        self.cache = {}  # date_str -> parsed_data

    def scan_files(self) -> list[str]:
        """扫描所有逐笔xls文件"""
        pattern = os.path.join(self.output_dir, '**', '逐笔', '*.xls')
        files = glob.glob(pattern, recursive=True)
        return sorted(files)

    def load_all(self) -> dict:
        """加载所有可用的结算单"""
        files = self.scan_files()
        for fp in files:
            try:
                data = parse_trade_xls(fp)
                if data['date']:
                    self.cache[data['date']] = data
            except Exception as e:
                print(f'  警告: 解析失败 {os.path.basename(fp)}: {e}')
        print(f'已加载 {len(self.cache)} 个交易日的数据')
        return self.cache

    def get_date_range(self) -> tuple:
        """获取已有数据的日期范围"""
        dates = sorted(self.cache.keys())
        if not dates:
            return None, None
        return dates[0], dates[-1]

    def get_missing_dates(self, start: str, end: str) -> list[str]:
        """获取指定范围内缺失的交易日（排除周末和中国法定节假日）"""
        global _TRADING_DAY_WARNED
        if not _HAS_CHINESE_CALENDAR and not _TRADING_DAY_WARNED:
            print("⚠ 未安装 chinesecalendar，法定节假日判断仅排除周末。"
                  "建议运行: pip install chinesecalendar")
            _TRADING_DAY_WARNED = True

        missing = []
        current = datetime.strptime(start, '%Y-%m-%d')
        end_dt = datetime.strptime(end, '%Y-%m-%d')
        while current <= end_dt:
            ds = current.strftime('%Y-%m-%d')
            if is_cn_trading_day(ds) and ds not in self.cache:
                missing.append(ds)
            current += timedelta(days=1)
        return missing


# ============================================================
# 核心分析引擎
# ============================================================
class ReviewEngine:
    """复盘分析引擎"""

    def __init__(self, data_loader: DataLoader):
        self.loader = data_loader
        self.trades_df = None
        self.options_df = None
        self.daily_summary_df = None

    def build_dfs(self, start: str = None, end: str = None):
        """构建 DataFrame（可选日期过滤）"""
        all_futures = []
        all_options = []
        daily_rows = []

        for date_str, data in self.loader.cache.items():
            if start and date_str < start:
                continue
            if end and date_str > end:
                continue

            for t in data['futures_trades']:
                all_futures.append(t)
            for t in data['options_trades']:
                all_options.append(t)

            summary = data.get('summary', {})
            # 兼容两种报表格式：
            #   逐日盯市: "当日盈亏" / 无浮动盈亏
            #   逐笔对冲: "平仓盈亏" / "浮动盈亏"
            daily_rows.append({
                'date': date_str,
                'equity': _to_float(summary.get('客户权益', 0)),
                'pnl': _to_float(summary.get('当日盈亏', None) or summary.get('平仓盈亏', 0)),
                'float_pnl': _to_float(summary.get('浮动盈亏', summary.get('持仓盈亏', 0))),
                'commission': _to_float(summary.get('当日手续费', 0)),
                'deposit': _to_float(summary.get('当日结存', 0)),
                'margin': _to_float(summary.get('保证金占用', 0)),
            })

        self.trades_df = pd.DataFrame(all_futures) if all_futures else pd.DataFrame()
        self.options_df = pd.DataFrame(all_options) if all_options else pd.DataFrame()
        self.daily_summary_df = pd.DataFrame(daily_rows) if daily_rows else pd.DataFrame()

        if not self.daily_summary_df.empty:
            self.daily_summary_df['date'] = pd.to_datetime(self.daily_summary_df['date'])
            self.daily_summary_df = self.daily_summary_df.sort_values('date').reset_index(drop=True)

        if not self.trades_df.empty:
            self.trades_df['date'] = pd.to_datetime(self.trades_df['date'])

        if not self.options_df.empty:
            self.options_df['date'] = pd.to_datetime(self.options_df['date'])

        return self

    # ---- 期权开平仓匹配（FIFO）----
    def _match_options_pnl(self) -> pd.DataFrame:
        """
        使用 FIFO 算法对期权成交记录进行开平仓匹配，计算每笔已实现盈亏。

        匹配规则：
          - 同一个 opt_contract 下，买方(支出)与卖方(收入)配对
          - 买方开仓 → 卖方平仓: 盈亏 = (卖价 - 买价) × 手数
          - 卖方开仓 → 买方平仓: 盈亏 = (卖价 - 买价) × 手数
        返回带 realized_pnl 列的 options_df 副本。
        """
        if self.options_df.empty:
            return pd.DataFrame()

        df = self.options_df.copy()
        df['realized_pnl'] = 0.0  # 新增列：每笔成交对应的已实现盈亏

        # 按合约分组，每组内按日期排序做 FIFO 匹配
        for contract, group in df.groupby('opt_contract'):
            group = group.sort_values('date').reset_index(drop=True)

            # 两个队列：买方队列（多头持仓）、卖方队列（空头持仓）
            # 每个元素: {volume: 剩余手数, premium_price: 开仓价格}
            long_queue = []   # 买方开仓记录（等待卖方平仓）
            short_queue = []  # 卖方开仓记录（等待买方平仓）

            for idx in group.index:
                row = df.loc[idx]
                direction = str(row.get('direction', '')).strip()
                volume = float(row.get('volume', 0))
                price = float(row.get('premium_price', 0))

                if direction == '买方':
                    # 买方 → 如果有卖方队列则匹配平仓，否则加入买方队列（开多）
                    remaining = volume
                    pnl_sum = 0.0

                    while remaining > 0 and short_queue:
                        pos = short_queue[0]
                        match_vol = min(remaining, pos['volume'])
                        # 卖方开仓价格 vs 买方平仓价格
                        pnl_sum += (price - pos['premium_price']) * match_vol
                        pos['volume'] -= match_vol
                        remaining -= match_vol
                        if pos['volume'] <= 0:
                            short_queue.pop(0)

                    df.at[idx, 'realized_pnl'] = round(pnl_sum, 2)

                    # 剩余部分加入买方队列（新开仓）
                    if remaining > 0:
                        long_queue.append({'volume': remaining, 'premium_price': price})

                elif direction == '卖方':
                    # 卖方 → 如果有买方队列则匹配平仓，否则加入卖方队列（开空）
                    remaining = volume
                    pnl_sum = 0.0

                    while remaining > 0 and long_queue:
                        pos = long_queue[0]
                        match_vol = min(remaining, pos['volume'])
                        # 买方开仓价格 vs 卖方平仓价格
                        pnl_sum += (pos['premium_price'] - price) * match_vol
                        pos['volume'] -= match_vol
                        remaining -= match_vol
                        if pos['volume'] <= 0:
                            long_queue.pop(0)

                    df.at[idx, 'realized_pnl'] = round(pnl_sum, 2)

                    # 剩余部分加入卖方队列（新开仓）
                    if remaining > 0:
                        short_queue.append({'volume': remaining, 'premium_price': price})

        return df

    def get_matched_options_df(self) -> pd.DataFrame:
        """获取已完成开平仓匹配的期权 DataFrame（含 realized_pnl）"""
        return self._match_options_pnl()

    # ---- 按盈利排序的成交记录 ----
    def get_trades_sorted_by_pnl(self, top_n: int = None, ascending: bool = False) -> pd.DataFrame:
        """获取按平仓盈亏排序的成交记录（默认从高到低）"""
        if self.trades_df.empty:
            return pd.DataFrame()
        df = self.trades_df.sort_values('realized_pnl', ascending=ascending).reset_index(drop=True)
        if top_n:
            df = df.head(top_n)
        return df

    # ---- 品种汇总 ----
    def get_symbol_summary(self) -> pd.DataFrame:
        """
        按品种汇总统计。
        
        期权盈亏使用权利金现金流法（premium_total / 原始 pnl）。
        同时覆盖：只做期权的品种（无期货成交记录）。
        
        Returns DataFrame with columns:
          品种, 手数, 期货盈亏(元), 期货手续费(元), 期权盈亏(元), 期权手续费(元), 总盈亏(元), 总手续费(元), 胜率, 盈亏比
        """
        # 收集所有品种（期货 ∪ 期权）
        all_symbols = set()
        if not self.trades_df.empty:
            all_symbols.update(self.trades_df['symbol'].unique())
        if not self.options_df.empty:
            all_symbols.update(self.options_df['symbol'].unique())

        if not all_symbols:
            return pd.DataFrame()

        df = self.trades_df.copy()
        # 只统计有平仓盈亏的记录（开仓为 '--'/0）
        closed_trades = df[df['realized_pnl'] != 0].copy() if not df.empty else pd.DataFrame()

        results = []
        for symbol in sorted(all_symbols):
            # ---- 期货部分 ----
            futures_of_symbol = df[df['symbol'] == symbol] if not df.empty else pd.DataFrame()
            closed = closed_trades[closed_trades['symbol'] == symbol] if not closed_trades.empty else pd.DataFrame()
            wins = closed[closed['realized_pnl'] > 0] if not closed.empty else pd.DataFrame()
            losses = closed[closed['realized_pnl'] < 0] if not closed.empty else pd.DataFrame()

            total_pnl = futures_of_symbol['realized_pnl'].sum() if not futures_of_symbol.empty else 0
            total_commission = futures_of_symbol['commission'].sum() if not futures_of_symbol.empty else 0
            win_count = len(wins)
            loss_count = len(losses)
            total_closed = win_count + loss_count

            avg_win = wins['realized_pnl'].mean() if len(wins) > 0 else 0
            avg_loss = abs(losses['realized_pnl'].mean()) if len(losses) > 0 else 0
            profit_factor = (wins['realized_pnl'].sum() / abs(losses['realized_pnl'].sum())) if (not losses.empty and losses['realized_pnl'].sum() != 0) else float('inf')

            # ---- 期权部分（权利金现金流法）----
            opt_group = self.options_df[self.options_df['symbol'] == symbol] if not self.options_df.empty else pd.DataFrame()
            opt_commission = opt_group['commission'].sum() if not opt_group.empty else 0
            opt_pnl = opt_group['pnl'].sum() if not opt_group.empty else 0

            total_volume = futures_of_symbol['volume'].sum() if not futures_of_symbol.empty else 0
            total_pnl_all = total_pnl + opt_pnl
            total_comm_all = total_commission + opt_commission

            results.append({
                '品种': symbol,
                '手数': int(total_volume),
                '期货盈亏(元)': round(total_pnl, 2),
                '期货手续费(元)': round(total_commission, 2),
                '期权盈亏(元)': round(opt_pnl, 2),
                '期权手续费(元)': round(opt_commission, 2),
                '总盈亏(元)': round(total_pnl_all, 2),
                '总手续费(元)': round(total_comm_all, 2),
                # 期货胜率与盈亏比（仅统计已平仓记录）
                '胜率': f'{win_count/total_closed*100:.1f}%' if total_closed > 0 else '-',
                '盈亏比': round(profit_factor, 2) if profit_factor != float('inf') else '∞',
            })

        result_df = pd.DataFrame(results)
        if not result_df.empty:
            result_df = result_df.sort_values('总盈亏(元)', ascending=False).reset_index(drop=True)
        return result_df

    # ---- 每日盈亏趋势（基于逐笔平仓盈亏求和）----
    def get_daily_trend(self) -> pd.DataFrame:
        """每日累计盈亏趋势，期货平仓盈亏 + 期权权利金现金流(pnl)按日期求和"""
        if self.trades_df.empty and self.options_df.empty:
            return pd.DataFrame()
        
        # 从期货逐笔数据按日期汇总
        daily = {}
        if not self.trades_df.empty:
            for _, row in self.trades_df.iterrows():
                d = str(row['date'])[:10]
                if d not in daily:
                    daily[d] = {'futures_pnl': 0.0, 'futures_comm': 0.0, 'opt_pnl': 0.0, 'opt_comm': 0.0}
                daily[d]['futures_pnl'] += row['realized_pnl']
                daily[d]['futures_comm'] += row['commission']
        
        # 从期权逐笔数据按日期汇总（使用原始 pnl 权利金现金流）
        if not self.options_df.empty:
            for _, row in self.options_df.iterrows():
                d = str(row['date'])[:10]
                if d not in daily:
                    daily[d] = {'futures_pnl': 0.0, 'futures_comm': 0.0, 'opt_pnl': 0.0, 'opt_comm': 0.0}
                daily[d]['opt_pnl'] += row.get('pnl', 0)
                daily[d]['opt_comm'] += row.get('commission', 0)
        
        rows = []
        for d in sorted(daily.keys()):
            item = daily[d]
            total_pnl = item['futures_pnl'] + item['opt_pnl']
            total_comm = item['futures_comm'] + item['opt_comm']
            rows.append({
                'date': d,
                'pnl': total_pnl,
                'futures_pnl': item['futures_pnl'],
                'opt_pnl': item['opt_pnl'],
                'commission': total_comm,
            })
        
        df = pd.DataFrame(rows)
        if not df.empty:
            df['date'] = pd.to_datetime(df['date'])
            df['累计净盈亏'] = (df['pnl'] - df['commission']).cumsum()
        return df

    # ---- 按日期查询 ----
    def query_by_date_range(self, start: str, end: str) -> dict:
        """综合查询指定日期范围"""
        self.build_dfs(start, end)

        symbol_df = self.get_symbol_summary()
        trades_df = self.get_trades_sorted_by_pnl()
        trend_df = self.get_daily_trend()

        # 只计算期货逐笔平仓盈亏（不含期权权利金收支）
        futures_pnl = self.trades_df['realized_pnl'].sum() if not self.trades_df.empty else 0
        futures_comm = self.trades_df['commission'].sum() if not self.trades_df.empty else 0
        # 期权：使用权利金现金流法（原始 pnl = premium_total）
        opt_pnl = self.options_df['pnl'].sum() if not self.options_df.empty else 0
        opt_comm = self.options_df['commission'].sum() if not self.options_df.empty else 0

        overview = {
            '查询范围': f'{start} ~ {end}',
            '交易日数': len(self.daily_summary_df),
            '期货笔数': len(self.trades_df),
            '期权笔数': len(self.options_df),
            # 期货（核心指标）
            '期货盈亏(元)': round(futures_pnl, 2),
            '期货手续费(元)': round(futures_comm, 2),
            '期货净盈亏(元)': round(futures_pnl - futures_comm, 2),
            # 期权（单独列示）
            '期权盈亏(元)': round(opt_pnl, 2),
            '期权手续费(元)': round(opt_comm, 2),
            '品种数': len(symbol_df) if not symbol_df.empty else 0,
        }

        return {
            'overview': overview,
            'symbol_summary': symbol_df,
            'trades_sorted': trades_df,
            'daily_trend': trend_df,
        }


# ============================================================
# 可视化
# ============================================================
class ReviewVisualizer:
    """复盘结果可视化"""

    def __init__(self, output_dir: str = './review_charts'):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def plot_symbol_pnl_bar(self, symbol_df: pd.DataFrame, title: str = '品种净盈亏排名') -> str:
        """品种净盈亏柱状图"""
        if symbol_df.empty:
            return ''
        fig, ax = plt.subplots(figsize=(max(10, len(symbol_df) * 0.7), 6))

        symbols = symbol_df['品种'].tolist()
        pnls = symbol_df['净盈亏(元)'].tolist()
        colors = ['#e74c3c' if v >= 0 else '#27ae60' for v in pnls]

        bars = ax.barh(symbols[::-1], [v for v in reversed(pnls)], color=colors[::-1], edgecolor='white', height=0.6)
        ax.axvline(x=0, color='gray', linewidth=0.8, linestyle='-')
        ax.set_xlabel('净盈亏 (元)', fontsize=11)
        ax.set_title(title, fontsize=14, fontweight='bold', pad=15)

        for bar, val in zip(bars, pnls[::-1]):
            width = bar.get_width()
            offset = 5 if width >= 0 else -5
            ha = 'left' if width >= 0 else 'right'
            ax.text(width + offset, bar.get_y() + bar.get_height()/2,
                    f'{val:+,.0f}', va='center', ha=ha, fontsize=9)

        plt.tight_layout()
        path = os.path.join(self.output_dir, 'symbol_pnl_ranking.png')
        fig.savefig(path, dpi=120, bbox_inches='tight')
        plt.close(fig)
        return path

    def plot_daily_trend(self, trend_df: pd.DataFrame, title: str = '每日累计净盈亏趋势') -> str:
        """每日盈亏趋势折线图"""
        if trend_df.empty:
            return ''
        fig, ax1 = plt.subplots(figsize=(14, 6))

        dates = trend_df['date'].dt.strftime('%m-%d').tolist()
        x = range(len(dates))

        # 每日净盈亏柱状图
        daily_net = (trend_df['pnl'] - trend_df['commission']).tolist()
        colors = ['#e74c3c' if v >= 0 else '#27ae60' for v in daily_net]
        ax1.bar(x, daily_net, color=colors, alpha=0.6, label='每日净盈亏', width=0.7)

        # 累计线
        ax2 = ax1.twinx()
        cumulative = trend_df['累计净盈亏'].tolist()
        ax2.plot(x, cumulative, 'b-o', linewidth=2, markersize=5, label='累计净盈亏')
        ax2.axhline(y=0, color='blue', linewidth=0.8, linestyle='--', alpha=0.5)

        ax1.set_xticks(x)
        ax1.set_xticklabels(dates, rotation=45, ha='right', fontsize=9)
        ax1.set_ylabel('每日净盈亏 (元)', fontsize=11, color='gray')
        ax2.set_ylabel('累计净盈亏 (元)', fontsize=11, color='blue')
        ax1.set_title(title, fontsize=14, fontweight='bold', pad=15)

        lines1, labels1 = ax1.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax1.legend(lines1 + lines2, labels1 + labels2, loc='upper left')

        plt.tight_layout()
        path = os.path.join(self.output_dir, 'daily_trend.png')
        fig.savefig(path, dpi=120, bbox_inches='tight')
        plt.close(fig)
        return path

    def plot_symbol_pie(self, symbol_df: pd.DataFrame, title: str = '品种盈亏分布') -> str:
        """品种盈亏饼图（只显示有盈亏的品种）"""
        if symbol_df.empty:
            return ''
        df = symbol_df[symbol_df['总盈亏(元)'] != 0].copy()
        if df.empty:
            return ''

        fig, ax = plt.subplots(figsize=(10, 8))
        labels = df['品种'].tolist()
        sizes = [abs(v) for v in df['总盈亏(元)'].tolist()]
        colors = ['#e74c3c' if v > 0 else '#27ae60' for v in df['总盈亏(元)'].tolist()]

        explode = [0.05] * len(labels)
        wedges, texts, autotexts = ax.pie(
            sizes, labels=labels, colors=colors, explode=explode,
            autopct=lambda pct: f'{pct:.1f}%\n({pct/100*sum(sizes):+.0f}元)' if pct > 3 else '',
            textprops={'fontsize': 9}, startangle=90, pctdistance=0.75
        )
        ax.set_title(title, fontsize=14, fontweight='bold')

        plt.tight_layout()
        path = os.path.join(self.output_dir, 'symbol_pie.png')
        fig.savefig(path, dpi=120, bbox_inches='tight')
        plt.close(fig)
        return path

    def plot_win_rate(self, symbol_df: pd.DataFrame, title: str = '品种胜率与盈亏比') -> str:
        """胜率和盈亏比对比图"""
        if symbol_df.empty:
            return ''
        df = symbol_df[symbol_df['胜率'] != 'N/A'].copy()
        if df.empty:
            return ''

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

        symbols = df['品种'].tolist()
        x = range(len(symbols))

        # 胜率
        win_rates = [float(str(v).replace('%', '')) for v in df['胜率']]
        colors_wr = ['#27ae60' if wr >= 50 else '#e74c3c' for wr in win_rates]
        ax1.bar(x, win_rates, color=colors_wr, width=0.6)
        ax1.axhline(y=50, color='gray', linewidth=1, linestyle='--', label='50%基准线')
        ax1.set_xticks(x)
        ax1.set_xticklabels(symbols, rotation=45, ha='right', fontsize=9)
        ax1.set_ylabel('胜率 (%)', fontsize=11)
        ax1.set_title('各品种胜率', fontsize=12, fontweight='bold')
        ax1.legend()
        ax1.set_ylim(0, 110)

        # 盈亏比
        pf_values = []
        for v in df['盈亏比']:
            try:
                pf_values.append(float(v) if v != '∞' else 10)
            except (ValueError, TypeError):
                pf_values.append(0)
        colors_pf = ['#27ae60' if pf >= 1 else '#e74c3c' for pf in pf_values]
        ax2.bar(x, pf_values, color=colors_pf, width=0.6)
        ax2.axhline(y=1, color='gray', linewidth=1, linestyle='--', label='盈亏平衡线')
        ax2.set_xticks(x)
        ax2.set_xticklabels(symbols, rotation=45, ha='right', fontsize=9)
        ax2.set_ylabel('盈亏比', fontsize=11)
        ax2.set_title('各品种盈亏比', fontsize=12, fontweight='bold')
        ax2.legend()

        fig.suptitle(title, fontsize=14, fontweight='bold', y=1.02)
        plt.tight_layout()
        path = os.path.join(self.output_dir, 'win_rate_profit_factor.png')
        fig.savefig(path, dpi=120, bbox_inches='tight')
        plt.close(fig)
        return path


# ============================================================
# 自动补数
# ============================================================
def auto_download_missing(missing_dates: list[str], config: dict):
    """调用 cfmmc_crawler 补充缺失的数据"""
    try:
        from cfmmc_crawler import CFMMCCrawler
    except ImportError:
        print('错误: 无法导入 cfmmc_crawler，请确认文件存在')
        return

    for account in config.get('accounts', []):
        crawler = CFMMCCrawler(
            account['fund_name'], account['broker'],
            account['account_no'], account['password'],
            config['output_dir'],
            auto_recognize=True
        )
        print(f'\n正在登录 {account["fund_name"]} - {account["broker"]} ...')
        try:
            crawler.login()
        except Exception as e:
            print(f'登录失败: {e}')
            continue

        if crawler.token:
            for d in missing_dates:
                dt_obj = datetime.strptime(d, '%Y-%m-%d').date()
                for qt in ['逐日', '逐笔']:
                    try:
                        crawler.get_daily_data(dt_obj, qt)
                    except Exception as e:
                        print(f'  下载失败 {d} {qt}: {e}')
            crawler.logout()


# ============================================================
# 报告输出
# ============================================================
def print_report(result: dict, visualizer: ReviewVisualizer = None):
    """打印复盘报告"""
    ov = result['overview']
    sym = result['symbol_summary']
    trades = result['trades_sorted']
    trend = result['daily_trend']

    print('\n' + '=' * 70)
    print(f'  📊 期货交易复盘报告  |  {ov["查询范围"]}')
    print('=' * 70)

    # 总览
    print(f'\n┌─── 总览 ───────────────────────────────────────┐')
    print(f'│  交易日数:     {ov["交易日数"]:>6}                          │')
    print(f'│  总成交笔数:   {ov["总成交笔数"]:>6}                          │')
    print(f'│  品种数:       {ov["品种数"]:>6}                          │')
    print(f'│  ─────────────────────────────────────────── │')
    print(f'│  总盈亏:       {ov["总盈亏(元)"]:>+12,.2f} 元              │')
    print(f'│  总手续费:     {ov["总手续费(元)"]:>12,.2f} 元              │')
    print(f'│  净盈亏:       {ov["净盈亏(元)"]:>+12,.2f} 元              │')
    print(f'└──────────────────────────────────────────────┘')

    # 品种汇总
    if not sym.empty:
        print(f'\n┌─── 品种汇总（按净盈亏排序）────────────────────┐')
        print(sym.to_string(index=False))
        print(f'└──────────────────────────────────────────────┘')

    # TOP 盈利/亏损交易
    if not trades.empty:
        print(f'\n┌─── TOP 10 盈利交易 ───────────────────────────┐')
        top_wins = trades.head(10)[['date', 'contract', 'direction', 'open_close',
                                     'price', 'volume', 'realized_pnl', 'commission']].copy()
        top_wins.columns = ['日期', '合约', '方向', '开平', '价格', '手数', '平仓盈亏', '手续费']
        top_wins['日期'] = top_wins['日期'].astype(str).str[:10]
        print(top_wins.to_string(index=False))
        print(f'└──────────────────────────────────────────────┘')

        print(f'\n┌─── TOP 10 亏损交易 ───────────────────────────┐')
        top_losses = trades.tail(10)[['date', 'contract', 'direction', 'open_close',
                                       'price', 'volume', 'realized_pnl', 'commission']].iloc[::-1].copy()
        top_losses.columns = ['日期', '合约', '方向', '开平', '价格', '手数', '平仓盈亏', '手续费']
        top_losses['日期'] = top_losses['日期'].astype(str).str[:10]
        print(top_losses.to_string(index=False))
        print(f'└──────────────────────────────────────────────┘')

    # 可视化
    if visualizer:
        print('\n📈 正在生成图表...')
        paths = []
        p1 = visualizer.plot_symbol_pnl_bar(sym)
        if p1:
            paths.append(p1)
        p2 = visualizer.plot_daily_trend(trend)
        if p2:
            paths.append(p2)
        p3 = visualizer.plot_symbol_pie(sym)
        if p3:
            paths.append(p3)
        p4 = visualizer.plot_win_rate(sym)
        if p4:
            paths.append(p4)
        for p in paths:
            print(f'  ✅ {p}')

    print()


# ============================================================
# CLI 入口
# ============================================================
def main():
    parser = argparse.ArgumentParser(description='期货交易复盘系统')
    parser.add_argument('--start', type=str, help='开始日期 YYYYMMDD 或 YYYY-MM-DD')
    parser.add_argument('--end', type=str, help='结束日期 YYYYMMDD 或 YYYY-MM-DD')
    parser.add_argument('--output-dir', type=str, default=None, help='数据目录')
    parser.add_argument('--no-chart', action='store_true', help='不生成图表')
    parser.add_argument('--auto-download', action='store_true', help='自动下载缺失数据')
    args = parser.parse_args()

    # 加载配置
    config = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)

    output_dir = args.output_dir or config.get('output_dir', DEFAULT_OUTPUT_DIR)

    # 格式化日期
    def fmt_date(d):
        d = d.strip()
        if len(d) == 8:
            return f'{d[:4]}-{d[4:6]}-{d[6:]}'
        return d

    start = fmt_date(args.start) if args.start else None
    end = fmt_date(args.end) if args.end else None

    # 交互式输入
    if not start:
        start = input('开始日期 (YYYYMMDD): ').strip()
        start = fmt_date(start)
    if not end:
        end = input('结束日期 (YYYYMMDD): ').strip()
        end = fmt_date(end)

    # 加载数据
    loader = DataLoader(output_dir)
    loader.load_all()

    # 检查缺失
    missing = loader.get_missing_dates(start, end)
    if missing and args.auto_download:
        print(f'\n发现 {len(missing)} 个缺失交易日，自动下载中...')
        auto_download_missing(missing, config)
        loader.load_all()  # 重新加载
        missing = loader.get_missing_dates(start, end)

    if missing:
        print(f'\n⚠️  有 {len(missing)} 个交易日缺少数据: {missing[:5]}{"..." if len(missing)>5 else ""}')

    # 分析
    engine = ReviewEngine(loader)
    result = engine.query_by_date_range(start, end)

    # 可视化 & 输出
    viz = None if args.no_chart else ReviewVisualizer()
    print_report(result, viz)


if __name__ == '__main__':
    main()
